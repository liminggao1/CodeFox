# 开发笔记：运行与调试

> 初次记录：2026-10-04；最后核对：2026-10-14
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
$PY scripts/debug_pass.py                 # 端到端自检，10 项
$PY scripts/debug_chat.py                 # 模拟 API 交互：输入“你好”或“写文件”
```

`pytest` 和自检不需要 `PYTHONPATH`（前者由 `tests/conftest.py` 注入 `src`，后者自己插入）。

### 两者的分工

|      | `pytest`                                                      | `scripts/debug_pass.py`                                                   |
| ---- | ------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 定位 | 单元与契约测试                                                | 子进程 / 真 Git / 线程集成自检                                            |
| 粒度 | 按模块拆分的测试文件                                          | 一条命令 10 项                                                            |
| 覆盖 | 任务、压缩、调度、权限、编码、技能、goal、workflow、subagents | 工具池、agent 回环、goal 边界、任务、workflow、worktree、子 agent、架构图 |
| 独有 | 边界条件与错误分支                                            | 真起子进程跑 CLI、真建 Git 仓库、跨模块串联检查                           |

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

**对话与布局：**顶部是 1 行品牌栏，左侧对话区，右侧只读会话状态面板默认收起，底部是输入区。
品牌栏右侧固定四个按钮：会话历史、会话状态、开启新会话（提交 `/new`）、设置。右侧会话状态面板默认收起，由 `会话状态` 按钮切换显隐，展开后 32 列并带 `[收起]`；只有一套宽屏布局；顶部入口点击后焦点回到输入框，草稿不丢失。
`设置` 打开弹窗，可切换诊断日志显示；`关闭` 或 Esc 返回对话，详细规则见 [仓库事实](../docs/architecture/facts.md) 第 7 节。
输入接受后立即显示消息与处理提示。模型等待、工具、TODO、后台进度、审批和结束摘要进入同一会话。
Hook 只写诊断日志，具体规则见 [仓库事实](../docs/architecture/facts.md) 第 7 节。
Lead 正文在文字片段到达时增量显示；子 agent 与记忆、目标等内部调用仍使用完整响应。
工具名称、关键参数和短结果默认可见，点击「详情」展开完整参数与输出；工具行不判断成功或失败，返回结果原样透传。模型、记忆等无参数无结果的状态行不显示空详情块，模型行只留状态与耗时。
审批结果就地更新审批卡（`已批准`/`已拒绝`），不单独占一行；未开启长期记忆时不显示 `记忆检查`/`记忆整理` 行。
同次模型调用的工具归在同一调用外框，默认收起为一行 `运行中 <本次操作> · 已耗时 Ns`，完整操作原样透传不截断；单击展开后先显示 `已运行的命令` 分组头，再逐条列出 `已运行 <本次操作>`，内容过多时在组内滚动；全文展开区也有独立滚动条。
鼠标滚轮先移动所在的内部区域，到边界后继续移动外层会话。审批按钮位于组外的内联条目。
恢复先挂载最近一段记录，点击「加载更早记录」继续查看；分组使用完整关联，显示上限不删除保存记录。
点击会话区后用 Home/End、方向键、PageUp/PageDown 或鼠标滚轮查看历史。
主动上翻时新记录不强制滚到底部；首次有效审批会滚到可操作的位置。

输入框支持中文和多行粘贴。Enter 发送，Shift+Enter 换行；点击「发送」或按 Ctrl+Enter 同样提交。
部分终端不能区分 Ctrl+Enter，此时使用发送按钮。空白提交会忽略。
运行期间只保留当前草稿，不排队第二条请求；忙碌时提交被拒绝，草稿保留。
输入框上方显示运行时状态（就绪/处理中/关闭中），下方左侧是权限摘要、右侧是模型名、消息数量和字符预算。

**命令补全：**在单行开头输入 `/`，输入框上方会显示可用命令、中文说明和用法。
继续输入前缀过滤，↑/↓ 循环选择，Enter 执行，Tab 只填入命令名，Esc 隐藏菜单并保留草稿。
下一次编辑文本会重新显示候选。点击候选与 Enter 相同。完整命令及参数可直接按 Enter 提交。
`/delete` 缺少会话 ID 时只填入并提示，不提交；填写 ID 后再按 Enter。
未知命令按 Enter 提示，Ctrl+Enter 或发送按钮可提交 slash 开头的原文，不会替你选候选。
多行内容、选区、光标离开末尾、执行中和弹窗期间不接管编辑键。
高度小于 16 行时缩小输入框并隐藏提示行、状态行和底部摘要，保留发送/停止按钮和输入框。

| 操作     | 快捷键 / 按钮                   | 行为                                            |
| -------- | ------------------------------- | ----------------------------------------------- |
| 发送     | Enter / Ctrl+Enter / 发送       | 提交消息或本地命令，接受后清空输入              |
| 换行     | Shift+Enter                     | 在输入框内插入换行                              |
| 命令选择 | ↑/↓、Enter、Tab、Esc / 点击候选 | 导航、执行、仅补全、隐藏；候选打开时 Enter 执行 |
| 清空草稿 | Ctrl+L                          | 只清空输入框                                    |
| 会话列表 | Ctrl+R                          | 空闲时打开会话列表                              |
| 停止回合 | Ctrl+C / 停止                   | 请求停止当前回合，保留已有结果和输入草稿        |
| 退出     | Ctrl+D                          | 请求关闭，等待保存与资源清理后退出              |

**审批：**Bash 和策略要求确认的 MCP 工具在主对话显示来源、工具名和可展开的完整参数。
按钮启用后选择「批准」或「拒绝」；Ctrl+C 停止回合，Ctrl+D 退出，两者都会解除审批等待。

**流式与恢复：**断流、停止或重试的临时正文会标明「本次输出未采用」，不会存为正式回复。
停止在下一网络读取/处理安全点生效，仍受请求超时限制。停止后不会自动重新请求模型。
真实工具结果逐项显示与保存，供模型的结果消息仍整批追加；中断恢复使用已保存结果，只为缺失项补中断信息。
后台事件即时显示，到下一安全检查点保存；显示完成不等于已经落盘。CLI 使用同一摘要，失败尝试以追加提示区分。
前台 Lead 及其同步 once 子 Agent 可以申请确认；后台 Lead、persistent 子 Agent、workflow 和
无交互上下文的调用会直接拒绝需要确认的工具。文件路径、拒绝列表和工具白名单仍先行检查。

**目标与会话：**下列命令在 CLI/TUI 中由同一个运行时处理，忙碌时拒绝新命令。

| 命令                | 行为                                                       |
| ------------------- | ---------------------------------------------------------- |
| `/goal`             | 查看目标状态，不调用主模型                                 |
| `/goal <condition>` | 设置目标，并把条件交给模型执行                             |
| `/goal clear`       | 清除目标                                                   |
| `/compact`          | 手动压缩当前会话上下文：归档并生成摘要，不进入模型回合循环 |
| `/resume`           | 列出会话；TUI 中弹出列表，选中会话并确认即可恢复           |
| `/resume <id>`      | 恢复指定会话的上下文，包括 goal、任务、工作目录和 TODO     |
| `/new`              | 保存当前会话并在同一启动目录开启干净会话，旧会话仍可恢复   |
| `/delete <id>`      | 删除非活动会话；列表也提供删除按钮                         |
| `/quit`             | 空闲时请求退出；执行期间使用 Ctrl+D                        |

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
/home/gaogao/miniconda3/envs/learncc/bin/python -m pytest -q tests/test_interaction.py tests/test_interactive_runtime.py tests/test_tui_integration.py tests/test_conversation.py
```

当前界面、展示与恢复事实见 [仓库事实](../docs/architecture/facts.md) 第 6 节。
当前已完成离线集成验证及 Linux PTY 的中文多行粘贴、发送和退出验证；真实 API 的 TUI 交互尚未实测。

## 二、离线自检：改完先跑这个

[scripts/debug_pass.py](../scripts/debug_pass.py) 一条命令覆盖主要机制，全离线。检查状态建在
临时目录，不碰仓库；架构图检查按设计重新生成 `docs/architecture/`。**退出码 = 失败数**，0 表示全通过。

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
| 10  | 架构依赖图             | 扫描 `src/` 得到节点与依赖，报告循环依赖，并刷新 `docs/architecture/`               |

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
[PASS] architecture dependency graph :: 12 nodes, 48 edges, 1 cycles -> docs/architecture/
10/10 passed
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
   （`.vscode/settings.json` 设为默认，`.vscode/launch.json` 的调试配置也显式使用该解释器，不会用 base）。
4. **按 F5**，选「Agent 交互（真实 API）」。断点直接打在 `src/agent_harness/` 的代码行上。

### 已准备的调试配置

`.vscode/launch.json`：

| 配置名                           | 用途                                                                 |
| -------------------------------- | -------------------------------------------------------------------- |
| Agent 交互（模拟 API，离线）     | 手动加入下方配置后可用；输入“你好”看普通回答，输入“写文件”看工具回环 |
| Agent 交互（真实 API）           | 主路径。集成终端可输入                                               |
| Agent 交互（外部终端，输入更稳） | 同上，改用外部终端；输入/方向键异常时用这个                          |
| Workflow demo（离线，不联网）    | 调试统一子 agent 工作流，fake client，不花额度                       |
| 端到端自检（离线）               | 调试 `scripts/debug_pass.py`                                         |
| pytest（当前文件）               | 在当前打开的测试文件上起断点                                         |

