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
$PY -m pytest -q                          # 124 个测试
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

先点击函数链接下断点，再选择上面的现成配置。凡是下面写了测试路径的，在
[.vscode/launch.json](../.vscode/launch.json) 里找到「pytest（当前文件）」，仅把
`"args": ["-q", "${file}"]` 中的 `"${file}"` 换成给出的路径；其他字段不用改。
写了两个测试路径的，分别替换并运行一次。配置中的解释器已固定为 WSL 的 `learncc`。

#### 1. 一轮对话的完整流程

在 [host.agent_loop](../src/agent_harness/host.py#L225) 下断点。选择「端到端自检（离线）」，
无需改配置；脚本化模型会先请求工具，再收到工具结果后完成回复。

这里是一次交互的总入口。第一次停住时看 `messages`、`context`、`active_request` 和 `tools`，
可以沿下面的顺序找实现：

1. [prepare_context](../src/agent_harness/host.py#L160) 先按字符预算裁剪或压缩历史。
2. [update_context](../src/agent_harness/host.py#L138) 刷新记忆、MCP 和队友状态，随后再次调用
   [assemble_tool_pool](../src/agent_harness/mcp_tools.py#L137) 获取工具描述与 handler。
3. [call_llm](../src/agent_harness/host.py#L198) 组装系统提示词并发起模型请求；返回工具调用后，
   [call_tool_handler](../src/agent_harness/file_tools.py#L217) 执行 handler。
4. 工具结果由 `build_user_content` 配对写回 `messages`，循环再次进入 `call_llm`；模型返回文本
   后依次经过 `Stop` hooks、目标判定和记忆整理。

成功判据是模型调用恰好两次，历史里依次出现 user、assistant 工具调用、user 工具结果和 assistant 文本。

#### 2. 上下文如何刷新

在 [host.update_context](../src/agent_harness/host.py#L138) 下断点。选择「端到端自检（离线）」，
无需改配置；先看空历史，再看主循环中的刷新。

这里返回新的四项上下文：`memory_catalog`、`memories`、`connected_mcp` 和 `active_teammates`。
可以沿下面的顺序找实现：

1. [read_memory_index](../src/agent_harness/memory_runtime.py#L192) 读取记忆目录索引。
2. [load_memories](../src/agent_harness/memory_runtime.py#L351) 根据 `messages` 的最近用户文本
   选取并加载相关记录。
3. [mcp_clients](../src/agent_harness/mcp_tools.py#L49) 和 [active_teammates](../src/agent_harness/teams.py#L98)
   提供当前连接与队友名称。
4. `agent_loop` 或 `run_cli` 用 `context.update(...)` 接收结果，再交给
   [assemble_system_prompt](../src/agent_harness/prompts.py#L68) 生成下一次模型请求的提示词。

先看空历史时四项是否为空，再在工具结果写回后看 `messages` 如何影响记忆选取；运行中新增 MCP
或队友后，下一轮应能在对应字段和系统提示词中看到它们。

#### 3. goal 的 Stop 判定

在 [host.evaluate_goal](../src/agent_harness/host.py#L211) 和
[GoalController.evaluate_after_turn](../src/agent_harness/goal.py#L303) 下断点。
选择「pytest（当前文件）」，把 `"${file}"` 改为
`"tests/test_goal.py::test_unmet_goal_continues_automatically_until_achieved"`；测试模拟先阻止、再放行。

这里负责把 Stop hook 的结果交给目标控制器，并决定本轮是否结束。可以沿下面的顺序找实现：

1. `agent_loop` 在没有工具调用的 assistant 回复后进入 [evaluate_goal](../src/agent_harness/host.py#L211)。
2. `evaluate_goal` 调用 [GoalController.evaluate_after_turn](../src/agent_harness/goal.py#L303)，
   评估器返回 `allow`、`block` 或其他动作。
3. 返回 `block` 时，主循环把条件和 `decision.reason` 写成 `[Goal still active]` 用户消息，
   再次调用模型；返回 `allow` 时控制器清空 `GOAL.active` 并记录 `last_status`。

第一次评估停住时重点看 `decision.reason`、`GOAL.active.condition` 和新增历史消息。测试成功判据是
模型调用两次、评估器看到两份完整历史，且目标最终为已完成。

#### 4. 系统提示词怎么拼出来

在 [prompts.assemble_system_prompt](../src/agent_harness/prompts.py#L68) 下断点。
选择「端到端自检（离线）」，无需改配置；检查传入的 `context` 和返回值。

这里把实时上下文拼成一次模型请求的 system prompt。可以沿下面的顺序找实现：

1. `assemble_system_prompt` 读取 `context` 的 `memory_catalog`、`memories`、`connected_mcp` 和
   `active_teammates`，并先放入 [PROMPT_SECTIONS](../src/agent_harness/prompts.py#L25) 的身份、工具、
   任务、团队、记忆和压缩规则。
2. 它追加工作目录和当前时间，再调用 [list_skills](../src/agent_harness/skills.py#L75) 生成 skills
   目录；有记忆时追加记忆索引与召回记录。
3. 它从 [mcp_clients](../src/agent_harness/mcp_tools.py#L49) 读取连接中的 MCP 服务名，追加
   `Connected MCP servers` 段落。
4. 函数返回完整字符串，由 `call_llm` 传给 `core.client.messages.create(system=...)`。

断点处先对照 `context` 和返回字符串的各段；改变 MCP、队友或记忆后重新执行，确认提示词每轮
重新组装，而不是沿用上一轮字符串。

#### 5. 工具调用与结果

在 [file_tools.call_tool_handler](../src/agent_harness/file_tools.py#L217) 下断点。
选择「端到端自检（离线）」，无需改配置；会依次经过写文件、编辑和路径拒绝。

这里是同步工具的统一执行边界。可以沿下面的顺序找实现：

1. `agent_loop` 从工具池的 `handlers` 按 `block.name` 取出 handler，并把 `block.input` 传给
   `call_tool_handler`。
2. `call_tool_handler` 处理未知工具和异常，把正常返回值统一转成字符串；以文件工具为例，
   [run_agent_write](../src/agent_harness/file_tools.py#L202) 先确定任务工作目录，再调用
   [run_write](../src/agent_harness/file_tools.py#L144)。
3. 执行前由 [trigger_hooks](../src/agent_harness/hooks.py#L24) 运行 `PreToolUse`，权限拒绝时
   不进入 handler；执行后运行 `PostToolUse`。
4. `agent_loop` 用原工具调用 ID 生成 `tool_result`，写回 `messages` 后再次请求模型。

断点处重点看 `handler`、`args` 和 `name`。成功判据是文件写入、编辑和路径拒绝三条路径都各自只执行一次。

#### 6. 工具池装配

在 [mcp_tools.assemble_tool_pool](../src/agent_harness/mcp_tools.py#L137) 下断点。
选择「端到端自检（离线）」，无需改配置；对照工具 schema 与 handler 表。

这里返回两张表：`tools` 是发给模型的工具名称、说明和参数格式；`handlers` 是工具名称到 Python
函数的映射。以 `write_file` 为例，可以沿下面的路径找实现：

1. [工具 schema](../src/agent_harness/registrations.py#L118) 告诉模型可以调用 `write_file`，以及需要哪些参数。
2. [处理函数映射](../src/agent_harness/registrations.py#L270) 把 `write_file` 指向 `file_tools.run_agent_write`。
3. [run_agent_write](../src/agent_harness/file_tools.py#L202) 确定工作目录，再调用实际写文件的
   [run_write](../src/agent_harness/file_tools.py#L144)。
4. 模型发回 `write_file` 调用后，[host.agent_loop](../src/agent_harness/host.py#L225) 从 `handlers`
   取出函数，交给 [call_tool_handler](../src/agent_harness/file_tools.py#L217) 执行。

每轮重新装配工具池，是为了纳入运行中连接的 [MCP 工具](../src/agent_harness/mcp_tools.py#L146)。
`compact` 的特殊处理见下方[运行相关](#运行相关)。

#### 7. 任务生命周期

在 [tasks.create_task](../src/agent_harness/tasks.py#L96)、
[claim_task](../src/agent_harness/tasks.py#L241)、
[complete_task](../src/agent_harness/tasks.py#L271) 下断点。
选择「pytest（当前文件）」，把 `"${file}"` 改为
`"tests/test_agent_teams.py::test_task_lifecycle_and_assignment_release"`；状态写入测试临时目录。

这里观察任务从创建到释放分配的状态变化，可以沿下面的顺序找实现：

1. [create_task](../src/agent_harness/tasks.py#L96) 生成运行时 ID，写入 `.tasks/<id>.json`，初始状态为 `pending`。
2. [claim_task](../src/agent_harness/tasks.py#L241) 校验 owner 和依赖，把任务改成 `in_progress`，
   同时登记 `teammate_assignments` 和工作目录。
3. [complete_task](../src/agent_harness/tasks.py#L271) 校验认领者，把状态改成 `completed`，保留任务文件。
4. `release_completed_assignment` 清理 owner 的分配记录，让工作目录不再被占用。

依次看任务 JSON 的 `status`、`owner`、`worktree` 和 `teammate_assignments`。成功判据是任务保留为
已完成状态，但 owner 的分配记录已经释放。

#### 8. 任务与 worktree 绑定

在 [task_worktrees.create_worktree](../src/agent_harness/task_worktrees.py#L13)、
[remove_worktree](../src/agent_harness/task_worktrees.py#L92) 下断点。
选择「pytest（当前文件）」，把 `"${file}"` 改为
`"tests/test_agent_teams.py::test_cross_process_claim_waits_for_worktree_binding"`。
测试会在临时 Git 仓库操作；跨进程认领在子进程内，默认只调试父进程的绑定与移除。

这里观察任务和 Git worktree 的双向绑定，可以沿下面的顺序找实现：

1. [create_worktree](../src/agent_harness/task_worktrees.py#L13) 校验任务仍是 `pending` 且无 owner，
   检查 Git 根目录、分支名和注册表，然后执行 `git worktree add`。
2. 创建成功后把 `task.worktree` 写回任务文件；子进程的 [claim_task](../src/agent_harness/tasks.py#L241)
   会等待同一任务锁，再认领这个已绑定任务。
3. 任务完成并释放 owner 后，[remove_worktree](../src/agent_harness/task_worktrees.py#L92)
   检查任务、租约、后台命令和工作区状态。
4. 检查通过后执行 `git worktree remove`；工作目录删除，`wt/<name>` 分支保留。

断点处重点看任务的 `worktree`、`.worktrees/<name>`、Git 注册表和 owner 租约。未完成、仍被占用或
有运行中的后台命令时，移除应返回错误。

#### 9. 队友线程与协议

在 [teams.spawn_teammate_thread](../src/agent_harness/teams.py#L317)、
[apply_plan_response](../src/agent_harness/teams.py#L252) 下断点。
选择「pytest（当前文件）」，分别把 `"${file}"` 改为
`"tests/test_agent_teams.py::test_reserved_teammate_names_do_not_shadow_runtime_identities"` 和
`"tests/test_agent_teams.py::test_plan_rejection_requires_a_new_submission"`。
这只覆盖启动前校验与计划协议；真实队友线程要用「Agent 交互（真实 API）」并提出协作任务。

这里观察队友登记、计划审批和消息协议，可以沿下面的顺序找实现：

1. [spawn_teammate_thread](../src/agent_harness/teams.py#L317) 校验名称，登记 `active_teammates`、
   `plan_gates` 和 `assignment_versions`；任务认领失败时回滚这些登记。
2. 队友提交计划后写入 `pending_requests`，Lead 通过 `run_review_plan` 设置 approved 或 rejected。
3. [apply_plan_response](../src/agent_harness/teams.py#L252) 校验 sender、target、request_id、任务版本
   和状态，只有匹配当前计划才更新 `plan_gates`。
4. 计划被拒绝时队友保持等待，新计划必须生成新的 `request_id`；旧回复不能解锁工具。

先用两个 pytest 用例观察保留名称和计划拒绝，再用「Agent 交互（真实 API）」提出协作任务查看真实
队友线程的收件箱与最终回复。

#### 10. 权限与 hooks

在 [hooks.permission_hook](../src/agent_harness/hooks.py#L33)、
[trigger_hooks](../src/agent_harness/hooks.py#L24) 下断点。
选择「pytest（当前文件）」，分别把 `"${file}"` 改为
`"tests/test_permissions.py::test_file_paths_must_stay_inside_the_workspace"` 和
`"tests/test_permissions.py::test_hooks_return_the_first_non_none_result"`。

这里观察工具执行前后的 hook 链，可以沿下面的顺序找实现：

1. [trigger_hooks](../src/agent_harness/hooks.py#L24) 按注册顺序调用某个事件的回调，遇到第一个
   非 `None` 返回值就停止。
2. [permission_hook](../src/agent_harness/hooks.py#L33) 检查 shell deny list、工作区路径、MCP 策略
   和是否处于主线程；返回拒绝文本时，`agent_loop` 不再调用工具 handler。
3. 权限通过后执行工具，再触发 `PostToolUse`；`Stop` 和 `UserPromptSubmit` 也复用同一分发函数。
4. 测试中的第三个回调不会执行，因为第二个回调已经返回 `stop here`。

断点处分别观察 `event`、回调列表和 `block`。路径越界、异步确认和回调短路应分别落到对应分支。

#### 11. 后台任务 / cron

在 [scheduler.start_background_task](../src/agent_harness/scheduler.py#L40)、
[run_schedule_cron](../src/agent_harness/scheduler.py#L363) 下断点。
选择「pytest（当前文件）」，分别把 `"${file}"` 改为
`"tests/test_scheduler.py::test_completed_result_is_collected_once"` 和
`"tests/test_scheduler.py::test_cron_tools_schedule_list_and_cancel"`。
这两个测试不触发真实时钟；要看 [cron_scheduler_loop](../src/agent_harness/scheduler.py#L308)，
需用「Agent 交互（真实 API）」创建定时任务并等待。

这里包含两条独立路径，可以沿下面的顺序找实现：

1. 后台 bash 由 [start_background_task](../src/agent_harness/scheduler.py#L40) 创建 `bg_id`，登记
   `background_tasks` 后启动 worker；worker 把输出放进 `background_results` 并更新状态。
2. [collect_background_results](../src/agent_harness/scheduler.py#L93) 一次性取出 completed/failed
   任务，格式化成 `task_notification`；下一轮由 `inject_background_notifications` 写入历史。
3. cron 工具 [run_schedule_cron](../src/agent_harness/scheduler.py#L363) 校验表达式并写入
   `scheduled_jobs`；durable 任务同时写 `.scheduled_tasks.json`。
4. `run_list_crons` 展示当前任务，`run_cancel_cron` 删除任务；真实时钟场景再由
   `cron_scheduler_loop` 把到期任务放进 `cron_queue`。

后台测试应看到 `running → completed → 已收集并删除`，cron 测试应看到 schedule、list、cancel 的
状态变化；两类通知都要在模型下一轮请求中出现。

#### 12. 工作流单次 agent 调用

在 [ExecutionState.agent](../src/agent_harness/workflow.py#L520) 下断点。
选择「Workflow demo（离线，不联网）」，无需改配置；使用 mock runner，但会在启动目录写入 `.runtime/`。

这里观察一次 workflow 子 agent 的预算、校验和落盘，可以沿下面的顺序找实现：

1. [ExecutionState.agent](../src/agent_harness/workflow.py#L520) 生成 label 和 journal key，检查缓存，
   再检查 `Budget.remaining()` 与共享 agent 上限。
2. 缓存未命中时，通过 `asyncio.to_thread` 调用 mock runner；返回的 `value/tokens` 先校验 schema，
   再累计到 `task.usage` 和 `task.cumulative_usage`。
3. [WorkflowJournal.record](../src/agent_harness/workflow.py#L420) 把 key、结果和累计用量追加到 journal，
   同时更新内存 cache。
4. [run_workflow](../src/agent_harness/workflow.py#L820) 保存 task/output 快照并把状态打印到终端。

断点处重点看 `prompt`、`schema`、`label`、`Budget` 和 journal key。成功判据是 demo 完成、`.runtime/`
位于启动目录，且没有真实 API 请求；把预算调小可观察超预算异常。

#### 13. journal 读写与 resume

在 [WorkflowJournal.__init__](../src/agent_harness/workflow.py#L353)、
[record](../src/agent_harness/workflow.py#L420)、
[cached](../src/agent_harness/workflow.py#L417) 下断点。
选择「pytest（当前文件）」，把 `"${file}"` 改为
`"tests/test_workflow.py::test_resume_replays_at_budget_limit_without_spending_again"`；
写入与恢复都在同一个调试进程中。

这里观察 journal 如何把一次运行变成可恢复的缓存，可以沿下面的顺序找实现：

1. [WorkflowJournal.__init__](../src/agent_harness/workflow.py#L353) 在新运行中创建空文件，在 resume
   中逐行读取 JSON，恢复 `cache`、`historical_tokens` 和 `historical_agents`。
2. [record](../src/agent_harness/workflow.py#L420) 写入 key、value、tokens 和累计用量，并立即 flush。
3. [cached](../src/agent_harness/workflow.py#L417) 用相同语义 key 查找结果；命中时 `ExecutionState.agent`
   直接返回缓存，不调用 runner，也不增加本次运行用量。
4. 恢复第二个调用时，`Budget` 根据历史用量判断是否还能消费；超出上限会在真正调用 runner 前抛错。

断点处对照 journal 文件和 `cache`。成功判据是恢复命中返回缓存结果，报告 `agents=0 tokens=0`，
累计用量仍保持第一次运行的数值。

#### 14. memory 选取 / 抽取

在 [memory_runtime.select_relevant_memories](../src/agent_harness/memory_runtime.py#L294)、
[extract_memories](../src/agent_harness/memory_runtime.py#L437) 下断点。
选择「Agent 交互（真实 API）」，无需改配置，输入普通问题。
没有已有 `.memory/` 记录时，选取会提前返回；抽取仍会调用模型。

这里观察记忆的“先选取、后抽取、再合并”顺序，可以沿下面的顺序找实现：

1. [list_memory_files](../src/agent_harness/memory_runtime.py#L213) 读取 `.memory/` 记录；没有记录或
   没有最近用户文本时，`select_relevant_memories` 直接返回空列表。
2. 有记录时，`select_relevant_memories` 把目录做成索引 prompt，解析模型返回的 JSON 下标；调用失败
   时退回关键词排序，结果交给 `load_memories` 按字符预算读取正文。
3. 一轮结束后，[extract_memories](../src/agent_harness/memory_runtime.py#L437) 把最近对话送给模型，
   对每条候选执行 type、scope、description 和 body 校验。
4. 校验通过的记录写入文件，再由 `consolidate_memories` 合并重复记录；未经校验的模型内容不会落盘。

先在 `.memory/` 放一条记录观察选取，再观察抽取 prompt、JSON 校验和合并结果；没有已有记录时，
选取会提前返回，但抽取仍会调用模型。

#### 15. 启动与 `/goal` 拦截

在 [cli.run_cli](../src/agent_harness/cli.py#L45)、
[handle_goal_command](../src/agent_harness/cli.py#L22) 下断点。
选择「Agent 交互（真实 API）」，无需改配置。只输入 `/goal` 或 `/goal clear` 再输入
`q`，即可离线看入口；输入普通问题或 `/goal <条件>` 才会发送真实请求。

这里观察 CLI 从输入到主循环的分流，可以沿下面的顺序找实现：

1. [run_cli](../src/agent_harness/cli.py#L45) 设置 `core.CLI_ACTIVE`，启动 runtime services，创建
   `history`、`context` 和后台 `async_event_loop`。
2. 每次读取输入后先调用 [handle_goal_command](../src/agent_harness/cli.py#L22)，判断是否为 `/goal`。
3. `/goal` 只打印状态，`/goal clear` 清除目标；两者返回 `handled=True`，不会进入 `agent_loop`。
4. `/goal <条件>` 设置目标，返回 `handled=False` 和去掉命令前缀的条件文本；普通问题也返回
   `handled=False`，随后触发 `UserPromptSubmit`、追加历史并调用 `host.agent_loop`。

断点处重点看 `handled`、替换后的 `query`、`history` 和 `session_state`。成功判据是本地命令不产生
API 调用，普通问题和带条件目标各只进入一次正常主循环。

行号会随源码变化；链接不对时按函数名搜索。

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

- **`compact` 没有 handler**。它是 `BUILTIN_TOOLS` 里的 schema，由
  [ManualCompaction.handle_tool_use](../src/agent_harness/compaction.py#L530) 处理，不进 handler 表。
  所以"27 工具 / 26 handler"是正常的。
- **主动压缩每轮只执行一次**。压缩后本轮不再向模型提供 `compact`；下一条用户请求可以再次主动
  压缩。上下文超限触发的自动压缩不受此限制。
- **自动压缩按字符预算计算**。修改 [core.CONTEXT_LIMIT](../src/agent_harness/core.py#L65)（默认 50000）后，
  触发线 80%（40000）、压缩目标 70%（35000）、最新工具结果批次预算 16%（8000）、单条结果落盘
  阈值 8%（4000）和预览长度 2%（1000）会随之变化。低于触发线就停止，无须强行达到目标；主动
  `compact` 不受这些阈值限制。字符数不等于模型 token 数，系统提示词和工具定义不计入此预算。
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
