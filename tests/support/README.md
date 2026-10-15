# support：测试共用设施

这里保存测试用的假模型、事件等待和真实 API 运行时辅助函数。它们不属于生产代码。

| 文件 | 内容 |
| --- | --- |
| [fakes.py](fakes.py) | 假响应、工具调用、阻塞 client 和模拟流 |
| [helpers.py](helpers.py) | 提交回合、等待事件、读取审批和会话快照 |
| [live.py](live.py) | 真实 API 测试的提交、审批和运行时关闭辅助函数 |

优先复用这里的夹具，避免各测试重复实现事件轮询或模型响应构造。测试隔离和工厂 fixture 在 [../conftest.py](../conftest.py)。
