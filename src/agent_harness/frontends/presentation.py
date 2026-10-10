"""Pure summaries shared by the terminal adapters."""

import json


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
