"""允许后的工具执行和后置 Hook 结算；不管理会话保存。"""

from dataclasses import dataclass

from ..tools.tool_result import ToolResult
from . import hooks, interaction


@dataclass(frozen=True)
class ToolExecution:
    result: ToolResult
    post_error: Exception | None = None


def call_tool_handler(handler, args, name):
    """统一执行同步处理器；未知工具和普通执行异常转换成明确失败。

    参数字典由协议层解析；权限在调用前检查。处理器必须返回 ToolResult，
    不从正文猜测状态，也不接受旧的字符串契约。
    """
    if handler is None:
        return ToolResult(f"Unknown tool: {name}", "failed")
    try:
        result = handler(**(args or {}))
        if not isinstance(result, ToolResult):
            raise TypeError(f"Tool '{name}' must return ToolResult")
        return result
    except Exception as exc:
        return ToolResult(f"Error: {type(exc).__name__}: {exc}", "failed")


def execute_tool(block, handler):
    """处理器仅执行一次；Hook 错误与真实工具结果分别返回。

    Lead、child 和后台负责先结算返回的真实结果，再终止所属执行。
    本模块只发布生命周期，不写会话文件或重试工具。
    """
    with interaction.operation_context(tool_call_id=getattr(block, "id", None)):
        operation = interaction.operation_status("tool", block.name, "running")
        result = call_tool_handler(handler, block.input, block.name)
        interaction.operation_status("tool", block.name, "postprocessing", operation_id=operation)
        post_error = None
        try:
            hooks.trigger_hooks("PostToolUse", block, result.content)
        except Exception as exc:
            post_error = exc
        interaction.operation_status("tool", block.name,
            "failed" if result.status == "failed" else "completed", operation_id=operation,
            **({"reason": result.content} if result.status == "failed" else {}))
        return ToolExecution(result, post_error)
