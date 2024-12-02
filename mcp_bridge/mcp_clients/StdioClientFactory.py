from mcp import ClientSession, StdioServerParameters, stdio_client
from .ClientInstance import ClientInstance
from loguru import logger
import shutil
import os

async def construct_stdio_client(config: StdioServerParameters):
    logger.log("DEBUG", "Constructing Stdio Server")

    env = dict(os.environ.copy())

    if config.env is not None:
        env.update(config.env)

    server_parameters = StdioServerParameters(
        command=shutil.which(config.command),
        args=config.args,
        env=env,
    )

    return stdio_client(server_parameters)