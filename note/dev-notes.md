# 开发笔记：运行与调试

> 记录时间：2026-10-04
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
$PY -m pytest -q                          # 123 个测试
PYTHONPATH=src $PY -m agent_harness workflow demo
PYTHONPATH=src $PY -m agent_harness workflow resume
$PY scripts/debug_pass.py                 # 端到端自检，9 项
```

`pytest` 和自检不需要 `PYTHONPATH`（前者由 `tests/conftest.py` 注入 `src`，后者自己插入）。

### 两者的分工

| | `pytest` | `scripts/debug_pass.py` |
| --- | --- | --- |
| 定位 | 单元与契约测试 | 子进程 / 真 Git / flat 集成自检 |
| 粒度 | 每模块一个测试文件，共 10 个 | 一条命令 9 项 |
| 覆盖 | 任务、压缩、调度、权限、编码、技能、goal、workflow、teams | 工具池、agent 回环、goal 边界、任务、workflow、worktree、flat |
| 独有 | 边界条件与错误分支 | 真起子进程跑 CLI、真建 Git 仓库、验证 flat 产物在仓库内可运行 |

改完 `src/` 后两个都要跑：先 `pytest -q`，再 `debug_pass.py`。重叠的几项（工具池、agent
回环、edit 守卫、路径逃逸、goal 边界、任务生命周期）是刻意保留的重复：`pytest` 里是隔离
单测，自检里是在真实进程边界上再确认一次。

### 真实 API 交互

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
PYTHONPATH=src /home/gaogao/miniconda3/envs/learncc/bin/python -m agent_harness
```

提示符 `agent-harness >>` 下输入一句话回车发送；`/goal` 看目标状态；`q` 退出。**每轮都消耗额度。**

装过包（`pip install -e .`）时可省掉 `PYTHONPATH`，并用命令 `agent-harness` 代替 `python -m agent_harness`。
本机**未安装**，走的是上面这条。

## 二、离线自检：改完先跑这个

[scripts/debug_pass.py](../scripts/debug_pass.py) 一条命令覆盖主要机制，全离线、不写仓库
（状态都建在临时目录）。**退出码 = 失败数**，0 表示全通过。

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
/home/gaogao/miniconda3/envs/learncc/bin/python scripts/debug_pass.py
```

### 覆盖范围

每块独立，一块失败不影响其余，最后打印报告。

| # | 检查 | 断言要点 |
| --- | --- | --- |
| 1 | 工具池装配 | 27 个工具、无重名、schema 与 handler 一一对应 |
| 2 | agent 循环工具往返 | 脚本化 client 驱动 `agent_loop`，`write_file` 真实落盘，恰好 2 次模型调用 |
| 3 | `edit_file` 误匹配防护 | `old_text` 不匹配时拒绝改写且文件不变 |
| 4 | 路径逃逸防护 | 读 `../../../etc/passwd` 被拒 |
| 5 | goal Stop 边界 | 评估器先 `block` 再放行：注入 1 条 `[Goal still active]`，循环继续，成功后清空 goal |
| 6 | 任务生命周期 | create → claim → complete → release |
| 7 | workflow 离线 | `demo` 完成，`resume` 命中 journal（agents=0 tokens=0），`.runtime/` 落在启动目录 |
| 8 | git worktree | 真实临时仓库上 create → claim → 提交 → complete → remove |
| 9 | 单文件版 | `flat/agent_full.py` 与源码拼接一致，且能独立跑 `workflow demo` |

### 实测输出

```
[PASS] tool pool assembled :: 27 tools (26 handlers + ['compact'])
[PASS] agent loop: tool call round-trip :: tool round-trip, 2 model calls
[PASS] edit_file: mismatch guarded :: mismatch refused, then applied
[PASS] read_file: path escape blocked :: path outside workdir blocked
[PASS] goal: stop boundary blocks once :: block -> continue -> allow -> cleared
[PASS] task lifecycle :: create/claim/complete/release
[PASS] workflow demo/resume offline :: demo, journal cache hit, state in launch dir
[PASS] worktree git ops :: create/claim/commit/complete/remove
[PASS] flat build freshness :: in sync with sources and runs standalone
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

