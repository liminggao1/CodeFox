"""
工具模板：复制后按 agent 的需要调整。

每个工具需要：
1. 定义（提供给模型的 JSON schema）
2. 实现（Python 函数）
"""

from pathlib import Path
import subprocess

WORKDIR = Path.cwd()
BASH_TIMEOUT_SECONDS = 60
TOOL_OUTPUT_CHAR_LIMIT = 50000


# =============================================================================
# 工具定义（用于 TOOLS 列表）
# =============================================================================

BASH_TOOL = {
    "name": "bash",
    "description": "Run a shell command. Use for: ls, find, grep, git, npm, python, etc.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute"
            }
        },
        "required": ["command"],
    },
}

READ_FILE_TOOL = {
    "name": "read_file",
    "description": "Read file contents. Returns UTF-8 text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file"
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read (default: all)"
            },
        },
        "required": ["path"],
    },
}

WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": "Write content to a file. Creates parent directories if needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path for the file"
            },
            "content": {
                "type": "string",
                "description": "Content to write"
            },
        },
        "required": ["path", "content"],
    },
}

EDIT_FILE_TOOL = {
    "name": "edit_file",
    "description": "Replace exact text in a file. Use for surgical edits.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file"
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to find (must match precisely)"
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text"
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
}

TODO_WRITE_TOOL = {
    "name": "TodoWrite",
    "description": "Update the task list. Use to plan and track progress.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Complete list of tasks",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Task description"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        "activeForm": {"type": "string", "description": "Present tense, e.g. 'Reading files'"},
                    },
                    "required": ["content", "status", "activeForm"],
                },
            }
        },
        "required": ["items"],
    },
}

TASK_TOOL_TEMPLATE = """
# 根据 agent 类型动态生成
TASK_TOOL = {
    "name": "Task",
    "description": f"Spawn a subagent for a focused subtask.\\n\\nAgent types:\\n{get_agent_descriptions()}",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Short task name (3-5 words)"},
            "prompt": {"type": "string", "description": "Detailed instructions"},
            "agent_type": {"type": "string", "enum": list(AGENT_TYPES.keys())},
        },
        "required": ["description", "prompt", "agent_type"],
    },
}
"""


# =============================================================================
# 工具实现
# =============================================================================

def safe_path(p: str) -> Path:
    """
    确保路径留在工作区内，防止通过 ../../../etc/passwd 等路径逃逸。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """
    执行 shell 命令并进行安全检查。

    检查内容：
    - 拦截明显危险的命令
    - 超时由 BASH_TIMEOUT_SECONDS 控制
    - 输出字符上限由 TOOL_OUTPUT_CHAR_LIMIT 控制
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True, errors="replace",
            timeout=BASH_TIMEOUT_SECONDS
        )
        output = (result.stdout + result.stderr).strip()
        return output[:TOOL_OUTPUT_CHAR_LIMIT] if output else "(no output)"

    except subprocess.TimeoutExpired:
        return f"Error: Command timed out ({BASH_TIMEOUT_SECONDS:g}s)"
    except Exception as e:
        return f"Error: {e}"


def run_read_file(path: str, limit: int = None) -> str:
    """
    读取文件，可选行数限制。

    功能：
    - 安全解析路径
    - 大文件可限制读取行数
    - 输出字符上限由 TOOL_OUTPUT_CHAR_LIMIT 控制
    """
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()

        if limit and limit < len(lines):
            lines = lines[:limit]
            lines.append(f"... ({len(text.splitlines()) - limit} more lines)")

        return "\n".join(lines)[:TOOL_OUTPUT_CHAR_LIMIT]

    except Exception as e:
        return f"Error: {e}"


def run_write_file(path: str, content: str) -> str:
    """
    写入文件，必要时创建父目录。

    功能：
    - 安全解析路径
    - 自动创建父目录
    - 返回写入的字节数以便确认
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"

    except Exception as e:
        return f"Error: {e}"


def run_edit_file(path: str, old_text: str, new_text: str) -> str:
    """
    精确替换文件中的文本。

    功能：
    - 精确匹配字符串，不使用正则表达式
    - 只替换首次出现的内容
    - 找不到文本时给出明确错误
    """
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")

        if old_text not in content:
            return f"Error: Text not found in {path}"

        new_content = content.replace(old_text, new_text, 1)
        fp.write_text(new_content, encoding="utf-8")
        return f"Edited {path}"

    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# 分发模式
# =============================================================================

def execute_tool(name: str, args: dict) -> str:
    """
    将工具调用分发给对应实现。

    添加新工具的步骤：
    1. 在 TOOLS 列表中添加定义
    2. 添加实现函数
    3. 在此分发器中添加分支
    """
    if name == "bash":
        return run_bash(args["command"])
    if name == "read_file":
        return run_read_file(args["path"], args.get("limit"))
    if name == "write_file":
        return run_write_file(args["path"], args["content"])
    if name == "edit_file":
        return run_edit_file(args["path"], args["old_text"], args["new_text"])
    # 在这里继续添加工具
    return f"Unknown tool: {name}"
