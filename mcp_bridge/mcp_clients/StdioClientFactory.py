from mcp import StdioServerParameters, stdio_client
from loguru import logger
import shutil
import os


async def construct_stdio_client(config: StdioServerParameters):
    logger.log("DEBUG", "Constructing Stdio Server")

    env = dict(os.environ.copy())

    if config.env is not None:
        env.update(config.env)

    command = shutil.which(config.command)
    if command is None:
        logger.error(f"could not find command {config.command}")
        exit(1)

    server_parameters = StdioServerParameters(
        command=command,
        args=config.args,
        env=env,
    )

    return stdio_client(server_parameters)
