# 开发笔记：运行与调试

> 初次记录：2026-10-04；最后核对：2026-10-10
> 定位：**本机个人笔记** —— 怎么跑、怎么调。
> 通用安装步骤见 [env-setup.md](env-setup.md)；本机环境约定（WSL / conda / 命令）见 [AGENTS.md](../AGENTS.md)；
> 对外命令清单见 [README.md](../README.md)。

## 一、怎么跑

本机在 WSL 的 conda 环境 `learncc` 里按源码运行，不依赖安装。

### 离线（不联网、不消耗额度）

```sh
R=/mnt/d/01dpan/01project/life/learncc/agent-harness
PY=/home/gaogao/miniconda3/envs/learncc/bin/python

cd $R
$PY -m pytest -q                          # 完整离线测试
PYTHONPATH=src $PY -m agent_harness workflow demo
PYTHONPATH=src $PY -m agent_harness workflow resume
$PY scripts/debug_pass.py                 # 端到端自检，9 项
```

`pytest` 和自检不需要 `PYTHONPATH`（前者由 `tests/conftest.py` 注入 `src`，后者自己插入）。

直接在脚本中调用 host 的主循环或工具池时，先显式初始化。初始化不请求模型或启动服务，也不覆盖已注入的 workflow runner；CLI/TUI 由 `InteractiveRuntime.start()` 自动调用。

```python
from agent_harness import host

host.initialize_runtime()
tools, handlers = host.assemble_tool_pool()
```

导入 host 时 `GOAL` 尚未创建。需要自定义目标评估器时先 `host.configure_goal(...)`，随后初始化会保留它；初始化失败后修正配置可重试。workflow demo 路由独立注册示例并选择离线 runner。

### 两者的分工

|      | `pytest`                                                      | `scripts/debug_pass.py`                                           |
| ---- | ------------------------------------------------------------- | ----------------------------------------------------------------- |
| 定位 | 单元与契约测试                                                | 子进程 / 真 Git / 线程集成自检                                    |
| 粒度 | 按模块拆分的测试文件                                          | 一条命令 9 项                                                     |
| 覆盖 | 任务、压缩、调度、权限、编码、技能、goal、workflow、subagents | 工具池、agent 回环、goal 边界、任务、workflow、worktree、子 agent |
| 独有 | 边界条件与错误分支                                            | 真起子进程跑 CLI、真建 Git 仓库、跨模块串联检查                   |

改完 `src/` 后两个都要跑：先 `pytest -q`，再 `debug_pass.py`。重叠的几项（工具池、agent
回环、edit 守卫、路径逃逸、goal 边界、任务生命周期）是刻意保留的重复：`pytest` 里是隔离
单测，自检里是在真实进程边界上再确认一次。

### 真实 API 交互

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
PYTHONPATH=src /home/gaogao/miniconda3/envs/learncc/bin/python -m agent_harness
```

提示符 `agent-harness >>` 下输入一句话回车发送；`/goal` 看目标状态；`/resume` 列出会话，`/resume <id>` 恢复会话，`/delete <id>` 删除非活动会话；`q` 退出。**每轮都消耗额度。**

首次有效交互会在 WSL 用户目录 `~/.agent-harness/sessions/` 创建一个上下文文件；退出或 Ctrl+C 不删除它。
`/resume <id>` 可从任意目录恢复历史、goal、Lead 活动任务、工作树和 TODO，并自动回到原启动目录。任务完成后继续使用原工作树。
恢复前提、会话隔离和存储规则统一见 [README 的交互会话说明](../README.md#行为要点)。

装过包（`pip install -e .`）时可省掉 `PYTHONPATH`，并用命令 `agent-harness` 代替 `python -m agent_harness`。
本机**未安装**，走的是上面这条。

### Textual 交互界面

本机已在 WSL 的 `learncc` 中安装 Textual 8.2.8。先打开 WSL 终端，然后执行：

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
PYTHONPATH=src /home/gaogao/miniconda3/envs/learncc/bin/python -m agent_harness tui
```

界面仍使用项目 `.env` 中的真实模型配置，会消耗 API 额度。请在要工作的目录启动；
任务和其他运行时文件跟随该目录，会话文件位置见上方「真实 API 交互」。同一目录只运行一个交互进程。

换环境时，在 WSL 的目标环境安装可选依赖即可；按源码运行无需安装项目包：

```sh
/home/gaogao/miniconda3/envs/learncc/bin/python -m pip install 'textual>=8.2.8,<9'
```

**对话与布局：**只有会话区与底部输入区，没有顶部状态栏、右栏和固定底栏。
输入接受后立即显示消息与处理提示。模型等待、实际 hook、工具、TODO、后台进度、审批和结束摘要进入同一会话。
Lead 正文在文字片段到达时增量显示；子 agent 与记忆、目标等内部调用仍使用完整响应。
工具名称、关键参数、状态和短结果默认可见，点击「详情」展开完整参数与输出。
同次模型调用的工具与 hook 归在同一过程组，内容过多时在组内滚动；全文展开区也有独立滚动条。
鼠标滚轮先移动所在的内部区域，到边界后继续移动外层会话。审批按钮位于组外的内联条目。
恢复先挂载最近一段记录，点击「加载更早记录」继续查看；分组使用完整关联，显示上限不删除保存记录。
点击会话区后用 Home/End、方向键、PageUp/PageDown 或鼠标滚轮查看历史。
主动上翻时新记录不强制滚到底部；首次有效审批会滚到可操作的位置。

输入框支持中文和多行粘贴。普通消息 Enter 换行，点击「发送」或按 Ctrl+Enter 提交。
部分终端不能区分 Ctrl+Enter，此时使用发送按钮。空白提交会忽略。
运行期间只保留当前草稿，不排队第二条请求；忙碌时提交被拒绝，草稿保留。

**命令补全：**在单行开头输入 `/`，输入框上方会显示可用命令、中文说明和用法。
继续输入前缀过滤，↑/↓ 循环选择，Enter 执行，Tab 只填入命令名，Esc 隐藏菜单并保留草稿。
下一次编辑文本会重新显示候选。点击候选与 Enter 相同。完整命令及参数可直接按 Enter 提交。
`/delete` 缺少会话 ID 时只填入并提示，不提交；填写 ID 后再按 Enter。
未知命令按 Enter 提示，Ctrl+Enter 或发送按钮可提交 slash 开头的原文，不会替你选候选。
多行内容、选区、光标离开末尾、执行中和弹窗期间不接管编辑键。
高度小于 16 行时缩小输入区并隐藏发送/停止按钮，使用快捷键；需要按钮时增大终端高度。

| 操作 | 快捷键 / 按钮 | 行为 |
| --- | --- | --- |
| 发送 | Ctrl+Enter / 发送 | 提交消息或本地命令，接受后清空输入 |
| 命令选择 | ↑/↓、Enter、Tab、Esc / 点击候选 | 导航、执行、仅补全、隐藏；普通消息 Enter 仍换行 |
| 清空草稿 | Ctrl+L | 只清空输入框 |
| 会话列表 | Ctrl+R | 空闲时打开会话列表 |
| 停止回合 | Ctrl+C / 停止 | 请求停止当前回合，保留已有结果和输入草稿 |
| 退出 | Ctrl+D | 请求关闭，等待保存与资源清理后退出 |

**审批：**Bash 和策略要求确认的 MCP 工具在主对话显示来源、工具名和可展开的完整参数。
按钮启用后选择「批准」或「拒绝」；Ctrl+C 停止回合，Ctrl+D 退出，两者都会解除审批等待。

**流式与恢复：**断流、停止或重试的临时正文会标明「本次输出未采用」，不会存为正式回复。
停止在下一网络读取/处理安全点生效，仍受请求超时限制。停止后不会自动重新请求模型。
真实工具结果逐项显示与保存，供模型的结果消息仍整批追加；中断恢复使用已保存结果，只为缺失项补中断信息。
后台事件即时显示，到下一安全检查点保存；显示完成不等于已经落盘。CLI 使用同一摘要，失败尝试以追加提示区分。
前台 Lead 及其同步 once 子 Agent 可以申请确认；后台 Lead、persistent 子 Agent、workflow 和
无交互上下文的调用会直接拒绝需要确认的工具。文件路径、拒绝列表和工具白名单仍先行检查。

**目标与会话：**下列命令在 CLI/TUI 中由同一个运行时处理，忙碌时拒绝新命令。

| 命令 | 行为 |
| --- | --- |
| `/goal` | 查看目标状态，不调用主模型 |
| `/goal <condition>` | 设置目标，并把条件交给模型执行 |
| `/goal clear` | 清除目标 |
| `/resume` | 列出会话；TUI 中弹出列表，选中会话并确认即可恢复 |
| `/resume <id>` | 恢复指定会话的上下文，包括 goal、任务、工作目录和 TODO |
| `/delete <id>` | 删除非活动会话；列表也提供删除按钮 |
| `/quit` | 空闲时请求退出；执行期间使用 Ctrl+D |

会话保存位置和恢复限制见上方「真实 API 交互」。恢复会重建会话记录，并重置当前激活期间的 Lead 用量。
历史审批只读，工具结果未记录时提示核对当前状态；恢复不会重执行工具或启动旧后台线程。
历史回合摘要按保存内容展示，模型压缩不删除展示历史。
用量只统计当前激活期间 Lead 主模型的输入/输出 token 和请求数；不含 goal、memory、压缩、
子 Agent 或 workflow 的调用，也不从历史恢复累计用量。

