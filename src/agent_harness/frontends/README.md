# frontends：交互入口

将终端输入转换成共享运行时请求，并将运行时事件呈现给用户。

| 文件/目录 | 职责 |
| --- | --- |
| [cli.py](cli.py) | `main` 路由 CLI、TUI 和离线工作流；`CLIAdapter` 读取输入、显示事件和收集审批答案 |
| [conversation.py](conversation.py) | 共用展示投影：记录去重、工具关联、正文确认与作废；返回独立数据，不操作终端或控件 |
| [presentation.py](presentation.py) | 两前端共用的纯参数摘要、短结果、生命周期文案和操作关联 |
| [tui/](tui/README.md) | CodeFox Textual 工作台、输入补全、事件展示及审批/会话弹窗 |

CLI 和 TUI 都调用 [InteractiveRuntime](../runtime/interactive_runtime.py)。
CLI 的输入线程是唯一标准输入读取者；输出线程只展示事件，并在展示成功后确认事件。
Textual 仅在选择 `tui` 入口时导入，普通 CLI 和离线工作流不加载它。

命令定义、会话切换、审批关联、停止和关闭规则由 [runtime/](../runtime/README.md) 管理。
前端维护选中项、焦点、滚动和弹窗等显示状态，不直接驱动模型或保存会话。

运行方法见 [开发笔记](../../../note/dev-notes.md#textual-交互界面)；包级结构见 [上级 README](../README.md)。
