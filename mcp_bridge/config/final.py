from typing import Annotated, Any, Literal, Union
from urllib.parse import urlparse
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, field_validator, ConfigDict

try:
    from mcp.client.stdio import StdioServerParameters
except ImportError:  # pragma: no cover - fallback for environments without the SDK installed
    class StdioServerParameters(BaseModel):
        model_config = ConfigDict(extra="forbid")

        command: str = Field(default="python")
        args: list[str] = Field(default_factory=list)
        env: dict[str, str] = Field(default_factory=dict)
        cwd: str | None = None

        @field_validator("command")
        @classmethod
        def validate_command(cls, value: str) -> str:
            if not value:
                raise ValueError("stdio MCP server requires a non-empty command")
            return value

try:
    from mcpx.client.transports.docker import DockerMCPServer
except ImportError:  # pragma: no cover - fallback for environments without the SDK installed
    class DockerMCPServer(BaseModel):
        model_config = ConfigDict(extra="forbid")

        image: str | None = None
        command: list[str] = Field(default_factory=list)
        env: dict[str, str] = Field(default_factory=dict)
        volumes: list[str] = Field(default_factory=list)

        @field_validator("image")
        @classmethod
        def validate_image(cls, value: str | None) -> str | None:
            if value is not None and not value.strip():
                raise ValueError("docker MCP server image must be non-empty when provided")
            return value


class InferenceServer(BaseModel):
    base_url: str = Field(
        default="http://localhost:11434/v1",
        description="Base URL of the inference server",
    )
    api_key: str = Field(
        default="unauthenticated", description="API key for the inference server"
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("base_url must be a valid absolute URL")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("base_url must use http or https")
        return value.rstrip("/")


class Logging(BaseModel):
    log_level: Literal["INFO", "DEBUG"] = Field("INFO", description="default log level")
    log_server_pings: bool = Field(False, description="log server pings")


class SamplingModel(BaseModel):
    model: Annotated[str, Field(description="Name of the sampling model")]

    intelligence: Annotated[
        float, Field(description="Intelligence of the sampling model")
    ] = 0.5
    cost: Annotated[float, Field(description="Cost of the sampling model")] = 0.5
    speed: Annotated[float, Field(description="Speed of the sampling model")] = 0.5


class Sampling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout: Annotated[int, Field(description="Timeout for sampling requests")] = 10
    models: Annotated[
        list[SamplingModel], Field(description="List of sampling models")
    ] = Field(default_factory=list)


class SSEMCPServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="URL of the MCP server")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("url must be a valid absolute URL")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("url must use http or https")
        return value.rstrip("/")


MCPServer = Annotated[
    Union[StdioServerParameters, SSEMCPServer, DockerMCPServer],
    Field(description="MCP server configuration"),
]


class Network(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field("0.0.0.0", description="Host of the network")
    port: int = Field(8000, description="Port of the network")

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value:
            raise ValueError("host cannot be empty")
        return value

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return value


class Cors(BaseModel):
    enabled: bool = Field(True, description="Enable CORS")
    allow_origins: list[str] = Field(["*"], description="Allowed origins")
    allow_credentials: bool = Field(True, description="Allow credentials")
    allow_methods: list[str] = Field(["*"], description="Allowed methods")
    allow_headers: list[str] = Field(["*"], description="Allowed headers")


class ApiKey(BaseModel):
    key: str = Field(..., description="API key")
    permissions: Literal["all"] = Field(
        "all", description="API key permissions"
    )  # TODO: Add support for other permissions


class Auth(BaseModel):
    enabled: bool = Field(False, description="Enable authentication")
    api_keys: list[ApiKey] = Field(default_factory=list, description="API keys")


class Security(BaseModel):
    model_config = ConfigDict(extra="forbid")

    CORS: Cors = Field(default_factory=Cors, description="CORS configuration")
    auth: Auth = Field(default_factory=Auth, description="Authentication configuration")


class Telemetry(BaseModel):
    """Telemetry configuration

    open-telemetry is entirely local to your own infrastructure and does not send any data to any external service unless you configure it to do so
    
    defaults to false since we cannot assume you are actually running an open telemetry collector on your machine.
    """
    enabled: bool = Field(False, description="Enable telemetry")
    service_name: str = Field(
        default="MCP Bridge", description="Name of the service"
    )
    otel_endpoint: str = Field(
        default="http://jaeger:4318/v1/traces",
        description="Endpoint for the OTEL exporter",
    )

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_BRIDGE__",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        cli_parse_args=False,
        cli_avoid_json=True,
        extra="ignore",
    )
    inference_server: InferenceServer = Field(
        default_factory=InferenceServer,
        description="Inference server configuration",
    )

    mcp_servers: dict[str, MCPServer] = Field(
        default_factory=dict, description="MCP servers configuration"
    )

    sampling: Sampling = Field(default_factory=Sampling, description="sampling config")

    logging: Logging = Field(default_factory=Logging, description="logging config")

    network: Network = Field(default_factory=Network, description="network config")

    security: Security = Field(default_factory=Security, description="security config")

    telemetry: Telemetry = Field(default_factory=Telemetry, description="telemetry config")

