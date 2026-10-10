#!/usr/bin/env python3
"""离线交互调试：真实 CLI 和主循环，模型返回固定响应。

输入“你好”观察普通回答；输入“写文件”观察工具回环；输入 q 退出。
运行时文件和会话记录放在临时目录，正常退出后清理。
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from debug_pass import FakeClient, FakeMessages, text_response, tool_response


class ChatMessages(FakeMessages):
    def create(self, **kwargs):
        """根据本轮输入生成脚本响应，并保留请求快照供断点查看。"""
        # 工具结果也是 user 消息；遇到结果时回答完成，不再重复请求工具。
        content = kwargs["messages"][-1]["content"]
        if isinstance(content, list) and any(block.get("type") == "tool_result" for block in content):
            response = text_response("工具结果已收到，请查看上面的 write_file 返回内容。")
        elif content == "写文件":
            response = tool_response("write_file", {"path": "hello.txt", "content": "你好，离线调试！\n"},
                                     tool_use_id=f"tool-{len(self._outer.calls) + 1}")
        else:
            response = text_response("你好！这是模拟模型回复，可以继续输入“写文件”观察工具调用。")
        self._outer.responses.append(response)
        # 历史随后会被追加；复制请求，避免看到已被后续轮次修改的值。
        snapshot = deepcopy(kwargs)
        return super().create(**snapshot)

    @contextmanager
    def stream(self, **kwargs):
        """提供生产流式消费器需要的事件，复用完整消息校验与展示路径。"""
        response = self.create(**kwargs)
        events = []
        for index, block in enumerate(response.content):
            events.append(SimpleNamespace(type="content_block_start", index=index, content_block=block))
            if block.type == "text":
                # 按固定短块输出；这是模拟增量，不代表真实 API 的分块或速度。
                for offset in range(0, len(block.text), 8):
                    delta = SimpleNamespace(type="text_delta", text=block.text[offset:offset + 8])
                    events.append(SimpleNamespace(type="content_block_delta", index=index, delta=delta))
            else:
                delta = SimpleNamespace(type="input_json_delta", partial_json=json.dumps(block.input, ensure_ascii=False))
                events.append(SimpleNamespace(type="content_block_delta", index=index, delta=delta))
            events.append(SimpleNamespace(type="content_block_stop", index=index))
        events.append(SimpleNamespace(type="message_stop"))

        class Stream:
            def __iter__(self):
                return iter(events)

            def get_final_message(self):
                return response

        yield Stream()


def main() -> None:
    # 将项目上层的src目录加入Python模块搜索路径，这样才能import agent_harness下的代码
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    # 保存脚本启动时的原始工作目录，最后需要切回去恢复环境
    original_cwd = Path.cwd()
    # 创建临时文件夹，前缀为agent-harness-chat-，退出with块会自动删除该目录
    with tempfile.TemporaryDirectory(prefix="agent-harness-chat-") as directory:
        # 先切换到临时目录，再导入core模块
        # 目的：让agent所有读写、会话等操作全部落在临时目录，不污染本机真实文件
        os.chdir(directory)
        try:
            # 延迟导入：必须在切换目录之后才导入core，core内部会读取当前工作目录
            from agent_harness import core
            from agent_harness.frontends.cli import CLIAdapter
            from agent_harness.runtime.interactive_runtime import InteractiveRuntime
            from agent_harness.runtime.session_store import SessionStore

            # 构造一个假的模型客户端，用于离线模拟，不调用真实大模型API
            client = FakeClient([])
            # 初始化消息容器，挂载到fake客户端上，管理对话消息
            client.messages = ChatMessages(client)
            # 把伪造客户端注入到core全局变量，替换真实模型客户端
            core.client = client
            # 关闭记忆模型，本次测试只观测主Agent模型逻辑，不额外调用记忆模块
            core.MEMORY_ENABLED = False

            # 创建会话存储实例，会话数据存放位置绑定core的工作目录
            store = SessionStore(core.WORKDIR)
            # 将会话存储目录重定向到临时目录下的sessions文件夹
            # 避免会话持久化写入你电脑真实的用户会话目录
            store.directory = core.WORKDIR / "sessions"

            # 控制台打印提示信息
            print(f"离线模拟交互；工作目录：{core.WORKDIR}")
            print("输入：你好 / 写文件；q 退出。回复固定，文件工具真实执行。")

            # 实例化交互式运行时（绑定会话存储），再套一层CLI命令行适配器，启动交互循环
            CLIAdapter(InteractiveRuntime(store=store)).run()
        finally:
            # 无论上面代码正常结束还是异常崩溃，最终都会执行这里
            # 切换回脚本最开始的原始工作目录，防止目录状态残留
            os.chdir(original_cwd)



if __name__ == "__main__":
    main()
