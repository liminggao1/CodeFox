"""Lead 工具批次：compact、普通工具与后台派发。

权限由 runtime.hooks 负责，同步执行由 runtime.tool_execution 负责。host 管理模型迭代；本模块
逐项结算一批工具，不请求模型，也不决定回合是否结束。
"""

from ..runtime import hooks, interaction, scheduler, tool_execution
from ..tools.tool_result import ToolResult
from ..workspace import tasks


def execute_tool_batch(blocks, handlers, manual_compaction, turn):
    """按顺序执行工具，并逐项记录结果与保存检查点。"""
    # 本次响应包含工具调用，准备收集工具执行结果。
    results = []
    # 按模型返回顺序处理每个内容块。
    for index, block in enumerate(blocks):
        # 非工具块保留在助手消息中，无需执行。
        if block.type != "tool_use":
            # 跳过文本等非工具内容。
            continue
        result, status, post_error = _execute_tool(
            block, handlers, manual_compaction, turn.model_call_id)
        results.append(result)
        # 将同步工具输出与原调用 ID 配对。
        if post_error is not None:
            # 先把真实结果写回历史，保存后再传播后置处理失败。
            remaining = [(_interrupted(pending)[0], "stopped")
                         for pending in blocks[index + 1:] if pending.type == "tool_use"]
            results.extend(item for item, _ in remaining)
            turn.append({"role": "user", "content": results})
            for item, state in remaining:
                turn.record_tool_result(item, state)
        turn.record_tool_result(result, status)
        if post_error is not None:
            raise post_error
    return results


def _interrupted(block):
    return ToolResult("Interrupted before execution", "stopped").model_result(block.id), "stopped", None


def _execute_tool(block, handlers, manual_compaction, model_call_id):
    # 停止与 compact 的特殊路径优先于普通工具钩子。
    if interaction.stopped():
        return _interrupted(block)
    # 在终端显示当前工具名称。
    if interaction.current_context() is None:
        print(f"\033[36m> {block.name}\033[0m")
    # 将手动压缩请求交给压缩模块处理。
    with interaction.operation_context(model_call_id=model_call_id):
        compact_result = manual_compaction.handle_tool_use(block)
    # compact 不经过普通工具钩子和执行器。
    if compact_result is not None:
        status = "failed" if compact_result.get("is_error") else "returned"
        # 保留与模型工具调用对应的结果。
        # 其他工具继续按原有路径执行。
        return compact_result, status, None

    with interaction.operation_context(model_call_id=model_call_id):
        check = hooks.check_tool_use(block, stopped=interaction.stopped,
                                     cwd=lambda: tasks.assignment_cwd("agent"))
    if check.status != "allowed":
        return ToolResult(check.text, check.status).model_result(block.id), check.status, None

    # 后台工具复用前置检查，在实际完成后执行自己的后置 hook。
    # 可后台执行的工具不阻塞当前模型轮次。
    if scheduler.should_run_background(block.name, block.input):
        if interaction.stopped():
            return _interrupted(block)
        # 启动后台任务；失败时也要返回工具结果。
        try:
            # 将工具调用交给后台任务调度器。
            with interaction.operation_context(model_call_id=model_call_id, tool_call_id=block.id):
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
        return ToolResult(output, status).model_result(block.id), status, None

    # 根据工具名查找同步处理函数。
    handler = handlers.get(block.name)
    # 执行工具，并把未知工具或执行异常转成结果文本。
    with interaction.operation_context(model_call_id=model_call_id):
        execution = tool_execution.execute_tool(block, handler)
    # 在终端显示截短的执行结果。
    if interaction.current_context() is None:
        print(execution.result.content[:300])
    return execution.result.model_result(block.id), execution.result.status, execution.post_error
