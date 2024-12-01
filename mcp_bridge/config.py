from typing import Annotated, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field

from mcp.client.stdio import StdioServerParameters


class InferenceServer(BaseModel):
    base_url: str = Field(description="Base URL of the inference server")
    api_key: str = Field(
        default="unauthenticated", description="API key for the inference server"
    )


class SSEMCPServer(BaseModel):
    # TODO: expand this once I find a good definition for this
    url: str = Field(description="URL of the MCP server")


MCPServer = Annotated[
    Union[StdioServerParameters, SSEMCPServer],
    Field(description="MCP server configuration"),
]


class Settings(BaseSettings):
    inference_server: InferenceServer = Field(
        description="Inference server configuration"
    )

    mcp_servers: dict[str, MCPServer] = Field(description="MCP servers configuration")

    model_config = SettingsConfigDict(
        env_prefix="MCP_BRIDGE__",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )


settings = Settings()  # type: ignore
