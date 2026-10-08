"""Assemble model schemas, handlers and permission policies in one snapshot."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ToolDefinition:
    schema: dict
    handler: object
    origin: str
    policy: str = "allow"


@dataclass(frozen=True)
class ToolSnapshot:
    tools: tuple[dict, ...]
    handlers: object
    policies: object


class ToolRegistry:
    def __init__(self):
        self._sources = ()
        self._policies = MappingProxyType({})

    def configure(self, *sources) -> None:
        """Use explicit source functions; the registry imports no source module."""
        self._sources = sources
        self._policies = MappingProxyType({})

    def snapshot(self) -> ToolSnapshot:
        tools, handlers, policies, origins = [], {}, {}, {}
        for source in self._sources:
            for entry in source():
                name = entry.schema["name"]
                if name in origins:
                    raise ValueError(
                        "MCP tool name collision after normalization: "
                        f"{name!r} maps both {origins[name]} and {entry.origin}"
                    )
                origins[name] = entry.origin
                tools.append(entry.schema)
                if entry.handler is not None:
                    handlers[name] = entry.handler
                policies[name] = entry.policy
        snapshot = ToolSnapshot(
            tuple(tools), MappingProxyType(handlers), MappingProxyType(policies)
        )
        # Publish policies only after the whole assembly has succeeded.
        self._policies = snapshot.policies
        return snapshot

    def policy_for(self, name: str) -> str:
        return self._policies.get(name, "confirm")


REGISTRY = ToolRegistry()