| 配置名 | 用途 |
| --- | --- |
| Agent 交互（真实 API） | 主路径。集成终端可输入 |
| Agent 交互（外部终端，输入更稳） | 同上，改用外部终端；输入/方向键异常时用这个 |
| Workflow demo（离线，不联网） | 调试 workflow 运行时，mock runner，不花额度 |
| 端到端自检（离线） | 调试 `scripts/debug_pass.py` |
| pytest（当前文件） | 在当前打开的测试文件上起断点 |

### 建议的断点位置

| 想看什么 | 位置 |
| --- | --- |
| 一轮对话的完整流程 | [host.py:213](../src/agent_harness/host.py:213) `agent_loop` |
| 上下文如何刷新 | [host.py:138](../src/agent_harness/host.py:138) `update_context` |
| goal 的 Stop 判定 | [host.py:200](../src/agent_harness/host.py:200) `evaluate_goal`、[goal.py:174](../src/agent_harness/goal.py:174) `evaluate` |
| 系统提示词怎么拼出来 | [prompts.py:68](../src/agent_harness/prompts.py:68) `assemble_system_prompt` |
| 工具调用与结果 | [file_tools.py:217](../src/agent_harness/file_tools.py:217) `call_tool_handler` |
| 工具池装配 | [mcp_tools.py:137](../src/agent_harness/mcp_tools.py:137) `assemble_tool_pool` |
| 任务生命周期 | [tasks.py](../src/agent_harness/tasks.py) `create_task` / `claim_task` / `complete_task` |
| 任务与 worktree 绑定 | [task_worktrees.py:13](../src/agent_harness/task_worktrees.py:13) `create_worktree` / `remove_worktree` |
| 队友线程与协议 | [teams.py](../src/agent_harness/teams.py) `spawn_teammate_thread`、`apply_plan_response` |
| 权限与 hooks | [hooks.py](../src/agent_harness/hooks.py) `permission_hook`、`trigger_hooks` |
| 后台任务 / cron | [scheduler.py](../src/agent_harness/scheduler.py) `start_background_task`、`cron_scheduler_loop` |
| 工作流单次 agent 调用 | [workflow.py:520](../src/agent_harness/workflow.py:520) `ExecutionState.agent` |
| journal 读写与 resume | [workflow.py:349](../src/agent_harness/workflow.py:349) `WorkflowJournal` |
| memory 选取 / 抽取 | [memory_runtime.py:287](../src/agent_harness/memory_runtime.py:287)、[:393](../src/agent_harness/memory_runtime.py:393) |
| 启动与 `/goal` 拦截 | [cli.py:43](../src/agent_harness/cli.py:43) `run_cli`、[:22](../src/agent_harness/cli.py:22) `handle_goal_command` |

行号会随源码变动，对不上就按函数名搜。拆分后每个机制一个文件，先按文件名定位，再找函数。

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

- **`compact` 没有 handler**。它是 `BUILTIN_TOOLS` 里的 schema，但在 `agent_loop` 内被特判
  （`if block.name == "compact"`），不进 handler 表。所以"27 工具 / 26 handler"是正常的。
- **`goal` 的 `block` 会持续循环**，直到评估器放行或达到 `block_cap`（默认 8）。写脚本时
  评估器要能"先 block 再 allow"，否则会一直要下一条脚本回复。
- **worktree 绑定要求任务处于 `pending` 且无 owner**。已认领的任务不能再绑定新 worktree。
- **移除 worktree 前必须先完成任务**。未完成时 `remove_worktree` 返回错误而不是强删；完成后
  移除会保留分支（`wt/<name>`），只删工作目录。
- **运行目录以 `core.WORKDIR` 为准**。任务、worktree、memory、workflow、transcript 和工具结果目录
  都在调用时从它解析；实际使用仍应在目标目录启动进程。调试时不要修改旧的 `host.WORKDIR`。
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
- **`cli.py` 会起一个 daemon 线程** `async_event_loop`（cron / 队友）。断点若打在里面可能被
  非用户输入触发，排查交互流程时只打在 `agent_loop` 及其下游。
- **集成终端里方向键/中文输入偶尔异常**，改用「外部终端」那条配置。
- **别把断点打在生成物上**。`flat/agent_full.py` 是拼接产物，改了不生效；要调就调
  `src/agent_harness/`。
- 改完 `src/agent_harness/` 记得重跑 `python scripts/build_flat.py`，否则自检第 9 项会报过期。

## 五、未覆盖（需要真实 API）

自检刻意不碰这些，要手动验证：

- 真实模型下的交互问答（见「一、怎么跑」）
- 队友线程端到端协作
- memory 抽取与合并
