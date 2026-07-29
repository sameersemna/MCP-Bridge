from collections import deque
from typing import Any

from .types import MCPServerHealth, UnhealthyEvent
from mcp_bridge.mcp_clients.McpClientManager import ClientManager

__all__ = ["manager"]


class HealthManager:
    """Manages the health of the server"""

    UnhealthyEvents: deque[UnhealthyEvent] = deque(
        maxlen=100
    )  # we do not want to memory leak
    last_inventory: dict[str, list[str]] | None = None

    def add_unhealthy_event(self, event: UnhealthyEvent) -> None:
        self.UnhealthyEvents.append(event)

    def get_unhealthy_events(self) -> list[UnhealthyEvent]:
        return list(self.UnhealthyEvents)

    def is_healthy(self) -> bool:
        return not any(event.severity == "error" for event in self.UnhealthyEvents)

    def get_mcp_inventory(self) -> dict[str, list[str]] | None:
        return self.last_inventory

    def get_mcp_server_health(self, client_manager: Any | None = None) -> list[MCPServerHealth]:
        server_health: list[MCPServerHealth] = []
        registry = client_manager or ClientManager

        for name, client in registry.get_clients():
            if client is None:
                server_health.append(
                    MCPServerHealth(name=name, status="offline", detail="client not initialized")
                )
                continue

            session = getattr(client, "session", None)
            if session is None:
                server_health.append(
                    MCPServerHealth(name=name, status="offline", detail="session not ready")
                )
            else:
                server_health.append(
                    MCPServerHealth(name=name, status="online", detail=None)
                )

        return server_health


manager: HealthManager = HealthManager()
