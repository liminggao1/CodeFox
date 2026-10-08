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
        """将当前快照转换为系统提示词接收的字段字典。

        返回：包含记忆目录、召回正文、MCP 名称和队友名称的新字典。
            名称字段保留不可变元组，不暴露运行时的可变登记表。
        """
        return {
            "memory_catalog": self.memory_catalog,
            "memories": self.memories,
            "connected_mcp": self.connected_mcp,
            "active_teammates": self.active_teammates,
        }


def build_context(messages: list) -> RuntimeContext:
    """为本次主模型调用读取最新上下文并创建只读快照。

    输入：messages 为当前历史，供记忆选取使用。
    返回：RuntimeContext，包含记忆索引、相关正文、已连接 MCP 和活跃队友名称。
        记忆选取可能请求模型；读取或选取异常按记忆模块规则处理。
    """
    with teams.team_lock:
        teammate_names = tuple(teams.active_teammates)
    return RuntimeContext(
        memory_catalog=memory_runtime.read_memory_index(),
        memories=memory_runtime.load_memories(messages),
        connected_mcp=tuple(mcp_tools.mcp_clients),
        active_teammates=teammate_names,
    )