配置以 [`.vscode/launch.json`](../.vscode/launch.json) 为准。**本次只在笔记提供配置，不自动修改该文件。**
下面给出包含模拟交互入口的配置示例；你可以手动替换整个文件，也可以只把第一个配置对象加入现有 `configurations` 数组，保留自己的其他配置。
后面的 15 个观察点复用这些配置，只需按说明选择入口或替换测试路径。

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Agent 交互（模拟 API，离线）",
      "type": "debugpy",
      "request": "launch",
      "python": "/home/gaogao/miniconda3/envs/learncc/bin/python",
      "program": "${workspaceFolder}/scripts/debug_chat.py",
      "cwd": "${workspaceFolder}",
      "console": "integratedTerminal",
      "justMyCode": true,
      "pythonArgs": ["-Xfrozen_modules=off"]
    },
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
`args` 中的测试路径换成给出的路径；其他字段不用改。当前配置已指定 goal 测试，若要运行当前打开的测试文件，改回 `["-q", "${file}"]`。
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

**launch 配置：** 选择「Agent 交互（模拟 API，离线）」，入口是 `scripts/debug_chat.py`，终端是 `integratedTerminal`。
当前 [launch.json](../.vscode/launch.json) 已有该配置；需要手动替换时用上方 JSON。

**先调试脚本启动。** 点击源码行左侧设置下面三个断点，然后按 F5：

