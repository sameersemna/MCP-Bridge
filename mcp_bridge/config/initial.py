from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, Json
from typing import Optional

import warnings

# The `json` field below intentionally shadows `BaseSettings.json`. It is a
# documented public env var (MCP_BRIDGE__CONFIG__JSON) and cannot be renamed.
# Suppress the pydantic warning at its source, before the model is instantiated.
warnings.filterwarnings(
    "ignore",
    message='Field name "json" in "InitialSettings" shadows an attribute in parent "BaseSettings"',
    category=UserWarning,
)

__all__ = ["initial_settings"]


class InitialSettings(BaseSettings):
    file: Optional[str] = Field("config.json")
    http_url: Optional[str] = Field(None)
    json: Optional[Json] = Field(None)  # allow for raw config to be passed as env var

    load_config: bool = Field(
        True, json_schema_extra={"include_in_schema": False}
    )  # this can be used to disable loading the config

    model_config = SettingsConfigDict(
        env_prefix="MCP_BRIDGE__CONFIG__",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


# This will load the InitialSettings from environment variables
initial_settings = InitialSettings()
