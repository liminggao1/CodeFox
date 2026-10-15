"""Lead 单工具策略：compact、普通工具与后台派发。

权限由 runtime.hooks 负责，同步执行由 runtime.tool_execution 负责。TurnEngine
管理批次和模型迭代；本模块只返回单次 ToolExecution，不写历史或决定回合结束。
"""

from ..runtime import hooks, interaction, scheduler, tool_execution
from ..runtime.tool_execution import ToolExecution
from ..tools.tool_result import ToolResult
from ..workspace import tasks


def execute_tool(block, handlers, manual_compaction) -> ToolExecution:
    """保留 Lead 的特殊路径，批次顺序与配对由共享执行机负责。"""
    # 停止与 compact 的特殊路径优先于普通工具钩子。
    if interaction.stopped():
        return ToolExecution(ToolResult("Interrupted before execution", "stopped"))
    # 在终端显示当前工具名称。
    if interaction.current_context() is None:
        print(f"\033[36m> {block.name}\033[0m")
    # 将手动压缩请求交给压缩模块处理。
    compact_result = manual_compaction.handle_tool_use(block)
    # compact 不经过普通工具钩子和执行器。
    if compact_result is not None:
        status = "failed" if compact_result.get("is_error") else "returned"
        # 保留与模型工具调用对应的结果。
        # 其他工具继续按原有路径执行。
        return ToolExecution(ToolResult(compact_result["content"], status))

    check = hooks.check_tool_use(block, stopped=interaction.stopped,
                                 cwd=lambda: tasks.assignment_cwd("agent"))
    if check.status != "allowed":
        return ToolExecution(ToolResult(check.text, check.status))

    # 后台工具复用前置检查，在实际完成后执行自己的后置 hook。
    # 可后台执行的工具不阻塞当前模型轮次。
    if scheduler.should_run_background(block.name, block.input):
        if interaction.stopped():
            return ToolExecution(ToolResult("Interrupted before execution", "stopped"))
        # 启动后台任务；失败时也要返回工具结果。
        try:
            # 将工具调用交给后台任务调度器。
            with interaction.operation_context(tool_call_id=block.id):
                bg_id = scheduler.start_background_task(block, handlers)
            # 告知模型结果稍后通过任务通知送达。
            output = (f"[Background task {bg_id} started] "
                      "Result will arrive as a task_notification.")
            status = "started"
        # 将启动异常转换成模型可读的结果。
        except Exception as exc:
            # 保留异常类型和信息，便于模型判断失败原因。
            output = f"Error: Failed to start background task: {type(exc).__name__}: {exc}"
            status = "failed"
        # 为本次后台工具调用登记返回内容。
        # 后台工具不再进入同步执行路径。
        return ToolExecution(ToolResult(output, status))

    # 根据工具名查找同步处理函数。
    handler = handlers.get(block.name)
    # 执行工具，并把未知工具或执行异常转成结果文本。
    execution = tool_execution.execute_tool(block, handler)
    # 在终端显示截短的执行结果。
    if interaction.current_context() is None:
        print(execution.result.content[:300])
    return execution