**停止与退出：**停止是合作式请求。等待审批会立即解除；已开始的同步模型、重试、工具、
goal 评估或记忆抽取需要先返回，再到检查点停止。已完成的文件操作和记忆写入保留。
它不递归取消独立子 Agent 或 workflow。停止后暂停后台结果自动唤醒 Lead，下一次显式提交恢复。
子 Agent 状态仍会进入会话记录。主模型报上下文过长错误时，已请求停止不会再启动恢复摘要；
原始模型异常仍报告失败。cron 通知属于当前运行实例，恢复会话后仍显示新的调度错误。
Ctrl+D 在会话中显示「正在关闭」，拒绝新输入，停止 cron，清理 shell，等待所属后台线程退出并保存会话；
关闭期间界面继续响应。保存失败会报告错误，TUI 退出码为 1。

**离线验证：**下面使用 fake client 和 Textual 无头测试，不联网；实际 TUI 启动仍使用真实 API。

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
/home/gaogao/miniconda3/envs/learncc/bin/python -m pytest -q tests/test_interaction.py tests/test_interactive_runtime.py tests/test_tui_integration.py tests/test_conversation_records.py tests/test_conversation_frontends.py
```

实现与验收记录见 [Textual 实施计划](../docs/plans/2026-10-09-02Textual交互界面实施计划.md)。
统一对话与历史展示见 [统一对话展示实施计划](../docs/plans/2026-10-09-05统一对话展示与事件恢复实施计划.md)。
当前已完成离线集成验证及 Linux PTY 的中文多行粘贴、发送和退出验证；真实 API 的 TUI 交互尚未实测。

## 二、离线自检：改完先跑这个

[scripts/debug_pass.py](../scripts/debug_pass.py) 一条命令覆盖主要机制，全离线、不写仓库
（状态都建在临时目录）。**退出码 = 失败数**，0 表示全通过。

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
/home/gaogao/miniconda3/envs/learncc/bin/python scripts/debug_pass.py
```

### 覆盖范围

每块独立，一块失败不影响其余，最后打印报告。

| #   | 检查                   | 断言要点                                                                            |
| --- | ---------------------- | ----------------------------------------------------------------------------------- |
| 1   | 工具池装配             | 27 个工具、无重名；除 compact 外都有 handler                                        |
| 2   | agent 循环工具往返     | 脚本化 client 驱动 `agent_loop`，`write_file` 真实落盘，恰好 2 次模型调用           |
| 3   | `edit_file` 误匹配防护 | `old_text` 不匹配时拒绝改写且文件不变                                               |
| 4   | 路径逃逸防护           | 读 `../../../etc/passwd` 被拒                                                       |
| 5   | goal Stop 边界         | 评估器先 `block` 再放行：注入 1 条 `[Goal still active]`，循环继续，成功后清空 goal |
| 6   | 任务生命周期           | create → claim → complete → release                                                 |
| 7   | workflow 离线          | `demo` 完成，`resume` 命中 journal（agents=0 tokens=0），`.runtime/` 落在启动目录   |
| 8   | git worktree           | 真实临时仓库上 create → claim → 提交 → complete → remove                            |
| 9   | 统一子 agent           | 实际创建工具覆盖 once 结果与 persistent 启动、文件、事件及关闭                      |

### 实测输出

```
[PASS] tool pool assembled :: 27 tools (26 handlers + ['compact'])
[PASS] agent loop: tool call round-trip :: tool round-trip, 2 model calls
[PASS] edit_file: mismatch guarded :: mismatch refused, then applied
[PASS] read_file: path escape blocked :: path outside workdir blocked
[PASS] goal: stop boundary blocks once :: block -> continue -> allow -> cleared
[PASS] task lifecycle :: create/claim/complete/release
[PASS] unified subagent lifetimes :: once result; persistent start/write/events/shutdown
[PASS] workflow demo/resume offline :: demo/resume; unified child write and cache replay
[PASS] worktree git ops :: create/claim/commit/complete/remove
9/9 passed
```

### 扩展方式

往 `main()` 里加一个 `check("<名字>", fn)`：`fn` 正常返回 → PASS 并打印返回值；抛异常 → FAIL
并记录异常类型与消息。

需要干净工作目录时用脚本已建好的 `workdir`；需要独立 `cwd` 的场景（worktree 那类）照
`--worktree-child` 的模式开子进程。

## 三、VS Code 断点调试

### 为什么必须用 Remote-WSL 窗口

文件在 D:（Windows 盘），但**执行和调试要在 WSL**：

- Windows 侧 Python 里没有项目依赖（`anthropic` / `python-dotenv` / `pyyaml` 只装在 `learncc`），
  在 Windows 窗口按 F5 会直接 import 失败。
- Remote-WSL 打开的是 WSL 路径 `/mnt/d/...`，改的就是 D: 上同一个文件，编辑体验不变；
  源码路径两侧一致，断点自动对上，**不需要 pathMappings**。

### 步骤

1. **用 WSL 打开目录**。命令面板（`Ctrl+Shift+P`）→ **WSL: Connect to WSL using Distro…**
   → `Ubuntu-22.04` → 打开 `/mnt/d/01dpan/01project/life/learncc/agent-harness`。
   或 WSL 终端里：
   ```sh
   code --remote wsl+Ubuntu-22.04 /mnt/d/01dpan/01project/life/learncc/agent-harness
   ```
   **要打开 `/mnt/d/...`，不要开 `D:\...`**；两种窗口外观很像，靠左下角区分。
2. **确认左下角**显示 `WSL: Ubuntu-22.04`。
3. **确认解释器**为 `/home/gaogao/miniconda3/envs/learncc/bin/python`
   （`.vscode/settings.json` 设为默认，`.vscode/launch.json` 的五个调试配置也显式使用该解释器，不会用 base）。
4. **按 F5**，选「Agent 交互（真实 API）」。断点直接打在 `src/agent_harness/` 的代码行上。

### 已准备的调试配置

`.vscode/launch.json`：

| 配置名                           | 用途                                        |
| -------------------------------- | ------------------------------------------- |
| Agent 交互（真实 API）           | 主路径。集成终端可输入                      |
| Agent 交互（外部终端，输入更稳） | 同上，改用外部终端；输入/方向键异常时用这个 |
| Workflow demo（离线，不联网）    | 调试统一子 agent 工作流，fake client，不花额度 |
| 端到端自检（离线）               | 调试 `scripts/debug_pass.py`                |
| pytest（当前文件）               | 在当前打开的测试文件上起断点                |

下面是当前 [`.vscode/launch.json`](../.vscode/launch.json) 的完整基础配置。后面的 15 个观察点都复用这些配置，只需按说明选择入口或替换测试路径。

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Agent 交互（真实 API）",
      "type": "debugpy",
      "request": "launch",
      "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
      "module": "agent_harness",
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "justMyCode": true,
      "pythonArgs": ["-Xfrozen_modules=off"],
      "env": {
        "PYTHONPATH": "${workspaceFolder}/src"
      }
    },
    {
      "name": "Agent 交互（外部终端，输入更稳）",
      "type": "debugpy",
      "request": "launch",
      "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
      "module": "agent_harness",
      "cwd": "${workspaceFolder}",
      "console": "externalTerminal",
      "justMyCode": true,
      "pythonArgs": ["-Xfrozen_modules=off"],
      "env": {
        "PYTHONPATH": "${workspaceFolder}/src"
      }
    },
    {
      "name": "Workflow demo（离线，不联网）",
      "type": "debugpy",
      "request": "launch",
      "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
      "module": "agent_harness",
      "args": ["workflow", "demo"],
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "justMyCode": true,
      "pythonArgs": ["-Xfrozen_modules=off"],
      "env": {
        "PYTHONPATH": "${workspaceFolder}/src"
      }
    },
    {
      "name": "端到端自检（离线）",
      "type": "debugpy",
      "request": "launch",
      "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
      "program": "${workspaceFolder}/scripts/debug_pass.py",
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "justMyCode": true,
      "pythonArgs": ["-Xfrozen_modules=off"]
    },
    {
      "name": "pytest（当前文件）",
      "type": "debugpy",
      "request": "launch",
      "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
      "module": "pytest",
      "args": ["-q", "${file}"],
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "pythonArgs": ["-Xfrozen_modules=off"]
    }
  ]
}
```

### 按观察点调试

先点击函数链接下断点，再选择上面的现成配置。源码链接采用相对路径和 `#L行号`，以当前函数与调用位置为准。凡是下面写了测试路径的，在
[.vscode/launch.json](../.vscode/launch.json) 里找到「pytest（当前文件）」，仅把
`"args": ["-q", "${file}"]` 中的 `"${file}"` 换成给出的路径；其他字段不用改。
写了两个测试路径的，分别替换并运行一次。配置中的解释器已固定为 WSL 的 `learncc`。

表中的“调用身份”区分函数在这条路径中的用途；“同文件 / 跨文件”指调用位置与实现位置，
不表示 Python 强制限制了访问范围。LLM 请求的是工具名，Python 运行时再执行对应函数。

- **工具处理器**：已登记到工具池的入口，例如 `write_file` 对应 `bind_file_tools` 返回的 `write` 闭包。
- **工具底层**：由处理器继续调用的业务函数，LLM 不直接按这个 Python 函数名请求。
- **内部 / 局部函数**：由程序直接调用；局部函数定义在另一个函数里面。
- **回调 / 线程入口 / CLI 入口**：分别由注册器、hooks、线程或命令行流程触发。
- **共享数据**：程序读取的状态，不是函数或模型工具。

基础文件函数由 Lead 和子 agent 各自的目录处理器调用；两种子 agent 模式共用同一 session。

#### 1. 一轮对话的完整流程

