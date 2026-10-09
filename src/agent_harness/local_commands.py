"""Shared local command names and presentation metadata; execution stays in runtime."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalCommand:
    name: str
    description: str
    usage: str
    requires_argument: bool = False


LOCAL_COMMANDS = (
    LocalCommand("/goal", "查看、设置或清除目标", "/goal [条件 | clear]"),
    LocalCommand("/resume", "打开会话列表或恢复会话", "/resume [会话 ID]"),
    LocalCommand("/delete", "删除非活动会话", "/delete <会话 ID>", True),
    LocalCommand("/quit", "退出并保存会话", "/quit"),
)


def find_command(name: str) -> LocalCommand | None:
    return next((command for command in LOCAL_COMMANDS if command.name == name), None)
