import docker.client
from contextlib import asynccontextmanager
import anyio
import anyio.lowlevel
import anyio.to_thread
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from loguru import logger
from config.final import DockerMCPServer
from mcp import types

@asynccontextmanager
async def docker_client(server: DockerMCPServer):
    """
    Client transport for Docker: this will connect to a server by
    running a Docker container and communicating with it over its stdin/stdout.
    """
    read_stream: MemoryObjectReceiveStream[types.JSONRPCMessage | Exception]
    read_stream_writer: MemoryObjectSendStream[types.JSONRPCMessage | Exception]

    write_stream: MemoryObjectSendStream[types.JSONRPCMessage]
    write_stream_reader: MemoryObjectReceiveStream[types.JSONRPCMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    client = docker.client.from_env()
    container = client.containers.run(
        image=server.image,
        name=server.container_name if server.container_name else None,
        command=server.args if server.args else None,
        environment=server.env if isinstance(server.env, (dict, list)) else {},
        detach=True,
        stdin_open=True,
        stdout=True,
        stderr=True,
        tty=False,
    )

    logger.debug(f"made instance of docker client for {container.name}")

    # Attach to container's input/output streams
    attach_socket = container.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
    attach_socket._sock.setblocking(False)

    async def read_from_stdout():
        try:
            async with read_stream_writer:
                buffer = ""
                while True:
                    chunk = await anyio.to_thread.run_sync(lambda: attach_socket._sock.recv(1024))
                    if not chunk:
                        break

                    chunk = chunk.decode("utf-8")
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()

                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            await read_stream_writer.send(exc)
                            continue

                        await read_stream_writer.send(message)
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def write_to_stdin():
        try:
            async with write_stream_reader:
                async for message in write_stream_reader:
                    json = message.model_dump_json(by_alias=True, exclude_none=True)
                    await anyio.to_thread.run_sync(lambda: attach_socket._sock.sendall((json + "\n").encode("utf-8")))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(read_from_stdout)
            tg.start_soon(write_to_stdin)
            yield read_stream, write_stream
    except Exception as e:
        logger.error(f"Error in docker client: {e}")
    finally:
        attach_socket._sock.close()
        # Do not stop or remove the container
        logger.debug(f"Container {container.name} remains running.")
