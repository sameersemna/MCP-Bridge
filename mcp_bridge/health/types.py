from pydantic import BaseModel, Field
from typing import Literal, Optional
import datetime


class UnhealthyEvent(BaseModel):
    """Represents an unhealthy event"""

    name: str = Field(..., description="Name of the event")
    severity: Literal["error", "warning"] = Field(
        ..., description="Severity of the event"
    )
    traceback: Optional[str] = Field(default=None, description="Traceback of the error")
    timestamp: str = Field(
        default_factory=lambda: datetime.datetime.now().isoformat(),
        description="Time of the event",
    )


class MCPServerHealth(BaseModel):
    """Represents the runtime health of a configured MCP server."""

    name: str = Field(..., description="Configured MCP server name")
    status: Literal["online", "offline", "degraded"] = Field(
        ..., description="Runtime status of the MCP server"
    )
    detail: str | None = Field(default=None, description="Optional detail about the state")


class HealthCheckResponse(BaseModel):
    """Represents a health check response"""

    status: Literal["ok", "error"] = Field(..., description="Server status")
    unhealthy_events: list[UnhealthyEvent] = Field(
        default_factory=list, description="List of unhealthy events"
    )
    mcp_servers: list[MCPServerHealth] = Field(
        default_factory=list, description="Runtime status of configured MCP servers"
    )
    mcp_inventory: dict[str, list[str]] | None = Field(
        default=None,
        description="Latest startup inventory summary for MCP servers",
    )
