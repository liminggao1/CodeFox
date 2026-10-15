"""工具执行结果与模型协议之间的单一契约。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolResult:
    content: str
    status: Literal["returned", "success", "failed", "denied", "stopped", "started"] = "success"

    def __post_init__(self):
        if not isinstance(self.content, str):
            raise TypeError("Tool result content must be text")
        if self.status not in {"returned", "success", "failed", "denied", "stopped", "started"}:
            raise ValueError(f"Unknown tool result status: {self.status}")

    def model_result(self, call_id):
        result = {"type": "tool_result", "tool_use_id": call_id, "content": self.content}
        if self.status in {"failed", "denied", "stopped"}:
            result["is_error"] = True
        return result
