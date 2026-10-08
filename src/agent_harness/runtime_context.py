"""Build immutable snapshots of the Lead agent's prompt context."""

from dataclasses import dataclass

from . import memory_runtime
from . import mcp_tools
from . import teams


@dataclass(frozen=True)
class RuntimeContext:
    memory_catalog: str = ""
    memories: str = ""
    connected_mcp: tuple[str, ...] = ()
    active_teammates: tuple[str, ...] = ()

    def prompt_values(self) -> dict:
        """Return existing prompt keys without sharing mutable runtime state."""
        return {
            "memory_catalog": self.memory_catalog,
            "memories": self.memories,
            "connected_mcp": self.connected_mcp,
            "active_teammates": self.active_teammates,
        }


def build_context(messages: list) -> RuntimeContext:
    """Read current memory, MCP and teammate state for one model call."""
    with teams.team_lock:
        teammate_names = tuple(teams.active_teammates)
    return RuntimeContext(
        memory_catalog=memory_runtime.read_memory_index(),
        memories=memory_runtime.load_memories(messages),
        connected_mcp=tuple(mcp_tools.mcp_clients),
        active_teammates=teammate_names,
    )
