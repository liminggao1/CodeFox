#!/usr/bin/env python3
"""
最小 agent 模板：复制后按需调整。

这是最简单的可运行 agent（约 80 行），包含 3 个工具和主循环。

用法：
    1. 设置 ANTHROPIC_API_KEY 环境变量
    2. python minimal-agent.py
    3. 输入命令，按 'q' 退出
"""

from anthropic import Anthropic
from pathlib import Path
import subprocess
import os

# 配置
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = os.getenv("MODEL_NAME", "claude-sonnet-4-20250514")
WORKDIR = Path.cwd()

# 系统提示词保持简洁
SYSTEM = f"""You are a coding agent at {WORKDIR}.

Rules:
- Use tools to complete tasks
- Prefer action over explanation
- Summarize what you did when done"""

# 最小工具集，可按需扩展
TOOLS = [
    {
        "name": "bash",
        "description": "Run shell command",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read file contents",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
]


def execute_tool(name: str, args: dict) -> str:
    """执行工具并返回结果。"""
    if name == "bash":
        try:
            r = subprocess.run(
                args["command"], shell=True, cwd=WORKDIR,
                capture_output=True, text=True, errors="replace", timeout=60
            )
            return (r.stdout + r.stderr).strip() or "(empty)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout"

    if name == "read_file":
        try:
            return (WORKDIR / args["path"]).read_text(encoding="utf-8")[:50000]
        except Exception as e:
            return f"Error: {e}"

    if name == "write_file":
        try:
            p = WORKDIR / args["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Wrote {len(args['content'])} bytes to {args['path']}"
        except Exception as e:
            return f"Error: {e}"

    return f"Unknown tool: {name}"


def agent(prompt: str, history: list = None) -> str:
    """运行 agent 主循环。"""
    if history is None:
        history = []

    history.append({"role": "user", "content": prompt})

    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=history,
            tools=TOOLS,
            max_tokens=8000,
        )

        # 构造 assistant 消息
        history.append({"role": "assistant", "content": response.content})

        # 没有工具调用时返回文本
        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if hasattr(b, "text"))

        # 执行工具
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"> {block.name}: {block.input}")
                output = execute_tool(block.name, block.input)
                print(f"  {output[:100]}...")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output
                })

        history.append({"role": "user", "content": results})


if __name__ == "__main__":
    print(f"Minimal Agent - {WORKDIR}")
    print("Type 'q' to quit.\n")

    history = []
    while True:
        try:
            query = input(">> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if query in ("q", "quit", "exit", ""):
            break
        print(agent(query, history))
        print()
