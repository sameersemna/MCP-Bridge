import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Literal

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from loguru import logger

import mcp.types as types


DEFAULT_INHERITED_ENV_VARS = (
    [
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "USERNAME",
        "USERPROFILE",
    ]
    if sys.platform == "win32"
    else ["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"]
)


def get_default_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in DEFAULT_INHERITED_ENV_VARS:
        value = os.environ.get(key)
        if value is None:
            continue
        if value.startswith("()"):
            continue
        env[key] = value
    return env


class StdioServerParameters:
    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        encoding: str = "utf-8",
        encoding_error_handler: Literal["strict", "ignore", "replace"] = "strict",
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self.encoding = encoding
        self.encoding_error_handler = encoding_error_handler

    def model_copy(self, deep: bool = True) -> "StdioServerParameters":
        return StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=dict(self.env) if self.env is not None else None,
            encoding=self.encoding,
            encoding_error_handler=self.encoding_error_handler,
        )

    @property
    def model_fields_set(self) -> set[str]:
        return set()


@asynccontextmanager
async def stdio_client(server: StdioServerParameters):
    read_stream: MemoryObjectReceiveStream[types.JSONRPCMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[types.JSONRPCMessage | Exception]
    write_stream: MemoryObjectSendStream[types.JSONRPCMessage]
    write_stream_reader: MemoryObjectReceiveStream[types.JSONRPCMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    process = await asyncio.create_subprocess_exec(
        server.command,
        *server.args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=server.env if server.env is not None else get_default_environment(),
    )

    async def stdout_reader() -> None:
        assert process.stdout is not None
        buffer = ""
        try:
            async with read_stream_writer:
                while True:
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=0.2)
                    except asyncio.TimeoutError:
                        if process.returncode is not None:
                            break
                        continue
                    if not chunk:
                        break
                    text = chunk.decode(server.encoding, errors=server.encoding_error_handler)
                    buffer += text
                    while "\n" in buffer:
                        payload, buffer = buffer.split("\n", 1)
                        if not payload.strip():
                            continue
                        try:
                            message = types.JSONRPCMessage.model_validate_json(payload)
                        except Exception as exc:
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(message)
                if buffer.strip():
                    try:
                        message = types.JSONRPCMessage.model_validate_json(buffer)
                    except Exception as exc:
                        await read_stream_writer.send(exc)
                    else:
                        await read_stream_writer.send(message)
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()
        except Exception as exc:  # pragma: no cover - defensive fallback
            try:
                await read_stream_writer.send(exc)
            except Exception:
                pass

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_stream_reader:
                async for message in write_stream_reader:
                    payload = message.model_dump_json(by_alias=True, exclude_none=True)
                    process.stdin.write(
                        (payload + "\n").encode(
                            encoding=server.encoding,
                            errors=server.encoding_error_handler,
                        )
                    )
                    await process.stdin.drain()
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"stdio stdin writer stopped: {exc}")

    async def stderr_reader() -> None:
        assert process.stderr is not None
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode(server.encoding, errors=server.encoding_error_handler).rstrip()
                if text:
                    logger.debug(f"stdio server stderr: {text}")
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"stdio stderr reader stopped: {exc}")

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        tg.start_soon(stderr_reader)
        try:
            yield read_stream, write_stream
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                    await process.stdin.wait_closed()
            except Exception:
                pass
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2)
            except Exception:
                process.kill()
                await process.wait()
            tg.cancel_scope.cancel()
