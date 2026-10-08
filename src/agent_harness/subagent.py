"""子 agent 工具：执行一次隔离且不可递归的 agent 任务。

子 agent 使用独立消息列表和受限工具池，不能继续创建 agent。
extract_text 和 has_tool_use 与主循环共用。
"""

from __future__ import annotations

from . import core
from . import prompts
from . import file_tools
from . import hooks

# -- 子 agent 工具 --

def sub_system() -> str:
    return (
        f"You are a coding subagent at {core.WORKDIR}. "
        f"Environment: {prompts.ENVIRONMENT_PROMPT}. "
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )


SUB_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"},
                                     "offset": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
]


SUB_HANDLERS = {
    "bash": file_tools.run_bash, "read_file": file_tools.run_read,
    "write_file": file_tools.run_write, "edit_file": file_tools.run_edit,
    "glob": file_tools.run_glob,
}


def extract_text(content) -> str:
    """提取 SDK 内容块中的文本。

    输入：content 为 SDK 对象列表，或可转为字符串的其他内容。
    返回：列表内文本块用换行连接并去掉首尾空白；非列表直接转为字符串。
    """
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text").strip()


def has_tool_use(content) -> bool:
    """根据实际 SDK 内容块检查是否有工具请求。

    输入：content 为响应内容块列表。
    返回：存在 type 为 tool_use 的对象时为 True，不依赖 stop_reason。
    """
    # 不只依赖 stop_reason；主循环以实际的 tool_use 块判断是否继续。
    return any(getattr(block, "type", None) == "tool_use"
               for block in content)


def spawn_subagent(description: str) -> str:
    """同步执行一次独立历史、受限工具池的子 agent 任务。

    输入：description 为任务说明。
    返回：最近一次非空助手文本，或没有总结的提示。
        最多调用模型 30 次，文件和 shell 工具经过权限 hooks，不能继续创建 agent。
        使用启动目录，不管理队友信箱和任务认领；模型请求异常继续抛出。
    """
    messages = [{"role": "user", "content": description}]
    for _ in range(30):
        response = core.client.messages.create(
            model=core.MODEL, system=sub_system(), messages=messages,
            tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if not has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = hooks.trigger_hooks("PreToolUse", block)
            if blocked:
                output = str(blocked)
            else:
                handler = SUB_HANDLERS.get(block.name)
                output = file_tools.call_tool_handler(handler, block.input, block.name)
                hooks.trigger_hooks("PostToolUse", block, output)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."