入口是 `agent_loop`，实现位于 [host.py:314](../src/agent_harness/host.py#L314)。
它由共享驱动执行用户回合或后台唤醒回合；本节表中的辅助函数由程序驱动，不是提供给 LLM 的工具入口。
主循环只编排上下文、模型恢复、goal 和工具迭代。
本轮消息记录、模型调用关联、单项结果与检查点见 [LeadTurn](../src/agent_harness/agents/lead_turn.py#L13)；
工具批次见 [execute_tool_batch](../src/agent_harness/agents/lead_tools.py#L10)。
完成、失败、停止与截断统一经过 [LeadTurn.finish](../src/agent_harness/agents/lead_turn.py#L117)，由它委托 scheduler 和 tasks 收尾。
选择「端到端自检（离线）」，在 [host.py:355](../src/agent_harness/host.py#L355) 下断点，按下面的主要调用顺序单步查看。

| 顺序 | 函数                 | 调用身份    | 调用 / 触发位置                                  | 实现位置                                                             | 作用                                                                               |
| ---- | -------------------- | ----------- | ------------------------------------------------ | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1    | `prepare_history`    | 内部·跨文件 | [host.py:355](../src/agent_harness/host.py#L355) | [compaction.py:44](../src/agent_harness/context/compaction.py#L44)           | 按预算整理或压缩历史                                                               |
| 2    | `build_context`      | 内部·跨文件 | [host.py:363](../src/agent_harness/host.py#L363) | [runtime_context.py:31](../src/agent_harness/runtime_context.py#L31) | 刷新记忆、MCP 和子 agent 信息，见第 2 节                                           |
| 3    | `assemble_tool_pool` | 内部·同文件 | [host.py:365](../src/agent_harness/host.py#L365) | [host.py:102](../src/agent_harness/host.py#L102)                     | 装配本轮工具；内部调用注册器 [snapshot](../src/agent_harness/tools/tool_registry.py#L36) |
| 4    | `call_llm`           | 内部·同文件 | [host.py:374](../src/agent_harness/host.py#L374) | [host.py:222](../src/agent_harness/host.py#L222)                     | 拼接系统提示词并请求模型                                                           |
| 5    | `has_tool_use`       | 内部·跨文件 | [host.py:425](../src/agent_harness/host.py#L425) | [message_content.py:17](../src/agent_harness/providers/message_content.py#L17) | 判断模型是否请求工具，决定进入下面哪个分支                                         |

**有普通同步工具请求时：**

| 顺序 | 函数 | 调用身份 | 调用 / 触发位置 | 实现位置 | 作用 |
| --- | --- | --- | --- | --- | --- |
| 6 | `check_tool_use` | 内部·跨文件 | [lead_tools.py:58](../src/agent_harness/agents/lead_tools.py#L58) | [hooks.py:61](../src/agent_harness/runtime/hooks.py#L61) | 执行授权与 PreToolUse，并检查停止请求 |
| 7 | `execute_tool` | 内部·跨文件 | [lead_tools.py:90](../src/agent_harness/agents/lead_tools.py#L90) | [hooks.py:76](../src/agent_harness/runtime/hooks.py#L76) | 调用 handler，再执行 PostToolUse |
| 8 | `build_user_content` | 内部·同文件 | [host.py:471](../src/agent_harness/host.py#L471) | [host.py:180](../src/agent_harness/host.py#L180) | 把本批工具结果和后台通知写回模型历史 |
| 9 | `apply_after_results` | 内部·跨文件 | [host.py:475](../src/agent_harness/host.py#L475) | [compaction.py:654](../src/agent_harness/context/compaction.py#L654) | 有手动压缩请求时执行压缩 |

然后回到循环顶部，再次请求模型。自检会演示：`请求写文件 → 执行 write_file → 回填结果 → 模型回答`。

**没有工具请求时：**

| 顺序 | 函数                           | 调用身份    | 调用 / 触发位置                                  | 实现位置                                           | 作用                                   |
| ---- | ------------------------------ | ----------- | ------------------------------------------------ | -------------------------------------------------- | -------------------------------------- |
| 6    | `trigger_hooks`                | 内部·跨文件 | [host.py:429](../src/agent_harness/host.py#L429) | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31)   | 触发 `Stop`                            |
| 7    | `evaluate_goal`                | 内部·同文件 | [host.py:433](../src/agent_harness/host.py#L433) | [host.py:269](../src/agent_harness/host.py#L269)   | 检查目标；判定为 `block` 就继续循环    |
| 8    | `remember_after_turn`          | 内部·同文件 | [host.py:460](../src/agent_harness/host.py#L460) | [host.py:156](../src/agent_harness/host.py#L156)   | 提取并整理记忆                         |
| 9    | `release_completed_assignment` | 内部·跨文件 | [lead_turn.py:129](../src/agent_harness/agents/lead_turn.py#L129) | [tasks.py:570](../src/agent_harness/workspace/tasks.py#L570) | 释放已完成任务的分配记录，随后结束本轮 |

#### 2. 上下文如何刷新

主循环在 [host.py:363](../src/agent_harness/host.py#L363) 调用 `build_context`，
实现位于 [runtime_context.py:31](../src/agent_harness/runtime_context.py#L31)。
进入函数后，按下面的顺序查看：

| 顺序 | 函数或数据                  | 调用身份    | 使用位置                                                             | 实现或定义位置                                                       | 作用                          |
| ---- | --------------------------- | ----------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------- |
| 1    | `subagent.active_subagents` | 共享数据    | [runtime_context.py:39](../src/agent_harness/runtime_context.py#L39) | [subagent.py:126](../src/agent_harness/agents/subagent.py#L126)             | 加锁读取活跃子 agent 名称     |
| 2    | `read_memory_index`         | 内部·跨文件 | [runtime_context.py:41](../src/agent_harness/runtime_context.py#L41) | [memory_runtime.py:197](../src/agent_harness/context/memory_runtime.py#L197) | 读取记忆索引                  |
| 3    | `load_memories`             | 内部·跨文件 | [runtime_context.py:42](../src/agent_harness/runtime_context.py#L42) | [memory_runtime.py:371](../src/agent_harness/context/memory_runtime.py#L371) | 加载相关记忆，具体选取流程见第 14 节 |
| 4    | `mcp_tools.mcp_clients`     | 共享数据    | [runtime_context.py:43](../src/agent_harness/runtime_context.py#L43) | [mcp_tools.py:54](../src/agent_harness/tools/mcp_tools.py#L54)             | 读取已登记的 MCP 名称         |

上下文构造完成后，主循环进入 `call_llm`，再按下面的顺序使用它：

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                  | 实现位置                                                             | 作用                                      |
| ---- | ------------------------ | ----------- | ------------------------------------------------ | -------------------------------------------------------------------- | ----------------------------------------- |
| 5    | `prompt_values`          | 内部·跨文件 | [host.py:235](../src/agent_harness/host.py#L235) | [runtime_context.py:17](../src/agent_harness/runtime_context.py#L17) | 将上下文字段交给提示词组装函数            |
| 6    | `assemble_system_prompt` | 内部·跨文件 | [host.py:235](../src/agent_harness/host.py#L235) | [prompts.py:73](../src/agent_harness/context/prompts.py#L73)                 | 把记忆、MCP 和子 agent 信息拼进系统提示词 |

每次循环都会重新构造上下文。上一轮新增的 MCP 或子 agent，会在下一次构造时读到。
要观察刷新前后的区别，选择「pytest（当前文件）」，将 `"${file}"` 替换为 `"tests/test_runtime_context.py"`。

#### 3. goal 的 Stop 判定

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_goal.py::test_unmet_goal_continues_automatically_until_achieved"`。
测试会替换评估器；下面后两步是实际评估器的路径。
这是主循环在停止边界自动执行的内部评估。评估器会请求模型，但自身未登记为模型工具。

| 顺序 | 函数                                 | 调用身份        | 调用 / 触发位置                                                    | 实现位置                                         | 作用                           |
| ---- | ------------------------------------ | --------------- | ------------------------------------------------------------------ | ------------------------------------------------ | ------------------------------ |
| 1    | `evaluate_goal`                      | 内部·同文件     | [host.py:433](../src/agent_harness/host.py#L433)                   | [host.py:269](../src/agent_harness/host.py#L269) | 主模型不再请求工具时检查目标   |
| 2    | `GoalController.evaluate_after_turn` | 内部·跨文件     | [host.py:283](../src/agent_harness/host.py#L283)                   | [goal.py:358](../src/agent_harness/agents/goal.py#L358) | 根据活动目标决定是否评估       |
| 3    | `PromptGoalEvaluator.evaluate`       | 内部·同文件     | [goal.py:386](../src/agent_harness/agents/goal.py#L386)                   | [goal.py:186](../src/agent_harness/agents/goal.py#L186) | 启动独立目标评估               |
| 4    | `_evaluate_sync`                     | 线程任务·同文件 | [goal.py:201](../src/agent_harness/agents/goal.py#L201)（to_thread 目标） | [goal.py:204](../src/agent_harness/agents/goal.py#L204) | 在线程中请求评估模型并解析判断 |

判定为 `block` 时，主循环在 [host.py:448](../src/agent_harness/host.py#L448) 追加继续提示；判定为 `achieved` 时，在 [goal.py:418](../src/agent_harness/agents/goal.py#L418) 清空活动目标。

#### 4. 系统提示词怎么拼出来

选择「端到端自检（离线）」，在 [host.py:235](../src/agent_harness/host.py#L235) 下断点。

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                      | 实现位置                                                             | 作用                     |
| ---- | ------------------------ | ----------- | ---------------------------------------------------- | -------------------------------------------------------------------- | ------------------------ |
| 1    | `prompt_values`          | 内部·跨文件 | [host.py:235](../src/agent_harness/host.py#L235)     | [runtime_context.py:17](../src/agent_harness/runtime_context.py#L17) | 取出本轮上下文字段       |
| 2    | `assemble_system_prompt` | 内部·跨文件 | [host.py:235](../src/agent_harness/host.py#L235)     | [prompts.py:73](../src/agent_harness/context/prompts.py#L73)                 | 拼接固定规则和本轮上下文 |
| 3    | `list_skills`            | 内部·跨文件 | [prompts.py:89](../src/agent_harness/context/prompts.py#L89) | [skills.py:80](../src/agent_harness/context/skills.py#L80)                   | 加入可用 skill 目录      |

拼接完成后，在 [host.py:257](../src/agent_harness/host.py#L257) 发起模型请求。

#### 5. 工具调用与结果

选择「端到端自检（离线）」，在 [lead_tools.py:90](../src/agent_harness/agents/lead_tools.py#L90) 下断点。以 `write_file` 为例：

| 顺序 | 函数                 | 调用身份                      | 调用 / 触发位置                                                                                                                                   | 实现位置                                                     | 作用                                     |
| ---- | -------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ---------------------------------------- |
| 1 | `execute_tool` → `call_tool_handler` | 内部·跨文件 | [lead_tools.py:90](../src/agent_harness/agents/lead_tools.py#L90) → [hooks.py:81](../src/agent_harness/runtime/hooks.py#L81) | [file_tools.py:293](../src/agent_harness/tools/file_tools.py#L293) | 通过统一执行入口取得并调用处理器 |
| 2 | `bind_file_tools.write` | 工具处理器：`write_file` | 绑定 [shared_tools.py:104](../src/agent_harness/tools/shared_tools.py#L104) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [file_tools.py:252](../src/agent_harness/tools/file_tools.py#L252) | Lead 与 child 都使用按 owner 绑定的写文件闭包 |
| 3 | `tool_cwd` | 内部·同文件 | [file_tools.py:239](../src/agent_harness/tools/file_tools.py#L239) | [file_tools.py:224](../src/agent_harness/tools/file_tools.py#L224) | 每次执行时取得当前任务目录 |
| 4 | `assignment_cwd` | 内部·跨文件 | [file_tools.py:227](../src/agent_harness/tools/file_tools.py#L227) | [tasks.py:525](../src/agent_harness/workspace/tasks.py#L525) | 读取任务分配及 worktree 目录 |
| 5 | `run_write` | 内部·同文件 | [file_tools.py:253](../src/agent_harness/tools/file_tools.py#L253) → [file_tools.py:240](../src/agent_harness/tools/file_tools.py#L240) | [file_tools.py:168](../src/agent_harness/tools/file_tools.py#L168) | 通过 invoke 传入 cwd 并写入文件 |
| 6    | `safe_path`          | 内部·同文件                   | [file_tools.py:175](../src/agent_harness/tools/file_tools.py#L175)                                                                                      | [file_tools.py:27](../src/agent_harness/tools/file_tools.py#L27)   | 检查文件路径是否在允许目录内             |
| 7    | `build_user_content` | 内部·同文件                   | [host.py:471](../src/agent_harness/host.py#L471)                                                                                                  | [host.py:180](../src/agent_harness/host.py#L180)             | 把执行结果写回历史，供下一次模型调用读取 |

执行前后的权限与 hooks 见第 10 节。

#### 6. 工具池装配

选择「端到端自检（离线）」，在 [host.py:365](../src/agent_harness/host.py#L365) 下断点。
启动时由 `initialize_runtime` 在 [host.py:146](../src/agent_harness/host.py#L146) 登记工具来源，之后每次循环按顺序装配。
表中“登记 → 执行”表示通过变量间接调用。工具来源回调提供工具定义，不是 LLM 可请求的工具。

| 顺序 | 函数                    | 调用身份     | 调用 / 触发位置                                                                                                                    | 实现位置                                                           | 作用                             |
| ---- | ----------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | -------------------------------- |
| 1    | `assemble_tool_pool`    | 内部·同文件  | [host.py:365](../src/agent_harness/host.py#L365)                                                                                   | [host.py:102](../src/agent_harness/host.py#L102)                   | 装配本轮工具                     |
| 2    | `ToolRegistry.snapshot` | 内部·跨文件  | [host.py:106](../src/agent_harness/host.py#L106)                                                                                   | [tool_registry.py:36](../src/agent_harness/tools/tool_registry.py#L36)   | 统一收集工具描述、处理函数和权限 |
| 3    | `builtin_definitions`   | 工具来源回调 | 登记 [host.py:147](../src/agent_harness/host.py#L147) → [tool_registry.py:44](../src/agent_harness/tools/tool_registry.py#L44)（source） | [registrations.py:308](../src/agent_harness/registrations.py#L308) | 读取内置工具定义                 |
| 4    | `discover_tools`        | 工具来源回调 | 登记 [host.py:148](../src/agent_harness/host.py#L148) → [tool_registry.py:44](../src/agent_harness/tools/tool_registry.py#L44)（source） | [mcp_tools.py:147](../src/agent_harness/tools/mcp_tools.py#L147)         | 读取已连接 MCP 的工具定义        |

注册器在同一调用位置依次执行各来源；Workflow 来源定义在 [host.py:149](../src/agent_harness/host.py#L149)。
`compact` 由主循环单独处理，见[运行相关](#运行相关)。

#### 7. 任务生命周期

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_subagents.py::test_task_lifecycle_and_assignment_release"`。

| 顺序 | 函数                           | 调用身份                    | 调用 / 触发位置                                                                                                                                   | 实现位置                                                           | 作用                               |
| ---- | ------------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------------------------- |
| 1    | `run_create_task`              | 工具处理器：`create_task`   | 登记 [registrations.py:174](../src/agent_harness/registrations.py#L174) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [registrations.py:47](../src/agent_harness/registrations.py#L47)   | 分发创建任务请求，格式化结果       |
| 1.1  | `create_task`                  | 工具底层·跨文件             | [registrations.py:57](../src/agent_harness/registrations.py#L57)                                                                                  | [tasks.py:191](../src/agent_harness/workspace/tasks.py#L191)                 | 创建 pending 任务                  |
| 2 | `shared_tools.run_claim_task` | 工具处理器：`claim_task` | 绑定 [shared_tools.py:108](../src/agent_harness/tools/shared_tools.py#L108) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [shared_tools.py:85](../src/agent_harness/tools/shared_tools.py#L85) | 按 owner 认领任务 |
| 2.1 | `claim_task` | 工具底层·跨文件 | [shared_tools.py:87](../src/agent_harness/tools/shared_tools.py#L87) → [shared_tools.py:78](../src/agent_harness/tools/shared_tools.py#L78) | [tasks.py:421](../src/agent_harness/workspace/tasks.py#L421) | 校验任务并登记 owner 和工作目录 |
| 3 | `shared_tools.run_complete_task` | 工具处理器：`complete_task` | 绑定 [shared_tools.py:109](../src/agent_harness/tools/shared_tools.py#L109) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [shared_tools.py:90](../src/agent_harness/tools/shared_tools.py#L90) | 按 owner 完成任务 |
| 3.1 | `complete_task` | 工具底层·跨文件 | [shared_tools.py:92](../src/agent_harness/tools/shared_tools.py#L92) → [shared_tools.py:78](../src/agent_harness/tools/shared_tools.py#L78) | [tasks.py:466](../src/agent_harness/workspace/tasks.py#L466) | 把 in_progress 任务标记为 completed |
| 4    | `release_completed_assignment` | 内部·跨文件                 | [lead_turn.py:129](../src/agent_harness/agents/lead_turn.py#L129)                                                                                                  | [tasks.py:570](../src/agent_harness/workspace/tasks.py#L570)                 | 本轮结束时释放已完成任务的分配记录 |

创建、认领、完成分别由模型请求对应工具触发；分配通常在本轮结束时释放，
Lead 成功移除自己的已完成 worktree 时也会提前释放，见第 8 节。
表中的 owner 是 Lead（`agent`）。子 agent 使用相同处理器，绑定自己的 owner，复用 `tasks.py` 的业务函数。
测试会直接调用业务函数。

#### 8. 任务与 worktree 绑定

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_subagents.py::test_cross_process_claim_waits_for_worktree_binding"`。
测试使用临时 Git 仓库，子进程中的认领默认不进入父进程断点。

| 顺序 | 函数                  | 调用身份                      | 调用 / 触发位置                                                                                                                                   | 实现位置                                                           | 作用                                  |
| ---- | --------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------- |
| 1    | `run_create_worktree` | 工具处理器：`create_worktree` | 登记 [registrations.py:282](../src/agent_harness/registrations.py#L282) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [registrations.py:24](../src/agent_harness/registrations.py#L24)   | 分发创建任务工作目录的请求            |
| 1.1  | `create_worktree`     | 工具底层·跨文件               | [registrations.py:33](../src/agent_harness/registrations.py#L33)                                                                                  | [task_worktrees.py:13](../src/agent_harness/workspace/task_worktrees.py#L13) | 检查任务处于 pending 且未认领         |
| 2    | `run_git`             | 内部·跨文件                   | [task_worktrees.py:66](../src/agent_harness/workspace/task_worktrees.py#L66)                                                                                | [worktrees.py:109](../src/agent_harness/workspace/worktrees.py#L109)         | 执行 git worktree add，创建目录和分支 |
| 3    | `save_task`           | 内部·跨文件                   | [task_worktrees.py:93](../src/agent_harness/workspace/task_worktrees.py#L93)                                                                                | [tasks.py:293](../src/agent_harness/workspace/tasks.py#L293)                 | 把 worktree 名称绑定到任务            |
| 4 | `shared_tools.run_claim_task` | 工具处理器：`claim_task` | 绑定 [shared_tools.py:108](../src/agent_harness/tools/shared_tools.py#L108) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [shared_tools.py:85](../src/agent_harness/tools/shared_tools.py#L85) | 按 owner 认领任务 |
| 4.1 | `claim_task` | 工具底层·跨文件 | [shared_tools.py:87](../src/agent_harness/tools/shared_tools.py#L87) → [shared_tools.py:78](../src/agent_harness/tools/shared_tools.py#L78) | [tasks.py:421](../src/agent_harness/workspace/tasks.py#L421) | 校验任务并登记 owner 和工作目录 |

任务完成后，Lead 可请求 `remove_worktree` 工具。它拒绝未提交、未跟踪和被忽略的文件，
也拒绝子 agent占用或运行中的后台命令。Git 移除成功后，才释放 Lead 自己匹配的已完成分配并解除绑定；
清理拒绝或 Git 失败时保留原分配。移除保留分支，不执行合并。
离线观察同一轮完成、清理和目录切换，运行
`"tests/test_worktree_cleanup.py::test_lead_completes_and_removes_in_one_model_round"`。

| 顺序 | 函数                           | 调用身份                      | 调用 / 触发位置                                                                                                                                   | 实现位置                                                             | 作用                                         |
| ---- | ------------------------------ | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------------------------- |
| 1    | `run_remove_worktree`          | 工具处理器：`remove_worktree` | 登记 [registrations.py:292](../src/agent_harness/registrations.py#L292) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [registrations.py:36](../src/agent_harness/registrations.py#L36)     | 处理 Lead 的非强制清理请求                   |
| 1.1  | `remove_worktree`              | 工具底层·跨文件               | [registrations.py:43](../src/agent_harness/registrations.py#L43)                                                                                  | [task_worktrees.py:103](../src/agent_harness/workspace/task_worktrees.py#L103) | 检查任务、占用和未提交改动                   |
| 2    | `run_git`                      | 内部·跨文件                   | [task_worktrees.py:165](../src/agent_harness/workspace/task_worktrees.py#L165)                                                                              | [worktrees.py:109](../src/agent_harness/workspace/worktrees.py#L109)           | 执行 git worktree remove，保留分支           |
| 3    | `release_completed_assignment` | 内部·跨文件                   | [task_worktrees.py:174](../src/agent_harness/workspace/task_worktrees.py#L174)                                                                              | [tasks.py:570](../src/agent_harness/workspace/tasks.py#L570)                   | 移除成功后释放 Lead 自己的已完成分配（如有） |
| 4    | `save_task`                    | 内部·跨文件                   | [task_worktrees.py:177](../src/agent_harness/workspace/task_worktrees.py#L177)                                                                              | [tasks.py:293](../src/agent_harness/workspace/tasks.py#L293)                   | 清除任务中的 worktree 绑定                   |

#### 9. 统一子 agent 生命周期与协议

离线调试选择「pytest（当前文件）」，运行 `"tests/test_subagent_lifetimes.py"`；该文件用 fake 模型和真实线程验证两种生命周期。

Lead 模型选择是否委派及 mode。once 同步交付最终结果；persistent 立即交付 started，保留同一历史等待下一次委派，只在当前进程内存活。子 agent 不具备递归创建或 worktree 创建/移除工具。

`spawn_subagent` 的可选 `tools` 省略时使用模式默认工具，`[]` 关闭工具。返回的 usage 累计执行与摘要请求的用量；persistent 的 started 只返回零快照。

**创建与共享执行：**

| 顺序 | 函数                           | 调用身份                   | 调用 / 触发位置                                                                                                                          | 实现位置                                                                 | 作用                                             |
| ---- | ------------------------------ | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------ |
| 1    | `run_spawn_subagent`           | 工具处理器：spawn_subagent | [registrations.py:239](../src/agent_harness/registrations.py#L239) | [registrations.py:114](../src/agent_harness/registrations.py#L114) | 把模型参数交给统一运行时，序列化结果             |
| 2    | `spawn_subagent`               | 工具底层·跨文件            | [registrations.py:117](../src/agent_harness/registrations.py#L117) | [subagent.py:380](../src/agent_harness/agents/subagent.py#L380) | 验证、登记、可选认领；按 mode 同步执行或启动线程 |
| 2.1  | `claim_task`                   | 内部·跨文件                | [subagent.py:435](../src/agent_harness/agents/subagent.py#L435) | [tasks.py:421](../src/agent_harness/workspace/tasks.py#L421) | 只有显式指定初始 task_id 才认领                  |
| 3    | `create_subagent_session`      | 内部·同文件                | [subagent.py:461](../src/agent_harness/agents/subagent.py#L461) | [subagent.py:201](../src/agent_harness/agents/subagent.py#L201) | 为两种模式构造相同的模型执行器                   |
| 4    | `_execute_work`                | 内部·同文件                | [subagent.py:349](../src/agent_harness/agents/subagent.py#L349) | [subagent.py:316](../src/agent_harness/agents/subagent.py#L316) | 循环执行模型回合，直到最终回答                   |
| 5    | `SubagentSession.run`          | 内部·跨文件                | [subagent.py:328](../src/agent_harness/agents/subagent.py#L328) | [subagent_session.py:137](../src/agent_harness/agents/subagent_session.py#L137) | 按当前委派整理历史，请求模型并回填工具结果       |
| 5.1  | `SubagentSession._request`     | 内部·同文件                | [subagent_session.py:160](../src/agent_harness/agents/subagent_session.py#L160) | [subagent_session.py:193](../src/agent_harness/agents/subagent_session.py#L193) | 每次请求携带当前委派，旧委派只作为历史参考       |
| 6    | `SubagentSession.execute_tool` | 内部·同文件                | [subagent_session.py:184](../src/agent_harness/agents/subagent_session.py#L184) | [subagent_session.py:117](../src/agent_harness/agents/subagent_session.py#L117) | 检查计划门禁和 hooks，再执行工具                 |

**持久实例、通信与收尾：**

| 顺序 | 函数                           | 调用身份                   | 调用 / 触发位置                                                                                                                                | 实现位置                                                 | 作用                                           |
| ---- | ------------------------------ | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------------- |
| 1    | `_persistent_loop`             | 线程入口·跨回合            | [subagent.py:345](../src/agent_harness/agents/subagent.py#L345) | [subagent.py:345](../src/agent_harness/agents/subagent.py#L345) | 每次委派共用 _execute_work，最终结果经信箱交付 |
| 2    | `send_message`                 | Lead 工具底层 / 子模型回调 | [subagent.py:208](../src/agent_harness/agents/subagent.py#L208) | [subagent.py:251](../src/agent_harness/agents/subagent.py#L251) | 是否发信及内容由发送方模型决定，程序投递       |
| 3    | `_receive`                     | 内部·同文件                | [subagent.py:322](../src/agent_harness/agents/subagent.py#L322) | [subagent.py:275](../src/agent_harness/agents/subagent.py#L275) | 在模型回合之间消费消息与协议                   |
| 4    | `release_completed_assignment` | 内部·跨文件                | [subagent.py:357](../src/agent_harness/agents/subagent.py#L357) | [tasks.py:570](../src/agent_harness/workspace/tasks.py#L570) | 最终回答后释放已完成任务目录，保留实例和历史   |
| 5    | `_wait_for_work`               | 内部·同文件                | [subagent.py:308](../src/agent_harness/agents/subagent.py#L308) | [subagent.py:308](../src/agent_harness/agents/subagent.py#L308) | IDLE 只等消息或关闭；不扫描任务板              |
| 6    | `_cleanup`                     | 内部·同文件                | [subagent.py:334](../src/agent_harness/agents/subagent.py#L334) | [subagent.py:334](../src/agent_harness/agents/subagent.py#L334) | 实例退出时清理登记、协议和任务分配             |
| 7    | `consume_lead_inbox`           | 内部·跨文件                | [interactive_runtime.py:245](../src/agent_harness/runtime/interactive_runtime.py#L245) | [subagent.py:143](../src/agent_harness/agents/subagent.py#L143) | 共享驱动在后台唤醒回合收取子 agent 结果       |

任务 A 完成后先汇报并释放分配，persistent 实例继续等待。Lead 发来 B 的 ID 和说明后，子模型显式 claim_task 才切换目录。任务未完成的最终文本只表示等待指导，仍占用原任务，不能认领 B。无 Task 的调查直接使用启动目录。

**计划审批与关闭：**

| 顺序 | 函数                     | 调用身份                          | 调用 / 触发位置                                                                                                                        | 实现位置                                                 | 作用                                     |
| ---- | ------------------------ | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------- |
| 1    | `submit_plan`            | 子模型工具回调                    | [subagent.py:209](../src/agent_harness/agents/subagent.py#L209) | [subagent.py:500](../src/agent_harness/agents/subagent.py#L500) | 创建当前任务与版本对应的审批请求         |
| 2    | `run_review_plan`        | Lead 工具处理器：review_plan      | [registrations.py:269](../src/agent_harness/registrations.py#L269) | [subagent.py:567](../src/agent_harness/agents/subagent.py#L567) | 按 request_id 审核，参数没有 name        |
| 3    | `apply_plan_response`    | 内部·同文件                       | [subagent.py:290](../src/agent_harness/agents/subagent.py#L290) | [subagent.py:215](../src/agent_harness/agents/subagent.py#L215) | 收信后校验身份、请求、任务版本并更新门禁 |
| 4    | `run_request_shutdown`   | Lead 工具处理器：request_shutdown | [registrations.py:254](../src/agent_harness/registrations.py#L254) | [subagent.py:527](../src/agent_harness/agents/subagent.py#L527) | 向持久实例发出关闭请求；目标参数为 name  |
| 5    | `apply_shutdown_request` | 内部·同文件                       | [subagent.py:282](../src/agent_harness/agents/subagent.py#L282) | [subagent.py:234](../src/agent_harness/agents/subagent.py#L234) | 只接受有效关闭请求，确认后退出并清理     |

计划被拒绝必须重新提交，旧审批不能解锁新任务。once 不提供消息或计划工具，且拒绝 require_plan=True。

协议状态与转换由 [subagent_protocol.py](../src/agent_harness/agents/subagent_protocol.py) 唯一维护。subagent 仍负责线程与投递，任务身份通过 [tasks.py:58](../src/agent_harness/workspace/tasks.py#L58) 一致读取。审批决定与收到回复后的门禁分别维护。


#### 10. 权限与 hooks

选择「pytest（当前文件）」，分别运行
`"tests/test_permissions.py::test_file_paths_must_stay_inside_the_workspace"` 和
`"tests/test_permissions.py::test_hooks_return_the_first_non_none_result"`。

| 顺序 | 函数 | 调用身份 | 调用 / 触发位置 | 实现位置 | 作用 |
| --- | --- | --- | --- | --- | --- |
| 1 | `check_tool_use` | 内部·跨文件 | [lead_tools.py:58](../src/agent_harness/agents/lead_tools.py#L58)；[subagent_session.py:128](../src/agent_harness/agents/subagent_session.py#L128) | [hooks.py:61](../src/agent_harness/runtime/hooks.py#L61) | 授权、前置 hook 和停止判断 |
| 2 | `trigger_hooks（PreToolUse）` | 内部·同文件 | [hooks.py:68](../src/agent_harness/runtime/hooks.py#L68) | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31) | 执行登记回调，取首个非 None 结果 |
| 3 | `permission_hook` | hook 回调 | [hooks.py:172](../src/agent_harness/runtime/hooks.py#L172) | [hooks.py:108](../src/agent_harness/runtime/hooks.py#L108) | 检查命令、文件路径和 MCP 权限 |
| 4 | `execute_tool` | 内部·跨文件 | [lead_tools.py:90](../src/agent_harness/agents/lead_tools.py#L90)；[subagent_session.py:134](../src/agent_harness/agents/subagent_session.py#L134) | [hooks.py:76](../src/agent_harness/runtime/hooks.py#L76) | 执行 handler，保留状态回调 |
| 5 | `trigger_hooks（PostToolUse）` | 内部·同文件 | [hooks.py:85](../src/agent_harness/runtime/hooks.py#L85) | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31) | handler 返回后触发一次；后置处理结束才发布完成状态 |
| 6 | `trigger_hooks（Stop）` | 内部·跨文件 | [host.py:429](../src/agent_harness/host.py#L429) | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31) | 最终回答边界触发 |

UserPromptSubmit 在用户输入边界触发。上表按边界汇总分支，不表示每轮都会依次执行全部事件。
hooks 是运行时回调，不是模型工具。Lead 和 child 共用检查与同步执行；child 先检查工具资格与计划授权。授权和 hook 只有 None 表示放行，空字符串、False、0 都拒绝。
Lead 停止优先于前置拒绝；拒绝或停止不执行 handler/PostToolUse。compact 保留特殊路径，后台的 PostToolUse 由 scheduler 在实际完成后触发。

#### 11. 后台任务 / cron

选择「pytest（当前文件）」，分别运行
`"tests/test_scheduler.py::test_completed_result_is_collected_once"` 和
`"tests/test_scheduler.py::test_cron_tools_schedule_list_and_cancel"`。
真实时钟触发需在交互会话中创建定时任务并等待。
后台 bash 由模型设置 `run_in_background=true` 请求，后台线程和通知收取由程序管理。
cron 的创建、查询、取消属于模型工具；到期检查由调度线程自动执行。

**后台 bash：**

| 顺序 | 函数                              | 调用身份      | 调用 / 触发位置                                                                                                                            | 实现位置                                                   | 作用                                 |
| ---- | --------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------- | ------------------------------------ |
| 1    | `should_run_background`           | 内部·跨文件   | [lead_tools.py:64](../src/agent_harness/agents/lead_tools.py#L64)                                                                                           | [scheduler.py:38](../src/agent_harness/runtime/scheduler.py#L38)   | Lead 工具批次检查 bash 的后台标志                 |
| 2    | `start_background_task`           | 内部·跨文件   | [lead_tools.py:71](../src/agent_harness/agents/lead_tools.py#L71)                                                                                           | [scheduler.py:50](../src/agent_harness/runtime/scheduler.py#L50)   | 登记后台任务并启动线程               |
| 3 | `run_worker` → `worker` | 线程入口·局部 | [scheduler.py:114](../src/agent_harness/runtime/scheduler.py#L114)（线程目标）→ [scheduler.py:116](../src/agent_harness/runtime/scheduler.py#L116)（启动） | [scheduler.py:106](../src/agent_harness/runtime/scheduler.py#L106) → [scheduler.py:62](../src/agent_harness/runtime/scheduler.py#L62) | 绑定执行上下文，再执行命令并发布后台状态 |
| 4    | `inject_background_notifications` | 内部·同文件   | [host.py:346](../src/agent_harness/host.py#L346)                                                                                           | [host.py:201](../src/agent_harness/host.py#L201)           | 后续循环中收取已完成通知             |
| 5    | `collect_background_results`      | 内部·跨文件   | [host.py:211](../src/agent_harness/host.py#L211)                                                                                           | [scheduler.py:139](../src/agent_harness/runtime/scheduler.py#L139) | 保存长输出并交付通知，成功后清理记录 |

**cron 创建：**

| 顺序 | 函数                | 调用身份                    | 调用 / 触发位置                                                                                                                                   | 实现位置                                                   | 作用                            |
| ---- | ------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------- |
| 1    | `run_schedule_cron` | 工具处理器：`schedule_cron` | 登记 [registrations.py:214](../src/agent_harness/registrations.py#L214) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [scheduler.py:491](../src/agent_harness/runtime/scheduler.py#L491) | 通过工具分发接收定时请求        |
| 2    | `schedule_job`      | 内部·同文件                 | [scheduler.py:498](../src/agent_harness/runtime/scheduler.py#L498)                                                                                        | [scheduler.py:348](../src/agent_harness/runtime/scheduler.py#L348) | 登记任务，按 durable 设置持久化 |
| 3    | `validate_cron`     | 内部·同文件                 | [scheduler.py:357](../src/agent_harness/runtime/scheduler.py#L357)                                                                                        | [scheduler.py:293](../src/agent_harness/runtime/scheduler.py#L293) | 校验时间表达式                  |

**cron 到期触发：**

| 顺序 | 函数                    | 调用身份        | 调用 / 触发位置                                                        | 实现位置                                                   | 作用                               |
| ---- | ----------------------- | --------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------- |
| 1    | `cron_scheduler_loop`   | 线程入口·同文件 | [scheduler.py:555](../src/agent_harness/runtime/scheduler.py#L555)（线程启动） | [scheduler.py:419](../src/agent_harness/runtime/scheduler.py#L419) | 服务启动后在线程中持续检查到期任务 |
| 2    | `cron_matches`          | 内部·同文件     | [scheduler.py:433](../src/agent_harness/runtime/scheduler.py#L433)             | [scheduler.py:233](../src/agent_harness/runtime/scheduler.py#L233) | 检查当前时间是否匹配               |
| 3    | `_enqueue_due_job`      | 内部·同文件     | [scheduler.py:434](../src/agent_harness/runtime/scheduler.py#L434)             | [scheduler.py:401](../src/agent_harness/runtime/scheduler.py#L401) | 把到期任务放入队列                 |
| 4    | `consume_cron_queue`    | 内部·跨文件     | [lead_turn.py:89](../src/agent_harness/agents/lead_turn.py#L89)                       | [scheduler.py:440](../src/agent_harness/runtime/scheduler.py#L440) | LeadTurn 登记到期任务并注入历史       |
| 5    | `call_llm`              | 内部·同文件     | [host.py:374](../src/agent_harness/host.py#L374)                       | [host.py:222](../src/agent_harness/host.py#L222)           | 把定时提示词交给模型               |
| 6    | `acknowledge_cron_jobs` | 内部·跨文件     | [lead_turn.py:109](../src/agent_harness/agents/lead_turn.py#L109)                       | [scheduler.py:451](../src/agent_harness/runtime/scheduler.py#L451) | 模型响应成功后确认交付             |

**查看与取消是另外两个工具入口：**

| 顺序 | 函数              | 调用身份                  | 调用 / 触发位置                                                                                                                                   | 实现位置                                                   | 作用             |
| ---- | ----------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------- |
| 1    | `run_list_crons`  | 工具处理器：`list_crons`  | 登记 [registrations.py:217](../src/agent_harness/registrations.py#L217) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [scheduler.py:504](../src/agent_harness/runtime/scheduler.py#L504) | 查看当前定时任务 |
| 2    | `run_cancel_cron` | 工具处理器：`cancel_cron` | 登记 [registrations.py:222](../src/agent_harness/registrations.py#L222) → [file_tools.py:304](../src/agent_harness/tools/file_tools.py#L304)（handler） | [scheduler.py:520](../src/agent_harness/runtime/scheduler.py#L520) | 接收取消请求     |
| 3    | `cancel_job`      | 内部·同文件               | [scheduler.py:526](../src/agent_harness/runtime/scheduler.py#L526)                                                                                        | [scheduler.py:379](../src/agent_harness/runtime/scheduler.py#L379) | 删除对应任务     |

#### 12. 工作流单次 agent 调用

选择「Workflow demo（离线，不联网）」。demo 和交互都经过统一 once 子 agent；demo 注入 DemoModelClient，运行记录写入启动目录的 .runtime/。

交互中主模型调用 `Workflow` 工具，处理器 [run_workflow_sync](../src/agent_harness/workflows/workflow_runtime.py#L1026) 在 [host.py:150](../src/agent_harness/host.py#L150) 登记。
工作流脚本调用 `ctx.agent`；步骤顺序与并行由脚本决定，步骤内使用哪些工具由子模型决定。
默认提供 bash、读写、编辑和 glob；`tools=[]` 关闭工具，非空名单限制可用工具。

| 顺序 | 函数 | 调用身份 | 调用 / 触发位置 | 实现位置 | 作用 |
| --- | --- | --- | --- | --- | --- |
| 1 | `ExecutionState.agent` | 工作流 API（协程） | [workflow_demo.py:111](../src/agent_harness/workflows/workflow_demo.py#L111) | [workflow_runtime.py:613](../src/agent_harness/workflows/workflow_runtime.py#L613) | 接收步骤指令和工具配置 |
| 2 | `WorkflowJournal.key` | 内部·同文件 | [workflow_runtime.py:627](../src/agent_harness/workflows/workflow_runtime.py#L627) | [workflow_runtime.py:400](../src/agent_harness/workflows/workflow_runtime.py#L400) | 用指令、标签、schema、工具配置生成缓存键 |
| 3 | `_run_agent` | 内部·同文件 | [workflow_runtime.py:629](../src/agent_harness/workflows/workflow_runtime.py#L629) | [workflow_runtime.py:631](../src/agent_harness/workflows/workflow_runtime.py#L631) | 管理缓存、执行、格式校验和记账 |
| 4 | `WorkflowJournal.cached` | 内部·同文件 | [workflow_runtime.py:638](../src/agent_harness/workflows/workflow_runtime.py#L638) | [workflow_runtime.py:411](../src/agent_harness/workflows/workflow_runtime.py#L411) | 命中时直接重放，不创建子 agent |
| 5 | `Budget.remaining` | 内部·同文件 | [workflow_runtime.py:687](../src/agent_harness/workflows/workflow_runtime.py#L687) | [workflow_runtime.py:468](../src/agent_harness/workflows/workflow_runtime.py#L468) | 检查剩余预算 |
| 6 | `SubagentRunner.run` | 线程任务·跨文件 | [workflow_runtime.py:693](../src/agent_harness/workflows/workflow_runtime.py#L693) | [workflow_runtime.py:294](../src/agent_harness/workflows/workflow_runtime.py#L294) | 将步骤接入统一 once 创建入口 |
| 7 | `spawn_subagent` | 内部·跨文件 | [workflow_runtime.py:309](../src/agent_harness/workflows/workflow_runtime.py#L309) | [subagent.py:380](../src/agent_harness/agents/subagent.py#L380) | 登记步骤实例，按工具名单执行并清理 |
| 8 | `_execute_work` | 内部·同文件 | [subagent.py:349](../src/agent_harness/agents/subagent.py#L349) | [subagent.py:316](../src/agent_harness/agents/subagent.py#L316) | 多回合执行到最终回答 |
| 9 | `SubagentSession.run` | 内部·跨文件 | [subagent.py:328](../src/agent_harness/agents/subagent.py#L328) | [subagent_session.py:137](../src/agent_harness/agents/subagent_session.py#L137) | 压缩历史，请求模型，回填工具结果 |
| 10 | `SubagentSession.execute_tool` | 内部·同文件 | [subagent_session.py:184](../src/agent_harness/agents/subagent_session.py#L184) | [subagent_session.py:117](../src/agent_harness/agents/subagent_session.py#L117) | 校验可用工具、计划门禁和权限 hooks |
| 11 | `_parse_runner_json` | 内部·同文件 | [workflow_runtime.py:320](../src/agent_harness/workflows/workflow_runtime.py#L320) | [workflow_runtime.py:256](../src/agent_harness/workflows/workflow_runtime.py#L256) | 步骤返回后解析结构化文本 |
| 12 | `SimpleJsonSchema.validate` | 内部·同文件 | [workflow_runtime.py:659](../src/agent_harness/workflows/workflow_runtime.py#L659) | [workflow_runtime.py:200](../src/agent_harness/workflows/workflow_runtime.py#L200) | 校验结果；不合格时最多一次无工具格式修正 |
| 13 | `_call_runner` | 内部·同文件 | [workflow_runtime.py:655](../src/agent_harness/workflows/workflow_runtime.py#L655) | [workflow_runtime.py:690](../src/agent_harness/workflows/workflow_runtime.py#L690) | 每次请求立即结算；取消也等待已启动调用 |
| 14 | `Budget.add` | 内部·同文件 | [workflow_runtime.py:704](../src/agent_harness/workflows/workflow_runtime.py#L704) | [workflow_runtime.py:449](../src/agent_harness/workflows/workflow_runtime.py#L449) | 累计预算并检查超额 |
| 15 | `WorkflowJournal.record` | 内部·同文件 | [workflow_runtime.py:679](../src/agent_harness/workflows/workflow_runtime.py#L679) | [workflow_runtime.py:419](../src/agent_harness/workflows/workflow_runtime.py#L419) | 只缓存成功且格式合格的步骤 |

格式修正只处理上次最终结果，关闭工具，文件操作不会因格式错误再执行一次。失败消费也保存在任务快照，恢复时继续计入预算。
普通失败、取消和预算超额的收尾规则见 [README](../README.md#行为要点)。
整个流程的输出在 [workflow_runtime.py:901](../src/agent_harness/workflows/workflow_runtime.py#L901) 保存，任务快照在 [workflow_runtime.py:902](../src/agent_harness/workflows/workflow_runtime.py#L902) 保存。
真实客户端由 [workflow_runtime.py:290](../src/agent_harness/workflows/workflow_runtime.py#L290) 默认读取 `core.client`；离线客户端在 [cli.py:227](../src/agent_harness/frontends/cli.py#L227) 注入。

#### 13. journal 读写与 resume

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_workflow.py::test_resume_replays_at_budget_limit_without_spending_again"`。

journal 的方法都是工作流内部调用。恢复通过 CLI 的 `workflow resume`，或 `Workflow` 工具的
`resume_from_run_id` 参数进入；LLM 不直接请求这些 journal 方法。

**首次运行：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                                  | 实现位置                                                                 | 作用                       |
| ---- | -------------------------- | ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------- |
| 1    | `WorkflowJournal.__init__` | 内部·同文件 | [workflow_runtime.py:843](../src/agent_harness/workflows/workflow_runtime.py#L843) | [workflow_runtime.py:335](../src/agent_harness/workflows/workflow_runtime.py#L335) | 创建新的 journal 文件      |
| 2    | `WorkflowJournal.cached`   | 内部·同文件 | [workflow_runtime.py:638](../src/agent_harness/workflows/workflow_runtime.py#L638) | [workflow_runtime.py:411](../src/agent_harness/workflows/workflow_runtime.py#L411) | 检查本步骤是否已有记录     |
| 3    | `WorkflowJournal.record`   | 内部·同文件 | [workflow_runtime.py:679](../src/agent_harness/workflows/workflow_runtime.py#L679) | [workflow_runtime.py:419](../src/agent_harness/workflows/workflow_runtime.py#L419) | 步骤成功后追加记录并 flush |

**恢复运行：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                                  | 实现位置                                                                 | 作用                             |
| ---- | -------------------------- | ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------- |
| 1    | `_read_snapshot`           | 内部·同文件 | [workflow_runtime.py:832](../src/agent_harness/workflows/workflow_runtime.py#L832) | [workflow_runtime.py:924](../src/agent_harness/workflows/workflow_runtime.py#L924) | 读取原运行的任务和参数           |
| 2    | `WorkflowJournal.__init__` | 内部·同文件 | [workflow_runtime.py:840](../src/agent_harness/workflows/workflow_runtime.py#L840) | [workflow_runtime.py:335](../src/agent_harness/workflows/workflow_runtime.py#L335) | 读取 journal，恢复缓存和历史消费 |
| 3    | `WorkflowJournal.key`      | 内部·同文件 | [workflow_runtime.py:627](../src/agent_harness/workflows/workflow_runtime.py#L627) | [workflow_runtime.py:400](../src/agent_harness/workflows/workflow_runtime.py#L400) | 生成与原步骤对应的缓存键         |
| 4    | `WorkflowJournal.cached`   | 内部·同文件 | [workflow_runtime.py:638](../src/agent_harness/workflows/workflow_runtime.py#L638) | [workflow_runtime.py:411](../src/agent_harness/workflows/workflow_runtime.py#L411) | 命中后复用结果，不再执行 runner  |

缓存未命中时，在 [workflow_runtime.py:687](../src/agent_harness/workflows/workflow_runtime.py#L687) 检查包含历史消费的剩余预算。

缓存键包含工具名单。默认配置与显式列出全部基础工具等价，关闭或限制工具会使用不同的键。恢复重放结果和历史消费，不重新执行工具，也不重建已删除的文件。

#### 14. memory 选取 / 抽取

选择「Agent 交互（真实 API）」。没有已有记忆时跳过选取，正常回答结束后仍会进入抽取。
选取、抽取和整理由运行时自动触发。这些函数会请求模型，但没有登记为模型工具。

**请求模型前选取记忆：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                      | 实现位置                                                             | 作用                              |
| ---- | -------------------------- | ----------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- | --------------------------------- |
| 1    | `load_memories`            | 内部·跨文件 | [runtime_context.py:42](../src/agent_harness/runtime_context.py#L42) | [memory_runtime.py:371](../src/agent_harness/context/memory_runtime.py#L371) | 按预算加载当前请求相关的记忆      |
| 2    | `select_relevant_memories` | 内部·同文件 | [memory_runtime.py:386](../src/agent_harness/context/memory_runtime.py#L386) | [memory_runtime.py:314](../src/agent_harness/context/memory_runtime.py#L314) | 从当前记忆库筛选相关记录          |
| 3    | `list_memory_files`        | 内部·同文件 | [memory_runtime.py:323](../src/agent_harness/context/memory_runtime.py#L323) | [memory_runtime.py:228](../src/agent_harness/context/memory_runtime.py#L228) | 读取记忆目录                      |
| 4    | `recent_user_text`         | 内部·同文件 | [memory_runtime.py:325](../src/agent_harness/context/memory_runtime.py#L325) | [memory_runtime.py:286](../src/agent_harness/context/memory_runtime.py#L286) | 取得最近用户文本作为查询          |
| 5    | `extract_json_array`       | 内部·同文件 | [memory_runtime.py:350](../src/agent_harness/context/memory_runtime.py#L350) | [memory_runtime.py:273](../src/agent_harness/context/memory_runtime.py#L273) | 解析模型选中的记录下标            |
| 6    | `read_memory_file`         | 内部·同文件 | [memory_runtime.py:388](../src/agent_harness/context/memory_runtime.py#L388) | [memory_runtime.py:216](../src/agent_harness/context/memory_runtime.py#L216) | 读取选中的记忆正文                |

筛选请求失败时，在 [memory_runtime.py:368](../src/agent_harness/context/memory_runtime.py#L368) 调用关键词匹配，实现位于 [memory_runtime.py:298](../src/agent_harness/context/memory_runtime.py#L298)。

**回答结束后抽取并整理记忆：**

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                                      | 实现位置                                                             | 作用                       |
| ---- | ------------------------ | ----------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------- |
| 1    | `extract_memories`       | 内部·跨文件 | [host.py:168](../src/agent_harness/host.py#L168)                     | [memory_runtime.py:443](../src/agent_harness/context/memory_runtime.py#L443) | 从最近对话中抽取候选记忆   |
| 2    | `validate_memory_record` | 内部·同文件 | [memory_runtime.py:492](../src/agent_harness/context/memory_runtime.py#L492) | [memory_runtime.py:412](../src/agent_harness/context/memory_runtime.py#L412) | 检查候选记录是否合法       |
| 3    | `should_store_memory`    | 内部·同文件 | [memory_runtime.py:500](../src/agent_harness/context/memory_runtime.py#L500) | [memory_runtime.py:103](../src/agent_harness/context/memory_runtime.py#L103) | 检查是否值得保存及是否重复 |
| 4    | `write_memory_file`      | 内部·同文件 | [memory_runtime.py:502](../src/agent_harness/context/memory_runtime.py#L502) | [memory_runtime.py:149](../src/agent_harness/context/memory_runtime.py#L149) | 写入通过检查的记忆         |
| 5    | `rebuild_memory_index`   | 内部·同文件 | [memory_runtime.py:168](../src/agent_harness/context/memory_runtime.py#L168) | [memory_runtime.py:171](../src/agent_harness/context/memory_runtime.py#L171) | 更新索引                   |
| 6    | `consolidate_memories`   | 内部·跨文件 | [host.py:171](../src/agent_harness/host.py#L171)                     | [memory_runtime.py:518](../src/agent_harness/context/memory_runtime.py#L518) | 有新记忆写入时合并重复记录 |

#### 15. 启动与 `/goal` 拦截

CLI 和 Textual 都将输入交给 [InteractiveRuntime](../src/agent_harness/runtime/interactive_runtime.py)。
`/goal` 是共享驱动识别的本地命令，不是 LLM 工具。查询和清除只交付命令结果；
设置条件后继续执行关联模型回合，CLI 等到该回合结束才恢复普通输入。

| 断点 / 入口 | 实现位置 | 观察内容 |
| --- | --- | --- |
| `CLIAdapter.run` / `pump` | [cli.py](../src/agent_harness/frontends/cli.py) | 唯一输入读取者；普通输入和审批决定 |
| `InteractiveRuntime._run` | [interactive_runtime.py](../src/agent_harness/runtime/interactive_runtime.py) | 启动服务，串行处理前台输入和后台唤醒 |
| `InteractiveRuntime._command` | 同上 | goal 查询、清除和设置；会话列表、恢复和删除 |
| `InteractiveRuntime._resume` | 同上 | 按显式目标目录准备，再统一激活上下文；失败边界见 README |
| `InteractiveRuntime._execute` | 同上 | hook、历史更新、会话记录、调用 host 和一次性回合结束 |
| `InteractiveRuntime._checkpoint` | 同上 | 轮询后台事实并保存已投影的展示记录；普通事实到达即投影交付 |
| `agent_loop` | [host.py](../src/agent_harness/host.py) | 模型、工具、停止检查、goal 和 memory 收尾 |
| `CLIAdapter._render_events` | [cli.py](../src/agent_harness/frontends/cli.py) | 输出线程展示完成后调用 `acknowledge_event()`，始终不读取输入 |
| `HarnessApp.consume` | [tui/app.py](../src/agent_harness/frontends/tui/app.py) | UI 线程分批展示并确认事件，再取可交付审批 |
| `InteractionChannel.next_approval` | [interaction.py](../src/agent_harness/runtime/interaction.py) | 等待前置事件展示完成；统一过滤过期会话事件和审批，后续日志不阻挡请求 |

旧 `async_event_loop` 和 `print_turn_assistants` 已移除。执行线程只发布普通数据，adapter 负责呈现。
TUI 操作见 [怎么跑](#textual-交互界面)，调试源码 TUI 可将现有交互配置的 `args` 改为 `["tui"]`。

### 用法要点

- 想省额度就先调试「Workflow demo（离线）」，摸清断点再打真实 API。
- 断住时用「调试控制台」看变量，或鼠标悬停；左侧「变量」面板可在调用栈里逐层展开。
- 停止调试按 Shift+F5；daemon 线程会随主进程一起结束。

### 可选：从另一个窗口 attach

Remote-WSL 已让断点自动生效，一般不需要。真要 attach：在 WSL 里跑
`python -m debugpy --listen 5678 --wait-for-client -m agent_harness`，再在 launch.json 加
`"request": "attach"` + `"connect": {"host": "127.0.0.1", "port": 5678}` 和 `"pathMappings"`。

## 四、踩过的坑

### 运行相关

- **模型输出额度**集中在 [core.py](../src/agent_harness/core.py)。主循环、子 agent和一次性子 agent使用
  `DEFAULT_MAX_TOKENS`；工作流、历史压缩、记忆筛选、抽取和合并各有专用常量。
  目标评估器的额度由 [PromptGoalEvaluator](../src/agent_harness/agents/goal.py#L173) 的参数单独设置。
- **运行参数**：命令超时、工具输出上限、子代理轮数、重试退避和事件轮询间隔在
  [core.py](../src/agent_harness/core.py) 修改；记忆输入预算在
  [memory_runtime.py](../src/agent_harness/context/memory_runtime.py)，后台通知摘要长度在
  [scheduler.py](../src/agent_harness/runtime/scheduler.py)。输入字符预算与输出 token 额度分别设置。
  独立 agent 脚手架和参考模板使用各自文件顶部的配置，不依赖本项目的 `core.py`。
- **`compact` 没有 handler**。它是 `BUILTIN_TOOLS` 里的 schema，由
  [ManualCompaction.handle_tool_use](../src/agent_harness/context/compaction.py#L631) 处理，不进 handler 表。
  所以"27 工具 / 26 handler"是正常的。
- **主动压缩每轮只执行一次**。压缩后本轮不再向模型提供 `compact`；下一条用户请求可以再次主动
  压缩。上下文超限触发的自动压缩不受此限制。
- **自动压缩按字符预算计算**。修改 [core.CONTEXT_LIMIT](../src/agent_harness/core.py)（默认 50000）后，
  触发线 80%（40000）、压缩目标 70%（35000）、最新工具结果批次预算 16%（8000）、单条结果落盘
  阈值 8%（4000）和预览长度 2%（1000）会随之变化。低于触发线就停止，无须强行达到目标；主动
  `compact` 不受这些阈值限制。字符数不等于模型 token 数，系统提示词和工具定义不计入此预算。
- **`goal` 的 `block` 会持续循环**，直到评估器放行或达到 `block_cap`（默认 8）。写脚本时
  评估器要能"先 block 再 allow"，否则会一直要下一条脚本回复。
- **worktree 绑定要求任务处于 `pending` 且无 owner**。已认领的任务不能再绑定新 worktree。
- **worktree 清理只供 Lead 模型请求**，使用规则和分配释放时机见[第 8 节](#8-任务与-worktree-绑定)。
  工具只接受名称，不提供 `discard_changes`；内部 Python 接口仍允许显式丢弃改动。
- **运行目录以 `core.WORKDIR` 为准**。任务、worktree、memory、workflow、transcript 和工具结果目录
  都在调用时从它解析；首次交互使用当前启动目录，恢复时由后端切回原目录。会话上下文的用户目录位置见 README。调试时不要修改旧的 `host.WORKDIR`。
- **`.env` 与启动目录无关**。`load_dotenv()` 从包文件位置逐级向上查找，所以在任何目录启动
  都能读到项目根的 `.env`。

### 调试相关

- **`.env` 会覆盖 launch.json 里的 `env`**（代码是 `load_dotenv(override=True)`）。换模型或
  base_url 要改 `.env`，在 launch.json 里设同名变量无效。
- **`debugpy` 只用于调试**，不在 `pyproject.toml` 依赖里，已装 1.8.22；换环境时单独补：
  ```sh
  conda run -n learncc pip install debugpy
  ```
- **断点是空心圆（不命中）**：多半是 frozen modules 或源码路径不一致。五个配置都带了
  `pythonArgs: ["-Xfrozen_modules=off"]`（Python 3.11+ 必需）；路径问题请确认开的是 WSL 窗口。
- **共享驱动线程** `interactive-lead` 同时执行用户回合和后台唤醒回合；`cli-output` 只呈现事件。
  排查输入来源时观察 `InteractiveRuntime._execute` 的 `kind`，再沿 `agent_loop` 跟踪执行。
- **集成终端里方向键/中文输入偶尔异常**，改用「外部终端」那条配置。

## 五、未覆盖（需要真实 API）

自检刻意不碰这些，要手动验证：

- 真实模型下的交互问答（见「一、怎么跑」）
- 真实模型下的子 agent 协作决策（线程、信箱和关闭已离线验证）
- memory 抽取与合并
