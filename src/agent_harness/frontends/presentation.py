"""Pure summaries shared by the terminal adapters."""

import json
from datetime import datetime, timezone


STATUS_LABELS = {"returned": "已返回结果", "success": "成功", "failed": "失败",
                 "denied": "已拒绝", "stopped": "已停止", "started": "后台已启动",
                 "running": "进行中", "pending": "待执行", "completed": "已返回",
                 "postprocessing": "后置处理中", "truncated": "输出截断",
                 "discarded": "本次输出未采用", "interrupted": "上次中断，结束状态未记录"}


def full_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def short_text(value, limit=160):
    text = next((line.strip() for line in full_text(value).splitlines() if line.strip()), "无文本结果")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def arguments_summary(arguments):
    keys = ("path", "command", "name", "task_id", "prompt", "query", "subject")
    values = [str(arguments[key]) for key in keys if key in arguments]
    return short_text(" · ".join(values) if values else arguments, 110)


def operation_key(record):
    data = record["payload"]
    if record["type"] in {"tool_call", "tool_result"} or data.get("phase") == "tool":
        identity = data.get("tool_call_id") or data.get("id") or data.get("tool_use_id")
        phase = "tool"
    else:
        identity, phase = data.get("operation_id"), data.get("phase")
    return (record.get("turn_id"), record["source"], data.get("model_call_id"), phase, identity)


def lifecycle_summary(data):
    phase = {"model": "模型", "hook": "Hook", "tool": "工具", "goal": "目标检查", "memory": "记忆整理"}.get(data["phase"], data["phase"])
    name = " · ".join(filter(None, (data.get("event"), data["name"])))
    return f"{phase} · {name} · {STATUS_LABELS.get(data['state'], data['state'])}"


def format_elapsed(seconds):
    """Return the compact duration used by event rows."""
    if seconds is None:
        return ""
    seconds = max(0.0, float(seconds))
    if seconds >= 10:
        return f"{seconds:.0f}s"
    return f"{seconds:.1f}s"


def record_time(value):
    """Parse a persisted event time without making the UI own clock state."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def session_title(snapshot=None, records=()):
    """Build a stable display title from existing session data."""
    snapshot = snapshot or {}
    session_id = snapshot.get("session_id") or "未开始会话"
    for record in records:
        if record.get("type") == "user_message":
            text = str(record.get("payload", {}).get("text", "")).strip().replace("\n", " ")
            if text:
                return f"{session_id} · {short_text(text, 34)}"
    return session_id


def format_session_state(snapshot=None, *, records=()):
    """Format the read-only state projection consumed by TUI widgets."""
    snapshot = snapshot or {}
    goal = snapshot.get("goal") or {}
    goal_text = goal.get("condition") or goal.get("text") if isinstance(goal, dict) else goal
    goal_text = str(goal_text or "无目标")
    todos = snapshot.get("todos") or []
    todo_lines = [f"{todo.get('status', '')} · {todo.get('content', '')}" for todo in todos]
    subagents = snapshot.get("subagents") or []
    child_lines = [f"{child.get('name', '子 agent')} · {child.get('status', '')}" for child in subagents]
    usage = snapshot.get("lead_main_model_usage") or {}
    lines = [
        f"会话  {snapshot.get('session_id') or '未开始'}",
        f"状态  {session_status_label(snapshot)}",
        f"消息  {len(snapshot.get('history') or records)}",
        f"Goal  {goal_text}",
        "TODO  " + ("\n      " + "\n      ".join(todo_lines) if todo_lines else "无"),
        f"工作目录  {snapshot.get('workdir') or '未提供'}",
        f"任务 ID  {snapshot.get('task_id') or '无'}",
        f"工作树  {snapshot.get('worktree') or '无'}",
        "子 agent  " + ("\n         " + "\n         ".join(child_lines) if child_lines else "无"),
        f"模型  {snapshot.get('model') or '未提供'}",
        f"请求  {usage.get('model_calls', 0)} · 输入 {usage.get('input_tokens', 0)} · 输出 {usage.get('output_tokens', 0)}",
    ]
    return "\n".join(lines)


def session_status_label(snapshot):
    if snapshot.get("closing"):
        return "关闭中"
    if snapshot.get("busy"):
        return "处理中"
    return "就绪"
