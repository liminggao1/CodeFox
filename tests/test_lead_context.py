"""通过会话输入和保存验证 Lead 状态拥有者。"""

from test_interactive_runtime import runtime_factory, response, tool, until
from test_conversation_records import stored


def test_todo_input_and_snapshot_cannot_mutate_runtime_state(runtime_factory):
    todos = [{"content": "verify", "status": "pending"}]
    runtime, _ = runtime_factory([
        response(tools=[tool("todo_write", todos=todos)], stop="tool_use"), response()])
    submitted = runtime.submit("plan")
    until(runtime, "turn_finished", submission_id=submitted.submission_id)
    todos[0]["content"] = "changed externally"
    snapshot = runtime.lead_context.snapshot()
    snapshot["todos"].clear()
    assert runtime.lead_context.todos == [{"content": "verify", "status": "pending"}]
    session_id = runtime.lead_context.session_id
    runtime.session.save()
    assert runtime.lead_context.session_id == session_id == runtime.session.session_id
    assert stored(runtime)["todos"] == runtime.lead_context.todos
