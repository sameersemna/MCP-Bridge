from sys import settrace
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, BaseModel, Field


class InferenceServer(BaseModel):
    base_url: str = Field(description="Base URL of the inference server")
    api_key: str = Field(
        default="unauthenticated", description="API key for the inference server"
    )


class Settings(BaseSettings):
    inference_server: InferenceServer = Field(
        description="Inference server configuration"
    )

    model_config = SettingsConfigDict(
        env_prefix="MCP_BRIDGE__",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )


settings = Settings()  # type: ignore
