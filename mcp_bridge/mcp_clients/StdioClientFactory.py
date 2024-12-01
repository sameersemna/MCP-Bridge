from mcp import StdioServerParameters, stdio_client
from .ClientInstance import ClientInstance
from loguru import logger

async def construct_stdio_server(config: StdioServerParameters) -> ClientInstance:
    logger.log("DEBUG", "Constructing Stdio Server")
    server_parameters = StdioServerParameters(
        command=config.command,
        args=config.args,
        env=config.env,
    )

    
    async with stdio_client(server_parameters) as client:
        return ClientInstance(name="stdio", server=client)