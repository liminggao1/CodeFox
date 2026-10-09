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
        """设置生成 ToolDefinition 列表的来源函数。

        输入：sources 为按装配顺序执行的无参来源函数。
        返回：None。替换来源并清空权限快照，实际装配在 snapshot 中执行。
        """
        self._sources = sources
        self._policies = MappingProxyType({})

    def snapshot(self) -> ToolSnapshot:
        """统一装配工具描述、处理器和权限，并检查跨来源名称冲突。

        返回：ToolSnapshot，含工具描述元组、名称到处理器及权限的只读映射。
            全部装配成功后才发布新权限表；重名或来源失败会抛出异常并保留旧权限。
        """
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
        """查询最近一次成功装配的工具权限。

        输入：name 为模型工具名称。
        返回：已登记策略；未知工具默认返回 confirm，要求交互确认。
        """
        return self._policies.get(name, "confirm")


REGISTRY = ToolRegistry()
