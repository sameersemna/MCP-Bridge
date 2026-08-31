import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from mcp import StdioServerParameters as _SdkStdioServerParameters
except ImportError:  # pragma: no cover - allows the package to import in minimal environments
    class _SdkStdioServerParameters:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

from mcp_bridge.mcp_clients.stdio_transport import StdioServerParameters, stdio_client

from mcp_bridge.mcp_clients.session import McpClientSession
from .AbstractClient import GenericMcpClient


# Keywords to identify virtual environment variables
venv_keywords = ["CONDA", "VIRTUAL", "PYTHON"]

class StdioClient(GenericMcpClient):
    config: StdioServerParameters

    def __init__(self, name: str, config: StdioServerParameters) -> None:
        super().__init__(name=name)

        # logger.debug(f"initializing settings for {name}: {config.command} {" ".join(config.args)}")

        own_config = config.model_copy(deep=True)

        env = dict(os.environ.copy())

        env = {
            key: value for key, value in env.items()
            if not any(key.startswith(keyword) for keyword in venv_keywords)
        }

        if config.env is not None:
            env.update(config.env)

        compat_dir = str(Path(__file__).resolve().parent.parent / "compat")
        pythonpath_entries = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
        if compat_dir not in pythonpath_entries:
            pythonpath_entries.insert(0, compat_dir)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

        own_config.env = env

        command = shutil.which(config.command)
        if command is None:
            # Raise instead of terminating the process: a single misconfigured
            # server (missing binary) must not kill the whole bridge. The
            # caller (McpClientManager.initialize) catches this and records the
            # server as failed, leaving the rest of the bridge running.
            raise RuntimeError(
                f"could not find command '{config.command}' for MCP server '{name}'"
            )

        own_config.command = command

        # this changes the default to ignore
        if "encoding_error_handler" not in config.model_fields_set:
            own_config.encoding_error_handler = "ignore"

        self.config = own_config

    async def _maintain_session(self) -> None:
        logger.debug(f"starting maintain session for {self.name}")
        async with stdio_client(self.config) as client:
            logger.debug(f"entered stdio_client context manager for {self.name}")
            assert client[0] is not None, f"missing read stream for {self.name}"
            assert client[1] is not None, f"missing write stream for {self.name}"
            async with McpClientSession(*client) as session:
                logger.debug(f"entered client session context manager for {self.name}")
                await session.initialize()
                logger.debug(f"finished initialise session for {self.name}")
                self.session = session

                try:
                    while True:
                        await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    logger.debug(f"session maintainer cancelled for {self.name}")
                    raise
                except Exception as exc:
                    logger.error(f"session maintenance failed for {self.name}: {exc}")
                    self.session = None
                    raise

        logger.debug(f"exiting session for {self.name}")
