from typing import Annotated, Any, Literal, Union
from urllib.parse import urlparse
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, field_validator, ConfigDict, model_validator


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    disabled: bool | None = Field(default=None, description="Whether this server is disabled")

try:
    from mcp.client.stdio import StdioServerParameters
except ImportError:  # pragma: no cover - fallback for environments without the SDK installed
    class StdioServerParameters(MCPServerConfig):
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
    class DockerMCPServer(MCPServerConfig):
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
    model_context_windows: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Optional map of model ID -> context window length (in tokens), e.g. "
            '{"nvidia/nemotron-3-super-120b-a12b:free": 128000, "minimax/minimax-m3:free": 1000000}. '
            "Used to derive the tool-loop context budget per model. If a model is not listed, "
            "the bridge falls back to a heuristic from the model ID, then to a default."
        ),
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


class SSEMCPServer(MCPServerConfig):
    model_config = ConfigDict(extra="forbid")

    type: Literal["http", "sse"] | None = Field(
        default=None,
        description="Transport type for the MCP server",
    )
    url: str = Field(description="URL of the MCP server")
    auth: dict[str, Any] = Field(
        default_factory=dict,
        description="Authentication configuration for the MCP server",
    )
    requestTimeout: int | None = Field(
        default=None,
        description="Request timeout in milliseconds",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("url must be a valid absolute URL")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("url must use http or https")
        return value.rstrip("/")

    @field_validator("requestTimeout")
    @classmethod
    def validate_request_timeout(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("requestTimeout must be greater than zero")
        return value


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

    @model_validator(mode="after")
    def validate_credentials_with_wildcard(self) -> "Cors":
        # Browsers reject `Access-Control-Allow-Origin: *` combined with
        # `Access-Control-Allow-Credentials: true`. A wildcard origin with
        # credentials is a misconfiguration that silently breaks CORS for
        # credentialed clients, so force credentials off when origins are
        # wildcarded (the safe, spec-compliant default).
        if self.allow_credentials and "*" in self.allow_origins:
            self.allow_credentials = False
        return self


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
    disabled_mcp_servers: set[str] = Field(
        default_factory=set,
        description="Names of MCP servers that are disabled in configuration",
    )
    cached_mcp_servers: set[str] = Field(
        default_factory=set,
        description=(
            "Names of MCP servers whose tool results are cached (persistent + "
            "in-memory). Opt-in per server via `\"cached\": true` in the server's "
            "config. Defaults to empty (no caching) for all servers."
        ),
    )

    sampling: Sampling = Field(default_factory=Sampling, description="sampling config")

    logging: Logging = Field(default_factory=Logging, description="logging config")

    network: Network = Field(default_factory=Network, description="network config")

    security: Security = Field(default_factory=Security, description="security config")

    telemetry: Telemetry = Field(default_factory=Telemetry, description="telemetry config")

    @model_validator(mode="before")
    @classmethod
    def collect_disabled_mcp_servers(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_servers = data.get("mcp_servers")
            if isinstance(raw_servers, dict):
                disabled_servers = {
                    name
                    for name, server_config in raw_servers.items()
                    if isinstance(server_config, dict) and server_config.get("disabled")
                }
                if disabled_servers:
                    data = dict(data)
                    data.setdefault("disabled_mcp_servers", disabled_servers)
        return data

    @model_validator(mode="before")
    @classmethod
    def collect_cached_mcp_servers(cls, data: Any) -> Any:
        """Collect the names of MCP servers opted into tool-result caching.

        The `cached` flag is read from the *raw* config dict because the SDK's
        ``StdioServerParameters`` (and other transport models) use
        ``extra="ignore"`` and would silently drop an unknown ``cached`` field.
        Mirroring ``collect_disabled_mcp_servers``, we capture it here into a
        dedicated ``cached_mcp_servers`` set.

        Crucially, the ``cached`` key is then **removed** from each server's
        config dict. Transport models like ``SSEMCPServer`` use
        ``extra="forbid"`` and would otherwise reject the unknown ``cached``
        field, breaking config loading entirely.
        """
        if isinstance(data, dict):
            raw_servers = data.get("mcp_servers")
            if isinstance(raw_servers, dict):
                cached_servers = {
                    name
                    for name, server_config in raw_servers.items()
                    if isinstance(server_config, dict) and server_config.get("cached")
                }
                if cached_servers:
                    data = dict(data)
                    data.setdefault("cached_mcp_servers", cached_servers)
                    # Strip `cached` from each server config so the transport
                    # models (extra="forbid") accept them.
                    data["mcp_servers"] = {
                        name: {
                            key: value
                            for key, value in server_config.items()
                            if key != "cached"
                        }
                        if isinstance(server_config, dict)
                        else server_config
                        for name, server_config in raw_servers.items()
                    }
        return data

