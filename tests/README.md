# 测试维护

测试在 WSL2 的 `learncc` 环境中从源码运行，环境约定见 [AGENTS.md](../AGENTS.md)。

先安装开发依赖：

```sh
python -m pip install -e ".[dev]"
```

```sh
python -m pytest -q
python scripts/debug_pass.py
```

默认只收集离线测试。真实模型测试单独启用，会消耗 API 额度：

```sh
LIVE_API=1 PYTHONPATH=src python -m pytest -q tests/live
```

## 目录

| 目录 | 内容 |
| --- | --- |
| [live/](live/README.md) | 需要真实模型 API 的测试；默认不会收集 |
| [support/](support/README.md) | 离线测试和真实 API 测试共用的夹具与事件辅助函数 |

根目录下的 `test_*.py` 直接按行为和模块划分，覆盖归属见下表。

## 共用设施

- [support/fakes.py](support/fakes.py)：`response()` 构造完整响应，`usage=(input, output)` 指定用量，`usage=None` 省略用量，`text=None` 只含工具块。`blocks()` 保留指定的内容块，`tool()` 和 `tool_call()` 分别提供固定 ID 和唯一 ID 的工具调用；阻塞 client 和模拟流也在这里。
- [support/helpers.py](support/helpers.py)：提交回合、等待并确认事件、读取审批和会话快照。审批前有日志或工具事件时，使用 `displayed_approval()` 确认前置事件。
- [conftest.py](conftest.py)：隔离会话和权限设置；`host_factory` 初始化离线宿主，`runtime_factory` 启动真实共享驱动并统一关闭线程，`approval_hook` 处理单次授权。

复用这些设施，避免在各文件中重复构造模型响应或实现事件等待循环。响应协议专属的原始数据仍留在协议测试中。

## 覆盖归属

| 测试文件 | 主要行为 |
| --- | --- |
| `test_permission_rules.py` | 命令规则、工作区内外文件操作、单次授权、权限模式持久化 |
| `test_permissions.py` | 权限与 hook 的接线、主循环和子 agent 执行、停止优先级、后台调用 |
| `test_turn_engine.py` | Lead 与子 agent 共用的回合循环、请求快照、阶段和工具结果配对 |
| `test_subagents.py`、`test_subagent_session.py`、`test_subagent_lifetimes.py` | 创建与协议、执行会话、真实线程及消息生命周期 |
| `test_interactive_runtime.py` | 命令、会话切换、后台交付和共享驱动的停止行为 |
| `test_conversation.py` | 展示记录持久化、回放及前端呈现；`test_tui_integration.py` 验证真实驱动与界面的连接 |
| 其余 `test_*.py` | 对应模块的规则与失败分支，包括协议、压缩、goal、任务、调度和工作流 |

## 精简原则

- 纯校验的短输入表放在一个测试中，共用准备过程，并在断言中带上当前输入。不同副作用、失败结果或恢复阶段继续参数化，保留可单独运行的用例。
- 同一生命周期里的状态变化合并为连贯流程。合并前核对独有断言、落盘内容、错误后的状态和线程收尾，不以减少收集项为唯一目标。
- Lead、once 和 persistent 共用的执行规则集中在 `test_turn_engine.py`；各入口保留独有的接线和生命周期验证，避免重复铺开模式、工具和错误类型的组合。
- 界面测试侧重真实交互、可见状态与恢复流程。删除源码字符串扫描、固定保存次数和重复布局细节等复述实现的断言。

调整覆盖归属时，同时删除失去用途的辅助函数、导入和参数分支。用例数不设长期上限，新增测试应对应独立的行为或回归风险。
