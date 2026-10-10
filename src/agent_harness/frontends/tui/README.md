# tui：Textual 界面

为共享交互运行时提供终端图形界面。`frontends/cli.py` 的 `tui` 路由启动 `HarnessApp`。

| 文件 | 职责 |
| --- | --- |
| [app.py](app.py) | `HarnessApp`：页面布局、输入提交、命令候选选择、事件消费、审批呈现、会话选择与退出 |
| [widgets.py](widgets.py) | 命令候选、对话记录、流式正文、紧凑操作与独立滚动、内联审批和会话弹窗 |
| [__init__.py](__init__.py) | 标识 TUI 包 |

命令候选来自 [runtime/local_commands.py](../../runtime/local_commands.py)。
界面负责筛选和呈现候选、构造输入；命令是否允许执行及其效果由运行时决定。
事件在 UI 线程展示成功后调用 `acknowledge_event()`，随后领取可交付的审批。
界面通过通道提交审批决定，不直接执行工具。
实时和恢复都渲染后端展示记录，按 ID 去重；历史审批只读。
仅挂载最近一段记录，可加载更早记录；前端不读写会话文件。
页面只有会话区和输入区；同次模型调用按关联 ID 分组，操作状态就地更新。
摘要默认可见，详情与过程组分别限制高度；审批保持在组外。
临时正文片段按调用和文本块更新，正式消息确认同一正文；失败尝试收起并标明未采用。

共享后端是 [runtime/](../../runtime/README.md)，输入与路由边界见 [frontends/README](../README.md)。
快捷键和使用方式见 [开发笔记](../../../../note/dev-notes.md#textual-交互界面)。