| 顺序 | 断点                                               | 如何操作                                                                                |
| ---- | -------------------------------------------------- | --------------------------------------------------------------------------------------- |
| 1    | [debug_chat.py:85](../scripts/debug_chat.py#L85)   | 创建模拟客户端；按两次 F10，查看 `client` 和 `client.messages`，后者应为 `ChatMessages` |
| 2    | [debug_chat.py:89](../scripts/debug_chat.py#L89)   | 将模拟客户端交给项目；按一次 F10，查看 `core.client is client`，应为 `True`             |
| 3    | [debug_chat.py:104](../scripts/debug_chat.py#L104) | 启动真实 CLI 和共享驱动；按 F5 继续，等待终端输入提示符                                 |

断点停在该行**执行之前**；查看赋值结果时先按 F10。启动阶段还没有用户输入，不会命中下面的模型断点。

**再调试一轮“你好”。** 预先设置下面的断点，程序运行到提示符后，在**集成终端**输入 `你好` 并回车。
下表按实际命中顺序排列，每看完一个位置按 F5 前往下一个：

| 顺序 | 断点                                                     | 看什么                                                                                                       |
| ---- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 1    | [cli.py:217](../src/agent_harness/frontends/cli.py#L217) | `text == "你好"`，CLI 将输入交给共享驱动                                                                     |
| 2    | [host.py:256](../src/agent_harness/host.py#L256)         | 进入主循环；`active_request == "你好"`，`messages[-1]` 为用户消息                                            |
| 3    | [host.py:297](../src/agent_harness/host.py#L297)         | 请求模型之前；查看 `context`、`tools` 和 `messages`；上下文细节见第 2 节                                     |
| 4    | [debug_chat.py:26](../scripts/debug_chat.py#L26)         | 模拟模型收到请求；看 `kwargs["system"]`、`kwargs["messages"]`、`kwargs["tools"]`；F10 后 `content == "你好"` |
| 5    | [debug_chat.py:64](../scripts/debug_chat.py#L64)         | 模拟流已经组装好；查看 `response.content` 和 `events`，继续后由真实流式消费器读取                            |
| 6    | [host.py:348](../src/agent_harness/host.py#L348)         | 模型响应已写回历史；`response.stop_reason == "end_turn"`，继续后走普通回答结束分支                           |

模拟响应在 [debug_chat.py:33](../scripts/debug_chat.py#L33) 生成；想看生成过程，可从第 26 行用 F10 走到这里。
想追踪流式入口，可另在 [debug_chat.py:42](../scripts/debug_chat.py#L42) 下断点，它先于第 26 行命中。
主循环和模型断点位于 `interactive-lead` 工作线程，CLI 输入断点位于主线程；在调用堆栈中查看当前线程。

F10 单步，F11 进入函数，Shift+F11 返回，F5 继续。暂时不看的断点可在“断点”面板取消勾选；输入 `q` 退出。

要调试工具调用，继续输入 `写文件`，另在 [lead_tools.py:61](../src/agent_harness/agents/lead_tools.py#L61)（工具检查）
和 [lead_tools.py:92](../src/agent_harness/agents/lead_tools.py#L92)（执行工具）下断点。
它会两次命中模拟模型：先返回 `tool_use`，再接收 `tool_result` 并回答；`你好` 不会命中工具断点。

#### 1.1 一轮对话的完整流程（真实 API）

**启动前检查：**在 WSL 的 `learncc` 环境中打开 Remote-WSL 窗口，确认项目根目录存在 `.env`，并只检查密钥是否存在，
`.env` 由 [core.py:29](../src/agent_harness/core.py#L29) 自动加载，
会覆盖 `launch.json` 中同名的 `env` 值。

协议和端点规则见 [llm_client.py:34](../src/agent_harness/providers/llm_client.py#L34)。


**launch 配置：** 选择「Agent 交互（真实 API）」，入口是 `agent_harness` 模块，终端使用`integratedTerminal`。

**启动阶段断点：** 首次只设置下面三个断点，确认配置后再继续；

| 顺序 | 断点                                           | 看什么                                                                                     |
| ---- | ---------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 1    | [core.py:29](../src/agent_harness/core.py#L29) | `.env` 加载入口；确认断点命中后按 F10，继续到客户端初始化                                  |
| 2    | [core.py:39](../src/agent_harness/core.py#L39) | `client` 创建；默认应为 Anthropic SDK 客户端，`LLM_API=responses` 时应为 `ResponsesClient` |
| 3    | [core.py:40](../src/agent_harness/core.py#L40) | `MODEL` 已从 `MODEL_ID` 读取；不要在 Variables 面板展开环境变量集合                        |

**输入“你好”的真实请求路径：**断点按下面顺序命中。网络等待期间不要反复按 F10，按 F5 交给
网络请求继续；如果需要停止，使用 CLI 的 Ctrl+C 或调试器停止按钮。

| 顺序 | 断点                                                                                   | 看什么                                                                                                        |
| ---- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| 1    | [cli.py:217](../src/agent_harness/frontends/cli.py#L217)                               | 输入已提交，`text == "你好"`；CLI 将请求交给共享运行时                                                        |
| 2    | [interactive_runtime.py:292](../src/agent_harness/runtime/interactive_runtime.py#L292) | 工作线程调用 `host.agent_loop`；当前回合已经进入真实执行路径                                                  |
| 3    | [host.py:286](../src/agent_harness/host.py#L286)                                       | 构造本轮 `RuntimeContext`；`memory_catalog` 和 `memories` 通常为空                                            |
| 4    | [host.py:297](../src/agent_harness/host.py#L297)                                       | `call_llm` 即将发起请求；检查 `state.current_model`、`context`、`tools` 和 `messages`，不要复制完整请求到日志 |
| 5    | [host.py:198](../src/agent_harness/host.py#L198)                                       | Lead 使用流式入口 `stream_message`；普通 CLI 回合会走这个分支                                                 |
| 6    | [host.py:185](../src/agent_harness/host.py#L185)                                       | `on_text` 收到真实模型文本增量；不要对每个增量逐步执行，按 F5 查看最终输出即可                                |
| 7    | [host.py:348](../src/agent_harness/host.py#L348)                                       | 完整响应已回填历史且没有工具调用；继续后会执行收尾                                                            |
| 8    | [host.py:383](../src/agent_harness/host.py#L383)                                       | 调用记忆收尾；`AGENT_MEMORY_ENABLED=0` 时立即跳过，不会追加模型请求                                           |
| 9    | [host.py:388](../src/agent_harness/host.py#L388)                                       | 本轮返回 `completed`；终端显示完整回答并回到提示符                                                            |

**按协议查看网络边界：**不要进入第三方 SDK 的内部实现；只在项目自己的适配层观察请求和响应。

- **Anthropic Messages（默认）：**在 [llm_client.py:502](../src/agent_harness/providers/llm_client.py#L502)
  观察 `client.messages.stream(**request)` 的调用边界。请求体由 SDK 发送，调试器里只查看
  `request` 的模型、消息数量和工具数量，不展开客户端认证字段。
- **Responses API：**在 [llm_client.py:494](../src/agent_harness/providers/llm_client.py#L494)
  确认进入适配器，在 [llm_client.py:413](../src/agent_harness/providers/llm_client.py#L413)
  查看转换后的流式请求，在 [llm_client.py:421](../src/agent_harness/providers/llm_client.py#L421)
  查看网络边界；收到文本事件时命中 [llm_client.py:424](../src/agent_harness/providers/llm_client.py#L424)，
  完成事件时命中 [llm_client.py:431](../src/agent_harness/providers/llm_client.py#L431)。

**预期结果：**输入“你好”通常只产生一次 Lead 模型请求；若触发重试、输出续写、目标检查或工具调用，
请求次数会增加。流式文本先通过 `on_text` 显示，完整响应确认后才写入会话历史。退出后可在
`~/.agent-harness/sessions/` 查看会话文件；运行时目录仍位于启动目录。真实请求失败时，优先查看
终端中的 HTTP 状态、端点和模型名，不要输出 API key；重试与降级由 [host.py:208](../src/agent_harness/host.py#L208)
统一处理。

**安全收尾：**按 `q` 或 Ctrl+D 退出，确认回到 shell 后再按 Shift+F5 停止调试。若测试使用了临时的
`.env`，调试结束后恢复原配置；不要提交 `.env`、会话文件或包含真实请求内容的调试日志。

#### 2. 上下文如何刷新

这里的“上下文”特指 **每次主模型请求前构造的 `RuntimeContext` 快照**，不是全部对话历史。
`messages` 保存对话历史，`tools` 保存工具定义；`call_llm` 将它们和快照生成的 `system` 一起交给模型。

主循环在 [host.py:286](../src/agent_harness/host.py#L286) 调用 `build_context`，
实现位于 [runtime_context.py:31](../src/agent_harness/runtime_context.py#L31)。
继续使用「Agent 交互（模拟 API，离线）」，输入 `你好`，在这一行断住。
先用 F11 进入函数，按下面的顺序查看；返回主循环后查看 `context`：

| 顺序 | 函数或数据              | 调用身份    | 使用位置                                                             | 实现或定义位置                                                               | 作用                                 |
| ---- | ----------------------- | ----------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------ |
| 1    | `subagent.active_names` | 内部·跨文件 | [runtime_context.py:38](../src/agent_harness/runtime_context.py#L38) | [subagent.py:171](../src/agent_harness/agents/subagent.py#L171)              | 加锁读取活跃子 agent 名称            |
| 2    | `read_memory_index`     | 内部·跨文件 | [runtime_context.py:41](../src/agent_harness/runtime_context.py#L41) | [memory_runtime.py:202](../src/agent_harness/context/memory_runtime.py#L202) | 读取记忆索引                         |
| 3    | `load_memories`         | 内部·跨文件 | [runtime_context.py:42](../src/agent_harness/runtime_context.py#L42) | [memory_runtime.py:380](../src/agent_harness/context/memory_runtime.py#L380) | 加载相关记忆，具体选取流程见第 14 节 |
| 4    | `mcp_tools.mcp_clients` | 共享数据    | [runtime_context.py:43](../src/agent_harness/runtime_context.py#L43) | [mcp_tools.py:55](../src/agent_harness/tools/mcp_tools.py#L55)               | 读取已登记的 MCP 名称                |

**首次输入“你好”时的预期值：**

| 字段               | 含义                    | 模拟调试初始值             |
| ------------------ | ----------------------- | -------------------------- |
| `memory_catalog`   | 长期记忆索引            | `""`，模拟脚本关闭长期记忆 |
| `memories`         | 当前请求召回的记忆正文  | `""`                       |
| `connected_mcp`    | 已连接的 MCP 服务名称   | `()`，尚未连接服务         |
| `active_subagents` | 当前活跃的子 agent 名称 | `()`，尚未启动子 agent     |

这些空值正常：输入“你好”不会自动创建 MCP、记忆或子 agent。
`RuntimeContext` 是冻结的数据类，名称复制为元组；后续登记表变化不会修改已经生成的快照。
`build_context` 读取当前状态，连接 MCP 和启动子 agent 由对应工具负责。

上下文构造完成后，主循环进入 `call_llm`，再按下面的顺序使用它：

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                  | 实现位置                                                             | 作用                                      |
| ---- | ------------------------ | ----------- | ------------------------------------------------ | -------------------------------------------------------------------- | ----------------------------------------- |
| 5    | `prompt_values`          | 内部·跨文件 | [host.py:177](../src/agent_harness/host.py#L177) | [runtime_context.py:17](../src/agent_harness/runtime_context.py#L17) | 将上下文字段交给提示词组装函数            |
| 6    | `assemble_system_prompt` | 内部·跨文件 | [host.py:177](../src/agent_harness/host.py#L177) | [prompts.py:73](../src/agent_harness/context/prompts.py#L73)         | 把记忆、MCP 和子 agent 信息拼进系统提示词 |

在 `assemble_system_prompt` 返回后查看 `system`：空的四类动态段落不加入提示词，固定规则、启动目录、时间和 skill 目录仍在。

每次循环都会重新构造上下文，包括同一个用户回合里的第二次模型调用。
输入 `写文件` 会两次命中 `build_context`，第二次的 `messages` 包含工具请求和结果；四个快照字段可能仍为空，因为写文件没有改变对应状态。

要观察字段实际变化，选择「pytest（当前文件）」，将 `args` 设置为：

```json
["-q", "tests/test_runtime_setup.py::test_agent_refreshes_memory_mcp_and_subagents_after_tool_round"]
```

这个离线测试显式启用记忆，并模拟记忆召回、MCP 连接和子 agent 登记。
在 [host.py:286](../src/agent_harness/host.py#L286) 和 [host.py:297](../src/agent_harness/host.py#L297) 下断点，比较两次 `context`、`tools` 和系统提示词。
首次尚无 MCP / 子 agent，第二次出现 `docs` / `alice`，并包含新增记忆索引与召回正文。
测试中的模型用 `messages.create` 返回固定结果，不走模拟交互脚本的流式入口。

#### 3. goal 的 Stop 判定

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_goal.py::test_unmet_goal_continues_automatically_until_achieved"`。
测试会替换评估器；下面后两步是实际评估器的路径。
这是主循环在停止边界自动执行的内部评估。评估器会请求模型，但自身未登记为模型工具。

| 顺序 | 函数                                 | 调用身份        | 调用 / 触发位置                                                           | 实现位置                                                | 作用                           |
| ---- | ------------------------------------ | --------------- | ------------------------------------------------------------------------- | ------------------------------------------------------- | ------------------------------ |
| 1    | `evaluate_goal`                      | 内部·同文件     | [host.py:356](../src/agent_harness/host.py#L356)                          | [host.py:211](../src/agent_harness/host.py#L211)        | 主模型不再请求工具时检查目标   |
| 2    | `GoalController.evaluate_after_turn` | 内部·跨文件     | [host.py:225](../src/agent_harness/host.py#L225)                          | [goal.py:358](../src/agent_harness/agents/goal.py#L358) | 根据活动目标决定是否评估       |
| 3    | `PromptGoalEvaluator.evaluate`       | 内部·同文件     | [goal.py:386](../src/agent_harness/agents/goal.py#L386)                   | [goal.py:186](../src/agent_harness/agents/goal.py#L186) | 启动独立目标评估               |
| 4    | `_evaluate_sync`                     | 线程任务·同文件 | [goal.py:201](../src/agent_harness/agents/goal.py#L201)（to_thread 目标） | [goal.py:204](../src/agent_harness/agents/goal.py#L204) | 在线程中请求评估模型并解析判断 |

判定为 `block` 时，主循环在 [host.py:369](../src/agent_harness/host.py#L369) 追加继续提示；判定为 `achieved` 时，在 [goal.py:418](../src/agent_harness/agents/goal.py#L418) 清空活动目标。

#### 4. 系统提示词怎么拼出来

选择「端到端自检（离线）」，在 [host.py:177](../src/agent_harness/host.py#L177) 下断点。

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                              | 实现位置                                                             | 作用                     |
| ---- | ------------------------ | ----------- | ------------------------------------------------------------ | -------------------------------------------------------------------- | ------------------------ |
| 1    | `prompt_values`          | 内部·跨文件 | [host.py:177](../src/agent_harness/host.py#L177)             | [runtime_context.py:17](../src/agent_harness/runtime_context.py#L17) | 取出本轮上下文字段       |
| 2    | `assemble_system_prompt` | 内部·跨文件 | [host.py:177](../src/agent_harness/host.py#L177)             | [prompts.py:73](../src/agent_harness/context/prompts.py#L73)         | 拼接固定规则和本轮上下文 |
| 3    | `list_skills`            | 内部·跨文件 | [prompts.py:89](../src/agent_harness/context/prompts.py#L89) | [skills.py:80](../src/agent_harness/context/skills.py#L80)           | 加入可用 skill 目录      |

拼接完成后，在 [host.py:199](../src/agent_harness/host.py#L199) 发起模型请求。

#### 5. 工具调用与结果

选择「端到端自检（离线）」，在 [lead_tools.py:92](../src/agent_harness/agents/lead_tools.py#L92) 下断点。以 `write_file` 为例：

| 顺序 | 函数                                 | 调用身份                 | 调用 / 触发位置                                                                                                                                                     | 实现位置                                                                   | 作用                                          |
| ---- | ------------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------- |
| 1    | `execute_tool` → `call_tool_handler` | 内部·跨文件              | [lead_tools.py:92](../src/agent_harness/agents/lead_tools.py#L92) → [tool_execution.py:40](../src/agent_harness/runtime/tool_execution.py#L40)                      | [tool_execution.py:15](../src/agent_harness/runtime/tool_execution.py#L15) | 通过统一执行入口取得并调用处理器              |
| 2    | `bind_file_tools.write`              | 工具处理器：`write_file` | 绑定 [shared_tools.py:105](../src/agent_harness/tools/shared_tools.py#L105) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [file_tools.py:254](../src/agent_harness/tools/file_tools.py#L254)         | Lead 与 child 都使用按 owner 绑定的写文件闭包 |
| 3    | `tool_cwd`                           | 内部·同文件              | [file_tools.py:241](../src/agent_harness/tools/file_tools.py#L241)                                                                                                  | [file_tools.py:226](../src/agent_harness/tools/file_tools.py#L226)         | 每次执行时取得当前任务目录                    |
| 4    | `assignment_cwd`                     | 内部·跨文件              | [file_tools.py:229](../src/agent_harness/tools/file_tools.py#L229)                                                                                                  | [tasks.py:508](../src/agent_harness/workspace/tasks.py#L508)               | 读取任务分配及 worktree 目录                  |
| 5    | `run_write`                          | 内部·同文件              | [file_tools.py:255](../src/agent_harness/tools/file_tools.py#L255) → [file_tools.py:242](../src/agent_harness/tools/file_tools.py#L242)                             | [file_tools.py:169](../src/agent_harness/tools/file_tools.py#L169)         | 通过 invoke 传入 cwd 并写入文件               |
| 6    | `safe_path`                          | 内部·同文件              | [file_tools.py:176](../src/agent_harness/tools/file_tools.py#L176)                                                                                                  | [file_tools.py:27](../src/agent_harness/tools/file_tools.py#L27)           | 检查文件路径是否在允许目录内                  |
| 7    | `build_user_content`                 | 内部·同文件              | [host.py:394](../src/agent_harness/host.py#L394)                                                                                                                    | [host.py:122](../src/agent_harness/host.py#L122)                           | 把执行结果写回历史，供下一次模型调用读取      |

执行前后的权限与 hooks 见第 10 节。

**调试 Todo 更新：** 选择「pytest（当前文件）」，将 `args` 改为：

```json
["-q", "tests/test_conversation.py::test_model_owns_todo_updates_without_cross_round_reminders"]
```

在 [run_todo_write](../src/agent_harness/tools/file_tools.py#L295) 和 [工具结果回填](../src/agent_harness/host.py#L394) 下断点，观察模型提交的 `todos`、工具返回的清单统计及下一次请求的 `messages`。
这个离线测试跨六个模型轮次执行普通工具，再由模拟模型明确更新和清空 Todo。完整职责及失败边界见 [仓库事实](../docs/architecture/facts.md) 第 8 节。

#### 6. 工具池装配

选择「端到端自检（离线）」，在 [host.py:288](../src/agent_harness/host.py#L288) 下断点。
启动时由 `initialize_runtime` 在 [host.py:89](../src/agent_harness/host.py#L89) 登记工具来源，之后每次循环按顺序装配。
表中“登记 → 执行”表示通过变量间接调用。工具来源回调提供工具定义，不是 LLM 可请求的工具。

| 顺序 | 函数                    | 调用身份     | 调用 / 触发位置                                                                                                                        | 实现位置                                                               | 作用                             |
| ---- | ----------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | -------------------------------- |
| 1    | `assemble_tool_pool`    | 内部·同文件  | [host.py:308](../src/agent_harness/host.py#L308)                                                                                       | [host.py:45](../src/agent_harness/host.py#L45)                         | 装配本轮工具                     |
| 2    | `ToolRegistry.snapshot` | 内部·跨文件  | [host.py:49](../src/agent_harness/host.py#L49)                                                                                         | [tool_registry.py:36](../src/agent_harness/tools/tool_registry.py#L36) | 统一收集工具描述、处理函数和权限 |
| 3    | `builtin_definitions`   | 工具来源回调 | 登记 [host.py:90](../src/agent_harness/host.py#L90) → [tool_registry.py:44](../src/agent_harness/tools/tool_registry.py#L44)（source） | [registrations.py:316](../src/agent_harness/registrations.py#L316)     | 读取内置工具定义                 |
| 4    | `discover_tools`        | 工具来源回调 | 登记 [host.py:91](../src/agent_harness/host.py#L91) → [tool_registry.py:44](../src/agent_harness/tools/tool_registry.py#L44)（source） | [mcp_tools.py:148](../src/agent_harness/tools/mcp_tools.py#L148)       | 读取已连接 MCP 的工具定义        |

注册器在同一调用位置依次执行各来源；Workflow 来源定义在 [host.py:92](../src/agent_harness/host.py#L92)。
`compact` 由主循环单独处理，见[运行相关](#运行相关)。

#### 7. 任务生命周期

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_subagents.py::test_task_lifecycle_and_assignment_release"`。

| 顺序 | 函数                             | 调用身份                    | 调用 / 触发位置                                                                                                                                                     | 实现位置                                                             | 作用                                |
| ---- | -------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------- |
| 1    | `run_create_task`                | 工具处理器：`create_task`   | 登记 [registrations.py:182](../src/agent_harness/registrations.py#L182) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler）     | [registrations.py:54](../src/agent_harness/registrations.py#L54)     | 分发创建任务请求，格式化结果        |
| 1.1  | `create_task`                    | 工具底层·跨文件             | [registrations.py:64](../src/agent_harness/registrations.py#L64)                                                                                                    | [tasks.py:178](../src/agent_harness/workspace/tasks.py#L178)         | 创建 pending 任务                   |
| 2    | `shared_tools.run_claim_task`    | 工具处理器：`claim_task`    | 绑定 [shared_tools.py:109](../src/agent_harness/tools/shared_tools.py#L109) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [shared_tools.py:86](../src/agent_harness/tools/shared_tools.py#L86) | 按 owner 认领任务                   |
| 2.1  | `claim_task`                     | 工具底层·跨文件             | [shared_tools.py:88](../src/agent_harness/tools/shared_tools.py#L88) → [shared_tools.py:79](../src/agent_harness/tools/shared_tools.py#L79)                         | [tasks.py:408](../src/agent_harness/workspace/tasks.py#L408)         | 校验任务并登记 owner 和工作目录     |
| 3    | `shared_tools.run_complete_task` | 工具处理器：`complete_task` | 绑定 [shared_tools.py:110](../src/agent_harness/tools/shared_tools.py#L110) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [shared_tools.py:91](../src/agent_harness/tools/shared_tools.py#L91) | 按 owner 完成任务                   |
| 3.1  | `complete_task`                  | 工具底层·跨文件             | [shared_tools.py:93](../src/agent_harness/tools/shared_tools.py#L93) → [shared_tools.py:79](../src/agent_harness/tools/shared_tools.py#L79)                         | [tasks.py:451](../src/agent_harness/workspace/tasks.py#L451)         | 把 in_progress 任务标记为 completed |
| 4    | `release_completed_assignment`   | 内部·跨文件                 | [lead_turn.py:129](../src/agent_harness/agents/lead_turn.py#L129)                                                                                                   | [tasks.py:552](../src/agent_harness/workspace/tasks.py#L552)         | 本轮结束时释放已完成任务的分配记录  |

创建、认领、完成分别由模型请求对应工具触发；分配通常在本轮结束时释放，
Lead 成功移除自己的已完成 worktree 时也会提前释放，见第 8 节。
表中的 owner 是 Lead（`agent`）。子 agent 使用相同处理器，绑定自己的 owner，复用 `tasks.py` 的业务函数。
测试会直接调用业务函数。

#### 8. 任务与 worktree 绑定

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_subagents.py::test_cross_process_claim_waits_for_worktree_binding"`。
测试使用临时 Git 仓库，子进程中的认领默认不进入父进程断点。

| 顺序 | 函数                          | 调用身份                      | 调用 / 触发位置                                                                                                                                                     | 实现位置                                                                     | 作用                                  |
| ---- | ----------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------- |
| 1    | `run_create_worktree`         | 工具处理器：`create_worktree` | 登记 [registrations.py:290](../src/agent_harness/registrations.py#L290) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler）     | [registrations.py:25](../src/agent_harness/registrations.py#L25)             | 分发创建任务工作目录的请求            |
| 1.1  | `create_worktree`             | 工具底层·跨文件               | [registrations.py:34](../src/agent_harness/registrations.py#L34)                                                                                                    | [task_worktrees.py:14](../src/agent_harness/workspace/task_worktrees.py#L14) | 检查任务处于 pending 且未认领         |
| 2    | `run_git`                     | 内部·跨文件                   | [task_worktrees.py:67](../src/agent_harness/workspace/task_worktrees.py#L67)                                                                                        | [worktrees.py:109](../src/agent_harness/workspace/worktrees.py#L109)         | 执行 git worktree add，创建目录和分支 |
| 3    | `save_task`                   | 内部·跨文件                   | [task_worktrees.py:92](../src/agent_harness/workspace/task_worktrees.py#L92)                                                                                        | [tasks.py:280](../src/agent_harness/workspace/tasks.py#L280)                 | 把 worktree 名称绑定到任务            |
| 4    | `shared_tools.run_claim_task` | 工具处理器：`claim_task`      | 绑定 [shared_tools.py:109](../src/agent_harness/tools/shared_tools.py#L109) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [shared_tools.py:86](../src/agent_harness/tools/shared_tools.py#L86)         | 按 owner 认领任务                     |
| 4.1  | `claim_task`                  | 工具底层·跨文件               | [shared_tools.py:88](../src/agent_harness/tools/shared_tools.py#L88) → [shared_tools.py:79](../src/agent_harness/tools/shared_tools.py#L79)                         | [tasks.py:408](../src/agent_harness/workspace/tasks.py#L408)                 | 校验任务并登记 owner 和工作目录       |

任务完成后，Lead 可请求 `remove_worktree` 工具。它拒绝未提交、未跟踪和被忽略的文件，
也拒绝子 agent占用或运行中的后台命令。Git 移除成功后，才释放 Lead 自己匹配的已完成分配并解除绑定；
清理拒绝或 Git 失败时保留原分配。移除保留分支，不执行合并。
离线观察同一轮完成、清理和目录切换，运行
`"tests/test_worktree_cleanup.py::test_lead_completes_and_removes_in_one_model_round"`。

| 顺序 | 函数                           | 调用身份                      | 调用 / 触发位置                                                                                                                                                 | 实现位置                                                                       | 作用                                         |
| ---- | ------------------------------ | ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | -------------------------------------------- |
| 1    | `run_remove_worktree`          | 工具处理器：`remove_worktree` | 登记 [registrations.py:300](../src/agent_harness/registrations.py#L300) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [registrations.py:40](../src/agent_harness/registrations.py#L40)               | 处理 Lead 的非强制清理请求                   |
| 1.1  | `remove_worktree`              | 工具底层·跨文件               | [registrations.py:47](../src/agent_harness/registrations.py#L47)                                                                                                | [task_worktrees.py:102](../src/agent_harness/workspace/task_worktrees.py#L102) | 检查任务、占用和未提交改动                   |
| 2    | `run_git`                      | 内部·跨文件                   | [task_worktrees.py:164](../src/agent_harness/workspace/task_worktrees.py#L164)                                                                                  | [worktrees.py:109](../src/agent_harness/workspace/worktrees.py#L109)           | 执行 git worktree remove，保留分支           |
| 3    | `release_completed_assignment` | 内部·跨文件                   | [task_worktrees.py:172](../src/agent_harness/workspace/task_worktrees.py#L172)                                                                                  | [tasks.py:552](../src/agent_harness/workspace/tasks.py#L552)                   | 移除成功后释放 Lead 自己的已完成分配（如有） |
| 4    | `save_task`                    | 内部·跨文件                   | [task_worktrees.py:175](../src/agent_harness/workspace/task_worktrees.py#L175)                                                                                  | [tasks.py:280](../src/agent_harness/workspace/tasks.py#L280)                   | 清除任务中的 worktree 绑定                   |

#### 9. 统一子 agent 生命周期与协议

离线调试选择「pytest（当前文件）」，运行 `"tests/test_subagent_lifetimes.py"`；该文件用 fake 模型和真实线程验证两种生命周期。

Lead 模型选择是否委派及 mode。once 同步交付最终结果；persistent 立即交付 started，保留同一历史等待下一次委派，只在当前进程内存活。子 agent 不具备递归创建或 worktree 创建/移除工具。

本节用 `description` 标识短任务。身份、工具白名单、双预算与结果查询的当前契约统一见
[仓库事实第 4 节](../docs/architecture/facts.md#4-统一子-agent2026-10-14)。调试时观察 `agent_id`、`rounds`、`tool_calls` 与 `usage`；persistent 的 started 是执行开始前的零快照。

**创建与共享执行：**

| 顺序 | 函数                           | 调用身份                   | 调用 / 触发位置                                                                 | 实现位置                                                                        | 作用                                             |
| ---- | ------------------------------ | -------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------ |
| 1    | `run_spawn_subagent`           | 工具处理器：spawn_subagent | [registrations.py:249](../src/agent_harness/registrations.py#L249)              | [registrations.py:121](../src/agent_harness/registrations.py#L121)              | 把模型参数交给统一运行时，序列化结果             |
| 2    | `spawn_subagent`               | 工具底层·跨文件            | [registrations.py:126](../src/agent_harness/registrations.py#L126)              | [subagent.py:627](../src/agent_harness/agents/subagent.py#L627)                 | 验证、登记、可选认领；按 mode 同步执行或启动线程 |
| 2.1  | `claim_task`                   | 内部·跨文件                | [subagent.py:694](../src/agent_harness/agents/subagent.py#L694)                 | [tasks.py:408](../src/agent_harness/workspace/tasks.py#L408)                    | 只有显式指定初始 task_id 才认领                  |
| 3    | `create_subagent_session`      | 内部·同文件                | [subagent.py:734](../src/agent_harness/agents/subagent.py#L734)                 | [subagent.py:382](../src/agent_harness/agents/subagent.py#L382)                 | 为两种模式构造相同的模型执行器                   |
| 4    | `_execute_work`                | 内部·同文件                | [subagent.py:755](../src/agent_harness/agents/subagent.py#L755)                 | [subagent.py:519](../src/agent_harness/agents/subagent.py#L519)                 | 重置工作预算，执行至最终回答或终止边界           |
| 5    | `SubagentSession.run`          | 内部·跨文件                | [subagent.py:542](../src/agent_harness/agents/subagent.py#L542)                 | [subagent_session.py:192](../src/agent_harness/agents/subagent_session.py#L192) | 整理当前委派历史，请求模型并回填整批工具结果     |
| 5.1  | `SubagentSession._request`     | 内部·同文件                | [subagent_session.py:219](../src/agent_harness/agents/subagent_session.py#L219) | [subagent_session.py:294](../src/agent_harness/agents/subagent_session.py#L294) | 观察实际模型请求、预算和累计用量                 |
| 6    | `SubagentSession.execute_tool` | 内部·同文件                | [subagent_session.py:254](../src/agent_harness/agents/subagent_session.py#L254) | [subagent_session.py:163](../src/agent_harness/agents/subagent_session.py#L163) | 检查工具预算、白名单、归档路径、计划门禁和 hooks |
| 7    | `get_subagent_output`          | Lead 查询工具              | [registrations.py:147](../src/agent_harness/registrations.py#L147)              | [subagent.py:280](../src/agent_harness/agents/subagent.py#L280)                 | 即时查看或等待结果；不请求模型、不消费信箱       |

**持久实例、通信与收尾：**

| 顺序 | 函数                           | 调用身份                   | 调用 / 触发位置                                                        | 实现位置                                                        | 作用                                           |
| ---- | ------------------------------ | -------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------- | ---------------------------------------------- |
| 1    | `_persistent_loop`             | 线程入口·跨回合            | [subagent.py:745](../src/agent_harness/agents/subagent.py#L745)        | [subagent.py:564](../src/agent_harness/agents/subagent.py#L564) | 每次委派共用 _execute_work，终态结果经信箱交付 |
| 2    | `send_message`                 | Lead 工具底层 / 子模型回调 | [subagent.py:390](../src/agent_harness/agents/subagent.py#L390)        | [subagent.py:436](../src/agent_harness/agents/subagent.py#L436) | 按名称或 ID 解析接收者并投递消息               |
| 3    | `_receive`                     | 内部·同文件                | [subagent.py:534](../src/agent_harness/agents/subagent.py#L534)        | [subagent.py:469](../src/agent_harness/agents/subagent.py#L469) | 在模型回合之间消费消息与协议                   |
| 4    | `release_completed_assignment` | 内部·跨文件                | [subagent.py:590](../src/agent_harness/agents/subagent.py#L590)        | [tasks.py:552](../src/agent_harness/workspace/tasks.py#L552)    | 释放已完成任务目录，保留实例和历史             |
| 5    | `_wait_for_work`               | 内部·同文件                | [subagent.py:603](../src/agent_harness/agents/subagent.py#L603)        | [subagent.py:511](../src/agent_harness/agents/subagent.py#L511) | IDLE 只等消息或关闭；不扫描任务板              |
| 6    | `_cleanup`                     | 内部·同文件                | [subagent.py:619](../src/agent_harness/agents/subagent.py#L619)        | [subagent.py:552](../src/agent_harness/agents/subagent.py#L552) | 实例退出时清理登记、协议和任务分配             |
| 7    | `consume_lead_inbox`           | 内部·跨文件                | [turn_runner.py:195](../src/agent_harness/runtime/turn_runner.py#L195) | [subagent.py:311](../src/agent_harness/agents/subagent.py#L311) | 共享驱动在后台唤醒回合收取子 agent 结果        |

任务 A 完成后先汇报并释放分配，persistent 实例继续等待。Lead 发来 B 的 ID 和说明后，子模型显式 claim_task 才切换目录。任务未完成的最终文本只表示等待指导，仍占用原任务，不能认领 B。无 Task 的调查直接使用启动目录。

**计划审批与关闭：**

| 顺序 | 函数                     | 调用身份                          | 调用 / 触发位置                                                    | 实现位置                                                        | 作用                                     |
| ---- | ------------------------ | --------------------------------- | ------------------------------------------------------------------ | --------------------------------------------------------------- | ---------------------------------------- |
| 1    | `submit_plan`            | 子模型工具回调                    | [subagent.py:391](../src/agent_harness/agents/subagent.py#L391)    | [subagent.py:785](../src/agent_harness/agents/subagent.py#L785) | 创建当前任务与版本对应的审批请求         |
| 2    | `run_review_plan`        | Lead 工具处理器：review_plan      | [registrations.py:308](../src/agent_harness/registrations.py#L308) | [subagent.py:854](../src/agent_harness/agents/subagent.py#L854) | 按 request_id 审核                       |
| 3    | `apply_plan_response`    | 内部·同文件                       | [subagent.py:486](../src/agent_harness/agents/subagent.py#L486)    | [subagent.py:398](../src/agent_harness/agents/subagent.py#L398) | 收信后校验身份、请求、任务版本并更新门禁 |
| 4    | `run_request_shutdown`   | Lead 工具处理器：request_shutdown | [registrations.py:293](../src/agent_harness/registrations.py#L293) | [subagent.py:813](../src/agent_harness/agents/subagent.py#L813) | 按名称或 ID 向持久实例发出关闭请求       |
| 5    | `apply_shutdown_request` | 内部·同文件                       | [subagent.py:476](../src/agent_harness/agents/subagent.py#L476)    | [subagent.py:419](../src/agent_harness/agents/subagent.py#L419) | 只接受有效关闭请求，确认后退出并清理     |

计划被拒绝必须重新提交，旧审批不能解锁新任务。once 不提供消息或计划工具，且拒绝 require_plan=True。

协议状态与转换由 [subagent_protocol.py](../src/agent_harness/agents/subagent_protocol.py) 唯一维护。subagent 仍负责线程与投递，任务身份通过 [tasks.py:55](../src/agent_harness/workspace/tasks.py#L55) 一致读取。审批决定与收到回复后的门禁分别维护。

#### 9.1 真实 API 演练：once 子 agent


**启动前检查：**在 Remote-WSL 窗口中打开项目，确认解释器为
`/home/gaogao/miniconda3/envs/learncc/bin/python`，项目根目录存在 `.env`，
**launch 配置：**选择「Agent 交互（真实 API）」，"module": "agent_harness",
**启动命令（没有现成配置时）：**在 WSL 的 `learncc` 环境，从项目根目录执行：
```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
PYTHONPATH=src /home/gaogao/miniconda3/envs/learncc/bin/python -m agent_harness
```

**准备断点：**下面列出一次委派的主要观察点；实际命中顺序和次数以模型选择的调用路径为准。设置断点后按 F5，断点停在对应语句执行前，需要查看赋值结果时先按 F10。

| 顺序 | 断点                                                                            | 看什么                                                                                  |
| ---- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| 1    | [registrations.py:121](../src/agent_harness/registrations.py#L121)              | Lead 工具处理器收到 `spawn_subagent` 参数，确认 `name`、`mode` 和委派文本已经传入运行时 |
| 2    | [subagent.py:627](../src/agent_harness/agents/subagent.py#L627)                 | 创建 once 子 agent；查看 description、身份、工具名单与双预算                            |
| 3    | [subagent.py:382](../src/agent_harness/agents/subagent.py#L382)                 | 构造独立 SubagentSession，查看 system、历史和可用工具                                   |
| 4    | [subagent.py:519](../src/agent_harness/agents/subagent.py#L519)                 | 在当前工具调用内同步执行工作单元，观察计数重置                                          |
| 5    | [subagent_session.py:192](../src/agent_harness/agents/subagent_session.py#L192) | 整理当前委派历史并开始模型回合                                                          |
| 6    | [subagent_session.py:329](../src/agent_harness/agents/subagent_session.py#L329) | 发起模型请求；查看 system 和工具数量，不展开认证字段                                    |
| 7    | [file_tools.py:152](../src/agent_harness/tools/file_tools.py#L152)              | 子 agent 读取 README.md；查看 Lead 委派的任务和实际读取内容                              |
| 8    | [subagent_session.py:305](../src/agent_harness/agents/subagent_session.py#L305) | 整批工具结果回填历史，后续请求受工作预算约束                                            |
| 9    | [subagent.py:759](../src/agent_harness/agents/subagent.py#L759)                 | 保存最终结果，Lead 收到相同结构并继续自己的模型回合                                     |

**输入请求：**程序出现 `agent-harness >>` 提示符后，在集成终端输入：

```text
请安排一个助手阅读项目根目录的 README.md，帮我整理这个项目的用途、启动方式和常用交互命令。等它完成后，你再给我一份简短的中文说明，只需要总结，不用修改文件。
```

这是一次有明确交付结果的阅读任务。用户只描述任务与期望结果，子 agent 的名称、生命周期、工具和预算由 Lead 选择。
本节观察 Lead 是否使用 once 完成委派，以及子 agent 的结果如何返回并用于最终回答；自然语言请求不保证模型一定选择 once。

#### 10. 权限与 hooks

选择「pytest（当前文件）」，分别运行
`"tests/test_permissions.py::test_file_paths_must_stay_inside_the_workspace"` 和
`"tests/test_permissions.py::test_hooks_return_the_first_non_none_result"`。

| 顺序 | 函数                           | 调用身份    | 调用 / 触发位置                                                                                                                                    | 实现位置                                                                   | 作用                                               |
| ---- | ------------------------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------- |
| 1    | `check_tool_use`               | 内部·跨文件 | [lead_tools.py:61](../src/agent_harness/agents/lead_tools.py#L61)；[subagent_session.py:185](../src/agent_harness/agents/subagent_session.py#L185) | [hooks.py:68](../src/agent_harness/runtime/hooks.py#L68)                   | 授权、前置 hook 和停止判断                         |
| 2    | `trigger_hooks（PreToolUse）`  | 内部·同文件 | [hooks.py:68](../src/agent_harness/runtime/hooks.py#L68)                                                                                           | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31)                   | 执行登记回调，取首个非 None 结果                   |
| 3    | `permission_hook`              | hook 回调   | [hooks.py:152](../src/agent_harness/runtime/hooks.py#L152)                                                                                         | [hooks.py:88](../src/agent_harness/runtime/hooks.py#L88)                   | 检查命令、文件路径和 MCP 权限                      |
| 4    | `execute_tool`                 | 内部·跨文件 | [lead_tools.py:92](../src/agent_harness/agents/lead_tools.py#L92)；[subagent_session.py:190](../src/agent_harness/agents/subagent_session.py#L190) | [tool_execution.py:32](../src/agent_harness/runtime/tool_execution.py#L32) | 执行 handler，返回正文与明确状态                   |
| 5    | `trigger_hooks（PostToolUse）` | 内部·同文件 | [tool_execution.py:44](../src/agent_harness/runtime/tool_execution.py#L44)                                                                         | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31)                   | handler 返回后触发一次；后置处理结束才发布完成状态 |
| 6    | `trigger_hooks（Stop）`        | 内部·跨文件 | [host.py:352](../src/agent_harness/host.py#L352)                                                                                                   | [hooks.py:31](../src/agent_harness/runtime/hooks.py#L31)                   | 最终回答边界触发                                   |

UserPromptSubmit 在用户输入边界触发。上表按边界汇总分支，不表示每轮都会依次执行全部事件。
hooks 是运行时回调，不是模型工具。Lead 和 child 共用前置检查及 tool_execution 执行结算；child 先检查工具资格与计划授权。授权和 hook 只有 None 表示放行，空字符串、False、0 都拒绝。
Lead 停止优先于前置拒绝；拒绝或停止不执行 handler/PostToolUse。compact 保留特殊路径，后台的 PostToolUse 由 scheduler 在实际完成后触发。

#### 11. 后台任务 / cron

选择「pytest（当前文件）」，分别运行
`"tests/test_scheduler.py::test_completed_result_is_collected_once"` 和
`"tests/test_scheduler.py::test_cron_tools_schedule_list_and_cancel"`。
真实时钟触发需在交互会话中创建定时任务并等待。
后台 bash 由模型设置 `run_in_background=true` 请求，后台线程和通知收取由程序管理。
cron 的创建、查询、取消属于模型工具；到期检查由调度线程自动执行。

**后台 bash：**

| 顺序 | 函数                              | 调用身份      | 调用 / 触发位置                                                                                                                                            | 实现位置                                                                                                                              | 作用                                     |
| ---- | --------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| 1    | `should_run_background`           | 内部·跨文件   | [lead_tools.py:67](../src/agent_harness/agents/lead_tools.py#L67)                                                                                          | [scheduler.py:39](../src/agent_harness/runtime/scheduler.py#L39)                                                                      | Lead 工具批次检查 bash 的后台标志        |
| 2    | `start_background_task`           | 内部·跨文件   | [lead_tools.py:74](../src/agent_harness/agents/lead_tools.py#L74)                                                                                          | [scheduler.py:51](../src/agent_harness/runtime/scheduler.py#L51)                                                                      | 登记后台任务并启动线程                   |
| 3    | `run_worker` → `worker`           | 线程入口·局部 | [scheduler.py:116](../src/agent_harness/runtime/scheduler.py#L116)（线程目标）→ [scheduler.py:118](../src/agent_harness/runtime/scheduler.py#L118)（启动） | [scheduler.py:108](../src/agent_harness/runtime/scheduler.py#L108) → [scheduler.py:64](../src/agent_harness/runtime/scheduler.py#L64) | 绑定执行上下文，再执行命令并发布后台状态 |
| 4    | `inject_background_notifications` | 内部·同文件   | [host.py:276](../src/agent_harness/host.py#L276)                                                                                                           | [host.py:143](../src/agent_harness/host.py#L143)                                                                                      | 后续循环中收取已完成通知                 |
| 5    | `collect_background_results`      | 内部·跨文件   | [host.py:153](../src/agent_harness/host.py#L153)                                                                                                           | [scheduler.py:141](../src/agent_harness/runtime/scheduler.py#L141)                                                                    | 保存长输出并交付通知，成功后清理记录     |

**cron 创建：**

| 顺序 | 函数                | 调用身份                    | 调用 / 触发位置                                                                                                                                                 | 实现位置                                                           | 作用                            |
| ---- | ------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------- |
| 1    | `run_schedule_cron` | 工具处理器：`schedule_cron` | 登记 [registrations.py:222](../src/agent_harness/registrations.py#L222) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [scheduler.py:501](../src/agent_harness/runtime/scheduler.py#L501) | 通过工具分发接收定时请求        |
| 2    | `schedule_job`      | 内部·同文件                 | [scheduler.py:508](../src/agent_harness/runtime/scheduler.py#L508)                                                                                              | [scheduler.py:357](../src/agent_harness/runtime/scheduler.py#L357) | 登记任务，按 durable 设置持久化 |
| 3    | `validate_cron`     | 内部·同文件                 | [scheduler.py:366](../src/agent_harness/runtime/scheduler.py#L366)                                                                                              | [scheduler.py:302](../src/agent_harness/runtime/scheduler.py#L302) | 校验时间表达式                  |

**cron 到期触发：**

| 顺序 | 函数                    | 调用身份        | 调用 / 触发位置                                                                | 实现位置                                                           | 作用                               |
| ---- | ----------------------- | --------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------ | ---------------------------------- |
| 1    | `cron_scheduler_loop`   | 线程入口·同文件 | [scheduler.py:568](../src/agent_harness/runtime/scheduler.py#L568)（线程启动） | [scheduler.py:429](../src/agent_harness/runtime/scheduler.py#L429) | 服务启动后在线程中持续检查到期任务 |
| 2    | `cron_matches`          | 内部·同文件     | [scheduler.py:443](../src/agent_harness/runtime/scheduler.py#L443)             | [scheduler.py:242](../src/agent_harness/runtime/scheduler.py#L242) | 检查当前时间是否匹配               |
| 3    | `_enqueue_due_job`      | 内部·同文件     | [scheduler.py:444](../src/agent_harness/runtime/scheduler.py#L444)             | [scheduler.py:411](../src/agent_harness/runtime/scheduler.py#L411) | 把到期任务放入队列                 |
| 4    | `consume_cron_queue`    | 内部·跨文件     | [lead_turn.py:89](../src/agent_harness/agents/lead_turn.py#L89)                | [scheduler.py:450](../src/agent_harness/runtime/scheduler.py#L450) | LeadTurn 登记到期任务并注入历史    |
| 5    | `call_llm`              | 内部·同文件     | [host.py:297](../src/agent_harness/host.py#L297)                               | [host.py:164](../src/agent_harness/host.py#L164)                   | 把定时提示词交给模型               |
| 6    | `acknowledge_cron_jobs` | 内部·跨文件     | [lead_turn.py:109](../src/agent_harness/agents/lead_turn.py#L109)              | [scheduler.py:461](../src/agent_harness/runtime/scheduler.py#L461) | 模型响应成功后确认交付             |

**查看与取消是另外两个工具入口：**

| 顺序 | 函数              | 调用身份                  | 调用 / 触发位置                                                                                                                                                 | 实现位置                                                           | 作用             |
| ---- | ----------------- | ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------- |
| 1    | `run_list_crons`  | 工具处理器：`list_crons`  | 登记 [registrations.py:225](../src/agent_harness/registrations.py#L225) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [scheduler.py:514](../src/agent_harness/runtime/scheduler.py#L514) | 查看当前定时任务 |
| 2    | `run_cancel_cron` | 工具处理器：`cancel_cron` | 登记 [registrations.py:230](../src/agent_harness/registrations.py#L230) → [tool_execution.py:24](../src/agent_harness/runtime/tool_execution.py#L24)（handler） | [scheduler.py:530](../src/agent_harness/runtime/scheduler.py#L530) | 接收取消请求     |
| 3    | `cancel_job`      | 内部·同文件               | [scheduler.py:536](../src/agent_harness/runtime/scheduler.py#L536)                                                                                              | [scheduler.py:388](../src/agent_harness/runtime/scheduler.py#L388) | 删除对应任务     |

#### 12. 工作流单次 agent 调用

选择「Workflow demo（离线，不联网）」。demo 和交互都经过统一 once 子 agent；demo 注入 DemoModelClient，运行记录写入启动目录的 .runtime/。

交互中主模型调用 `Workflow` 工具，处理器 [run_workflow_sync](../src/agent_harness/workflows/workflow_runtime.py#L1026) 在 [host.py:93](../src/agent_harness/host.py#L93) 登记。
工作流脚本调用 `ctx.agent`；步骤顺序与并行由脚本决定，步骤内使用哪些工具由子模型决定。
默认提供 bash、读写、编辑和 glob；`tools=[]` 关闭工具，非空名单限制可用工具。

| 顺序 | 函数                           | 调用身份           | 调用 / 触发位置                                                                    | 实现位置                                                                           | 作用                                         |
| ---- | ------------------------------ | ------------------ | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------------------------------------- |
| 1    | `ExecutionState.agent`         | 工作流 API（协程） | [workflow_demo.py:111](../src/agent_harness/workflows/workflow_demo.py#L111)       | [workflow_runtime.py:613](../src/agent_harness/workflows/workflow_runtime.py#L613) | 接收步骤指令和工具配置                       |
| 2    | `WorkflowJournal.key`          | 内部·同文件        | [workflow_runtime.py:627](../src/agent_harness/workflows/workflow_runtime.py#L627) | [workflow_runtime.py:400](../src/agent_harness/workflows/workflow_runtime.py#L400) | 用指令、标签、schema、工具配置生成缓存键     |
| 3    | `_run_agent`                   | 内部·同文件        | [workflow_runtime.py:629](../src/agent_harness/workflows/workflow_runtime.py#L629) | [workflow_runtime.py:631](../src/agent_harness/workflows/workflow_runtime.py#L631) | 管理缓存、执行、格式校验和记账               |
| 4    | `WorkflowJournal.cached`       | 内部·同文件        | [workflow_runtime.py:638](../src/agent_harness/workflows/workflow_runtime.py#L638) | [workflow_runtime.py:411](../src/agent_harness/workflows/workflow_runtime.py#L411) | 命中时直接重放，不创建子 agent               |
| 5    | `Budget.remaining`             | 内部·同文件        | [workflow_runtime.py:687](../src/agent_harness/workflows/workflow_runtime.py#L687) | [workflow_runtime.py:468](../src/agent_harness/workflows/workflow_runtime.py#L468) | 检查剩余预算                                 |
| 6    | `SubagentRunner.run`           | 线程任务·跨文件    | [workflow_runtime.py:693](../src/agent_harness/workflows/workflow_runtime.py#L693) | [workflow_runtime.py:294](../src/agent_harness/workflows/workflow_runtime.py#L294) | 将步骤接入统一 once 创建入口                 |
| 7    | `spawn_subagent`               | 内部·跨文件        | [workflow_runtime.py:309](../src/agent_harness/workflows/workflow_runtime.py#L309) | [subagent.py:627](../src/agent_harness/agents/subagent.py#L627)                    | 登记步骤实例，按工具名单执行并清理           |
| 8    | `_execute_work`                | 内部·同文件        | [subagent.py:755](../src/agent_harness/agents/subagent.py#L755)                    | [subagent.py:519](../src/agent_harness/agents/subagent.py#L519)                    | 执行到最终回答或预算耗尽                     |
| 9    | `SubagentSession.run`          | 内部·跨文件        | [subagent.py:542](../src/agent_harness/agents/subagent.py#L542)                    | [subagent_session.py:192](../src/agent_harness/agents/subagent_session.py#L192)    | 压缩历史，请求模型，回填工具结果             |
| 10   | `SubagentSession.execute_tool` | 内部·同文件        | [subagent_session.py:254](../src/agent_harness/agents/subagent_session.py#L254)    | [subagent_session.py:163](../src/agent_harness/agents/subagent_session.py#L163)    | 校验工具预算、可用工具、计划门禁和权限 hooks |
| 11   | `_parse_runner_json`           | 内部·同文件        | [workflow_runtime.py:320](../src/agent_harness/workflows/workflow_runtime.py#L320) | [workflow_runtime.py:256](../src/agent_harness/workflows/workflow_runtime.py#L256) | 步骤返回后解析结构化文本                     |
| 12   | `SimpleJsonSchema.validate`    | 内部·同文件        | [workflow_runtime.py:659](../src/agent_harness/workflows/workflow_runtime.py#L659) | [workflow_runtime.py:200](../src/agent_harness/workflows/workflow_runtime.py#L200) | 校验结果；不合格时最多一次无工具格式修正     |
| 13   | `_call_runner`                 | 内部·同文件        | [workflow_runtime.py:655](../src/agent_harness/workflows/workflow_runtime.py#L655) | [workflow_runtime.py:690](../src/agent_harness/workflows/workflow_runtime.py#L690) | 每次请求立即结算；取消也等待已启动调用       |
| 14   | `Budget.add`                   | 内部·同文件        | [workflow_runtime.py:704](../src/agent_harness/workflows/workflow_runtime.py#L704) | [workflow_runtime.py:449](../src/agent_harness/workflows/workflow_runtime.py#L449) | 累计预算并检查超额                           |
| 15   | `WorkflowJournal.record`       | 内部·同文件        | [workflow_runtime.py:679](../src/agent_harness/workflows/workflow_runtime.py#L679) | [workflow_runtime.py:419](../src/agent_harness/workflows/workflow_runtime.py#L419) | 只缓存成功且格式合格的步骤                   |

格式修正只处理上次最终结果，关闭工具，文件操作不会因格式错误再执行一次。失败消费也保存在任务快照，恢复时继续计入预算。
普通失败、取消和预算超额的收尾规则见 [README](../README.md#行为要点)。
整个流程的输出在 [workflow_runtime.py:901](../src/agent_harness/workflows/workflow_runtime.py#L901) 保存，任务快照在 [workflow_runtime.py:902](../src/agent_harness/workflows/workflow_runtime.py#L902) 保存。
真实客户端由 [workflow_runtime.py:290](../src/agent_harness/workflows/workflow_runtime.py#L290) 默认读取 `core.client`；离线客户端在 [cli.py:248](../src/agent_harness/frontends/cli.py#L248) 注入。

#### 13. journal 读写与 resume

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_workflow.py::test_resume_replays_at_budget_limit_without_spending_again"`。

journal 的方法都是工作流内部调用。恢复通过 CLI 的 `workflow resume`，或 `Workflow` 工具的
`resume_from_run_id` 参数进入；LLM 不直接请求这些 journal 方法。

**首次运行：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                                    | 实现位置                                                                           | 作用                       |
| ---- | -------------------------- | ----------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------------------- |
| 1    | `WorkflowJournal.__init__` | 内部·同文件 | [workflow_runtime.py:843](../src/agent_harness/workflows/workflow_runtime.py#L843) | [workflow_runtime.py:335](../src/agent_harness/workflows/workflow_runtime.py#L335) | 创建新的 journal 文件      |
| 2    | `WorkflowJournal.cached`   | 内部·同文件 | [workflow_runtime.py:638](../src/agent_harness/workflows/workflow_runtime.py#L638) | [workflow_runtime.py:411](../src/agent_harness/workflows/workflow_runtime.py#L411) | 检查本步骤是否已有记录     |
| 3    | `WorkflowJournal.record`   | 内部·同文件 | [workflow_runtime.py:679](../src/agent_harness/workflows/workflow_runtime.py#L679) | [workflow_runtime.py:419](../src/agent_harness/workflows/workflow_runtime.py#L419) | 步骤成功后追加记录并 flush |

**恢复运行：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                                    | 实现位置                                                                           | 作用                             |
| ---- | -------------------------- | ----------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------------------------- |
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

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                              | 实现位置                                                                     | 作用                         |
| ---- | -------------------------- | ----------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------- |
| 1    | `load_memories`            | 内部·跨文件 | [runtime_context.py:42](../src/agent_harness/runtime_context.py#L42)         | [memory_runtime.py:371](../src/agent_harness/context/memory_runtime.py#L371) | 按预算加载当前请求相关的记忆 |
| 2    | `select_relevant_memories` | 内部·同文件 | [memory_runtime.py:386](../src/agent_harness/context/memory_runtime.py#L386) | [memory_runtime.py:314](../src/agent_harness/context/memory_runtime.py#L314) | 从当前记忆库筛选相关记录     |
| 3    | `list_memory_files`        | 内部·同文件 | [memory_runtime.py:323](../src/agent_harness/context/memory_runtime.py#L323) | [memory_runtime.py:228](../src/agent_harness/context/memory_runtime.py#L228) | 读取记忆目录                 |
| 4    | `recent_user_text`         | 内部·同文件 | [memory_runtime.py:325](../src/agent_harness/context/memory_runtime.py#L325) | [memory_runtime.py:286](../src/agent_harness/context/memory_runtime.py#L286) | 取得最近用户文本作为查询     |
| 5    | `extract_json_array`       | 内部·同文件 | [memory_runtime.py:350](../src/agent_harness/context/memory_runtime.py#L350) | [memory_runtime.py:273](../src/agent_harness/context/memory_runtime.py#L273) | 解析模型选中的记录下标       |
| 6    | `read_memory_file`         | 内部·同文件 | [memory_runtime.py:388](../src/agent_harness/context/memory_runtime.py#L388) | [memory_runtime.py:216](../src/agent_harness/context/memory_runtime.py#L216) | 读取选中的记忆正文           |

筛选请求失败时，在 [memory_runtime.py:368](../src/agent_harness/context/memory_runtime.py#L368) 调用关键词匹配，实现位于 [memory_runtime.py:298](../src/agent_harness/context/memory_runtime.py#L298)。

**回答结束后抽取并整理记忆：**

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                                              | 实现位置                                                                     | 作用                       |
| ---- | ------------------------ | ----------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | -------------------------- |
| 1    | `extract_memories`       | 内部·跨文件 | [host.py:111](../src/agent_harness/host.py#L111)                             | [memory_runtime.py:443](../src/agent_harness/context/memory_runtime.py#L443) | 从最近对话中抽取候选记忆   |
| 2    | `validate_memory_record` | 内部·同文件 | [memory_runtime.py:492](../src/agent_harness/context/memory_runtime.py#L492) | [memory_runtime.py:412](../src/agent_harness/context/memory_runtime.py#L412) | 检查候选记录是否合法       |
| 3    | `should_store_memory`    | 内部·同文件 | [memory_runtime.py:500](../src/agent_harness/context/memory_runtime.py#L500) | [memory_runtime.py:103](../src/agent_harness/context/memory_runtime.py#L103) | 检查是否值得保存及是否重复 |
| 4    | `write_memory_file`      | 内部·同文件 | [memory_runtime.py:502](../src/agent_harness/context/memory_runtime.py#L502) | [memory_runtime.py:149](../src/agent_harness/context/memory_runtime.py#L149) | 写入通过检查的记忆         |
| 5    | `rebuild_memory_index`   | 内部·同文件 | [memory_runtime.py:168](../src/agent_harness/context/memory_runtime.py#L168) | [memory_runtime.py:171](../src/agent_harness/context/memory_runtime.py#L171) | 更新索引                   |
| 6    | `consolidate_memories`   | 内部·跨文件 | [host.py:114](../src/agent_harness/host.py#L114)                             | [memory_runtime.py:518](../src/agent_harness/context/memory_runtime.py#L518) | 有新记忆写入时合并重复记录 |

#### 15. 启动与 `/goal` 拦截

CLI 和 Textual 都将输入交给 [InteractiveRuntime](../src/agent_harness/runtime/interactive_runtime.py)。
`/goal` 是共享驱动识别的本地命令，不是 LLM 工具。查询和清除只交付命令结果；
设置条件后继续执行关联模型回合，CLI 等到该回合结束才恢复普通输入。

| 断点 / 入口                        | 实现位置                                                                      | 观察内容                                                             |
| ---------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `CLIAdapter.run` / `pump`          | [cli.py](../src/agent_harness/frontends/cli.py)                               | 唯一输入读取者；普通输入和审批决定                                   |
| `InteractiveRuntime._run`          | [interactive_runtime.py](../src/agent_harness/runtime/interactive_runtime.py) | 启动服务，串行处理前台输入和后台唤醒                                 |
| `InteractiveRuntime._command`      | 同上                                                                          | goal 查询、清除和设置；会话列表、恢复和删除                          |
| `InteractiveRuntime._resume`       | 同上                                                                          | 按显式目标目录准备，再统一激活上下文；失败边界见 README              |
| `InteractiveRuntime._execute`      | 同上                                                                          | hook、历史更新、会话记录、调用 host 和一次性回合结束                 |
| `InteractiveRuntime._checkpoint`   | 同上                                                                          | 轮询后台事实并保存已投影的展示记录；普通事实到达即投影交付           |
| `agent_loop`                       | [host.py](../src/agent_harness/host.py)                                       | 模型、工具、停止检查、goal 和 memory 收尾                            |
| `CLIAdapter._render_events`        | [cli.py](../src/agent_harness/frontends/cli.py)                               | 输出线程展示完成后调用 `acknowledge_event()`，始终不读取输入         |
| `HarnessApp.consume`               | [tui/app.py](../src/agent_harness/frontends/tui/app.py)                       | UI 线程分批展示并确认事件，再取可交付审批                            |
| `InteractionChannel.next_approval` | [interaction.py](../src/agent_harness/runtime/interaction.py)                 | 等待前置事件展示完成；统一过滤过期会话事件和审批，后续日志不阻挡请求 |

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
  [ManualCompaction.handle_tool_use](../src/agent_harness/context/compaction.py#L632) 处理，不进 handler 表。
  所以"27 工具 / 26 handler"是正常的。
- **主动压缩每轮只执行一次**。`compact` 始终留在模型工具池；同一用户回合内重复调用只回
  “本轮已请求”提示，不再重复压缩。用户也可随时用本地命令 `/compact` 手动压缩当前会话。
  上下文超限触发的自动压缩不受此限制。
- **自动压缩按完整请求的 token 估算计算**。窗口、预留输出、压缩和记忆召回预算统一见
  [仓库事实第 11 节](../docs/architecture/facts.md#11-上下文输入预算2026-10-14)。
  单条工具结果落盘与预览按字符限制，独立于窗口；主动 `compact` 不受自动触发线限制。
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
