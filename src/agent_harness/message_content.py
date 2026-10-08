"""Read model content without importing an agent lifecycle."""

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
