"""
子 agent 模式：用 Task 工具隔离上下文。

核心思路：给子 agent 独立上下文，避免探索细节填满主对话。
"""

import time
import sys

# 假设 client、MODEL 和 execute_tool 在其他位置已定义


# =============================================================================
# Agent 类型注册表
# =============================================================================

AGENT_TYPES = {
    # explore：只读，用于搜索和分析
    "explore": {
        "description": "Read-only agent for exploring code, finding files, searching",
        "tools": ["bash", "read_file"],  # 没有写入权限
        "prompt": "You are an exploration agent. Search and analyze, but NEVER modify files. Return a concise summary of what you found.",
    },

    # code：具备完整能力，用于实现代码
    "code": {
        "description": "Full agent for implementing features and fixing bugs",
        "tools": "*",  # 所有工具
        "prompt": "You are a coding agent. Implement the requested changes efficiently. Return a summary of what you changed.",
    },

    # plan：只读，用于设计
    "plan": {
        "description": "Planning agent for designing implementation strategies",
        "tools": ["bash", "read_file"],  # 只读
        "prompt": "You are a planning agent. Analyze the codebase and output a numbered implementation plan. Do NOT make any changes.",
    },

    # 可在这里添加自定义类型
    # "test": {
    #     "description": "运行和分析测试的 agent",
    #     "tools": ["bash", "read_file"],
    #     "prompt": "运行测试并汇报结果，不要修改代码。",
    # },
}


def get_agent_descriptions() -> str:
    """生成 Task 工具 schema 使用的描述。"""
    return "\n".join(
        f"- {name}: {cfg['description']}"
        for name, cfg in AGENT_TYPES.items()
    )


def get_tools_for_agent(agent_type: str, base_tools: list) -> list:
    """
    按 agent 类型筛选工具。

    '*' 表示全部基础工具；否则只允许指定名称的工具。

    注意：子 agent 不获得 Task 工具，以防无限递归。
    """
    allowed = AGENT_TYPES.get(agent_type, {}).get("tools", "*")

    if allowed == "*":
        return base_tools  # 所有基础工具，但不包含 Task

    return [t for t in base_tools if t["name"] in allowed]


# =============================================================================
# Task 工具定义
# =============================================================================

TASK_TOOL = {
    "name": "Task",
    "description": f"""Spawn a subagent for a focused subtask.

Subagents run in ISOLATED context - they don't see parent's history.
Use this to keep the main conversation clean.

Agent types:
{get_agent_descriptions()}

Example uses:
- Task(explore): "Find all files using the auth module"
- Task(plan): "Design a migration strategy for the database"
- Task(code): "Implement the user registration form"
""",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short task name (3-5 words) for progress display"
            },
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the subagent"
            },
            "agent_type": {
                "type": "string",
                "enum": list(AGENT_TYPES.keys()),
                "description": "Type of agent to spawn"
            },
        },
        "required": ["description", "prompt", "agent_type"],
    },
}


# =============================================================================
# 子 agent 执行
# =============================================================================

def run_task(description: str, prompt: str, agent_type: str,
             client, model: str, workdir, base_tools: list, execute_tool) -> str:
    """
    在隔离上下文中执行子 agent 任务。

    关键点：
    1. 独立历史：子 agent 从空历史开始，不继承父级上下文
    2. 工具筛选：按 agent 类型限制权限
    3. 专属提示词：定义该类型的行为
    4. 只返回摘要：父级只看到最终结果

    参数：
        description: 用于进度显示的简短名称
        prompt: 给子 agent 的详细指令
        agent_type: AGENT_TYPES 中的键
        client: Anthropic 客户端
        model: 使用的模型
        workdir: 工作目录
        base_tools: 工具定义列表
        execute_tool: 执行工具的函数

    返回：
        子 agent 的最终文本
    """
    if agent_type not in AGENT_TYPES:
        return f"Error: Unknown agent type '{agent_type}'"

    config = AGENT_TYPES[agent_type]

    # Agent 专属系统提示词
    sub_system = f"""You are a {agent_type} subagent at {workdir}.

{config["prompt"]}

Complete the task and return a clear, concise summary."""

    # 该 agent 类型允许的工具
    sub_tools = get_tools_for_agent(agent_type, base_tools)

    # 关键：消息历史隔离，子 agent 看不到父级对话
    sub_messages = [{"role": "user", "content": prompt}]

    # 进度显示
    print(f"  [{agent_type}] {description}")
    start = time.time()
    tool_count = 0

    # 复用同一个 agent 循环，但不显示中间消息
    while True:
        response = client.messages.create(
            model=model,
            system=sub_system,
            messages=sub_messages,
            tools=sub_tools,
            max_tokens=8000,
        )

        # 检查是否完成
        if response.stop_reason != "tool_use":
            break

        # 执行工具
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        results = []

        for tc in tool_calls:
            tool_count += 1
            output = execute_tool(tc.name, tc.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": output
            })

            # 在同一行刷新进度
            elapsed = time.time() - start
            sys.stdout.write(
                f"\r  [{agent_type}] {description} ... {tool_count} tools, {elapsed:.1f}s"
            )
            sys.stdout.flush()

        sub_messages.append({"role": "assistant", "content": response.content})
        sub_messages.append({"role": "user", "content": results})

    # 更新最终进度
    elapsed = time.time() - start
    sys.stdout.write(
        f"\r  [{agent_type}] {description} - done ({tool_count} tools, {elapsed:.1f}s)\n"
    )

    # 只提取并返回最终文本，父级 agent 只看到简洁摘要
    for block in response.content:
        if hasattr(block, "text"):
            return block.text

    return "(subagent returned no text)"


# =============================================================================
# 用法示例
# =============================================================================

"""
# 在主 agent 的 execute_tool 函数中：

def execute_tool(name: str, args: dict) -> str:
    if name == "Task":
        return run_task(
            description=args["description"],
            prompt=args["prompt"],
            agent_type=args["agent_type"],
            client=client,
            model=MODEL,
            workdir=WORKDIR,
            base_tools=BASE_TOOLS,
            execute_tool=execute_tool  # 传入自身以便递归调用
        )
    # ... 其他工具 ...


# 在 TOOLS 列表中：
TOOLS = BASE_TOOLS + [TASK_TOOL]
"""
