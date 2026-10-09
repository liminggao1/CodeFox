"""把 MCP 接入为运行时动态发现的工具。

先连接服务器，再以 mcp__server__tool 名称将其工具合并到常规工具池，
并应用 host 为每个工具设置的策略。
"""

from __future__ import annotations

import re

from . import core
from .tool_registry import ToolDefinition
from .task_worktrees import create_worktree

# -- MCP 系统 --

# MCP 工具在连接服务器后才发现，并以 mcp__server__tool 名称加入常规工具池。
class MCPClient:
    """在进程内模拟 MCP 的 tools/list 和 tools/call。"""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs: list[dict],
                 handlers: dict[str, callable]):
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        """按原始工具名称执行模拟 MCP 处理器。

        输入：tool_name 为服务器内部名称；args 为展开成关键字参数的字典。
        返回：字符串结果；未知工具或处理器异常转换成 MCP 错误文本。
        """
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


mcp_clients: dict[str, MCPClient] = {}
_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# 授权取决于 host 配置，而不是服务器返回的工具描述。

MCP_HOST_POLICY = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


def normalize_mcp_name(name: str) -> str:
    """替换不符合模型工具名称规则的字符。"""
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


def _mock_server_docs() -> MCPClient:
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search the documentation.",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]},
             "annotations": {"readOnlyHint": True}},
            {"name": "get_version",
             "description": "Get the documentation API version.",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []},
             "annotations": {"readOnlyHint": True}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy() -> MCPClient:
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment.",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]},
             "annotations": {"destructiveHint": True}},
            {"name": "status", "description": "Check deployment status.",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]},
             "annotations": {"readOnlyHint": True}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


def connect_mcp(name: str) -> str:
    """连接项目内置的模拟 MCP 服务器并登记客户端。

    输入：name 为 MOCK_SERVERS 中的服务器名称。
    返回：连接及发现工具的提示，或重复连接、未知服务器的提示。
        成功时写入 mcp_clients，下次工具池装配可见；当前实现不连接外部服务器。
    """
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        available = ", ".join(MOCK_SERVERS)
        return f"Unknown server '{name}'. Available: {available}"
    mcp_client = factory()
    mcp_clients[name] = mcp_client
    tool_names = [tool["name"] for tool in mcp_client.tools]
    core.terminal_print(f"  \033[31m[mcp] connected: {name} -> {tool_names}\033[0m")
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")


def discover_tools() -> list[ToolDefinition]:
    """将已连接 MCP 工具转换为统一注册器接收的定义。

    返回：ToolDefinition 列表，包含 mcp__ 前缀名称、schema、绑定处理器和 host 权限。
        名称过长或 schema 不是对象时抛出 ValueError；跨来源重名由注册器检查。
    """
    entries = []
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            # 名称长度上限（API 对工具名有 64 字符限制）
            if len(prefixed) > 64:
                raise ValueError(
                    f"MCP tool name is longer than 64 characters: {prefixed}"
                )
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            schema = tool_def.get("inputSchema", {})
            # schema 必须是 dict 且 type 为 object，否则拒绝注册
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")

            schema = {
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            }
            #   闭包陷阱：用默认参数绑定当前循环的 client/tool，
            #   否则所有 lambda 都会共享循环结束时最后一次的值
            handler = (
                lambda *, client=mcp_client, tool=raw_name, **kwargs:
                client.call_tool(tool, kwargs)
            )
            # 权限策略：MCP_HOST_POLICY 未配置的默认需用户确认
            policy = MCP_HOST_POLICY.get(
                (server_name, raw_name), "confirm"
            )

            entries.append(ToolDefinition(schema, handler, origin, policy))
    return entries



# -- Lead 的 worktree 工具 --

def run_create_worktree(name: str, task_id: str) -> str:
    return create_worktree(name, task_id)
