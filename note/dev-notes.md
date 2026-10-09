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
$PY -m pytest -q                          # 完整离线测试
PYTHONPATH=src $PY -m agent_harness workflow demo
PYTHONPATH=src $PY -m agent_harness workflow resume
$PY scripts/debug_pass.py                 # 端到端自检，9 项
```

`pytest` 和自检不需要 `PYTHONPATH`（前者由 `tests/conftest.py` 注入 `src`，后者自己插入）。

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

首次有效交互会在启动目录 `.transcripts/sessions/` 创建一个 JSON 快照。历史追加、工具结果、压缩替换和 goal 状态都会更新同一文件；退出或 Ctrl+C 不删除它。恢复前需要当前模型回合、后台任务、子 agent 和 Lead 信箱都已结束。恢复只加载历史和 goal，任务板与仍有效的 worktree 继续使用，旧线程和 MCP 连接不会恢复。

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

先点击函数链接下断点，再选择上面的现成配置。凡是下面写了测试路径的，在
[.vscode/launch.json](../.vscode/launch.json) 里找到「pytest（当前文件）」，仅把
`"args": ["-q", "${file}"]` 中的 `"${file}"` 换成给出的路径；其他字段不用改。
写了两个测试路径的，分别替换并运行一次。配置中的解释器已固定为 WSL 的 `learncc`。

表中的“调用身份”区分函数在这条路径中的用途；“同文件 / 跨文件”指调用位置与实现位置，
不表示 Python 强制限制了访问范围。LLM 请求的是工具名，Python 运行时再执行对应函数。

- **工具处理器**：已登记到工具池的入口，例如 `write_file` 对应 `run_agent_write`。
- **工具底层**：由处理器继续调用的业务函数，LLM 不直接按这个 Python 函数名请求。
- **内部 / 局部函数**：由程序直接调用；局部函数定义在另一个函数里面。
- **回调 / 线程入口 / CLI 入口**：分别由注册器、hooks、线程或命令行流程触发。
- **共享数据**：程序读取的状态，不是函数或模型工具。

基础文件函数由 Lead 和子 agent 各自的目录处理器调用；两种子 agent 模式共用同一 session。

#### 1. 一轮对话的完整流程

入口是 `agent_loop`，实现位于 [host.py:259](../src/agent_harness/host.py#L259)。
它由 CLI 或后台事件调用；本节表中的辅助函数由程序驱动，不是提供给 LLM 的工具入口。
选择「端到端自检（离线）」，在 [host.py:312](../src/agent_harness/host.py#L312) 下断点，按下面的主要调用顺序单步查看。

| 顺序 | 函数                 | 调用身份    | 调用 / 触发位置                                  | 实现位置                                                             | 作用                                                                               |
| ---- | -------------------- | ----------- | ------------------------------------------------ | -------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1    | `prepare_history`    | 内部·跨文件 | [host.py:312](../src/agent_harness/host.py#L312) | [compaction.py:44](../src/agent_harness/compaction.py#L44)           | 按预算整理或压缩历史                                                               |
| 2    | `build_context`      | 内部·跨文件 | [host.py:318](../src/agent_harness/host.py#L318) | [runtime_context.py:31](../src/agent_harness/runtime_context.py#L31) | 刷新记忆、MCP 和子 agent 信息，见第 2 节                                           |
| 3    | `assemble_tool_pool` | 内部·同文件 | [host.py:320](../src/agent_harness/host.py#L320) | [host.py:119](../src/agent_harness/host.py#L119)                     | 装配本轮工具；内部调用注册器 [snapshot](../src/agent_harness/tool_registry.py#L36) |
| 4    | `call_llm`           | 内部·同文件 | [host.py:326](../src/agent_harness/host.py#L326) | [host.py:210](../src/agent_harness/host.py#L210)                     | 拼接系统提示词并请求模型                                                           |
| 5    | `has_tool_use`       | 内部·跨文件 | [host.py:378](../src/agent_harness/host.py#L378) | [message_content.py:17](../src/agent_harness/message_content.py#L17) | 判断模型是否请求工具，决定进入下面哪个分支                                         |

**有普通同步工具请求时：**

| 顺序 | 函数                  | 调用身份    | 调用 / 触发位置                                  | 实现位置                                                     | 作用                            |
| ---- | --------------------- | ----------- | ------------------------------------------------ | ------------------------------------------------------------ | ------------------------------- |
| 6    | `trigger_hooks`       | 内部·跨文件 | [host.py:432](../src/agent_harness/host.py#L432) | [hooks.py:30](../src/agent_harness/hooks.py#L30)             | 触发 `PreToolUse`，检查能否执行 |
| 7    | `call_tool_handler`   | 内部·跨文件 | [host.py:466](../src/agent_harness/host.py#L466) | [file_tools.py:285](../src/agent_harness/file_tools.py#L285) | 执行工具                        |
| 8    | `trigger_hooks`       | 内部·跨文件 | [host.py:468](../src/agent_harness/host.py#L468) | [hooks.py:30](../src/agent_harness/hooks.py#L30)             | 触发 `PostToolUse`              |
| 9    | `build_user_content`  | 内部·同文件 | [host.py:486](../src/agent_harness/host.py#L486) | [host.py:170](../src/agent_harness/host.py#L170)             | 把工具结果和后台通知写回历史    |
| 10   | `apply_after_results` | 内部·跨文件 | [host.py:488](../src/agent_harness/host.py#L488) | [compaction.py:654](../src/agent_harness/compaction.py#L654) | 有手动压缩请求时执行压缩        |

然后回到循环顶部，再次请求模型。自检会演示：`请求写文件 → 执行 write_file → 回填结果 → 模型回答`。

**没有工具请求时：**

| 顺序 | 函数                           | 调用身份    | 调用 / 触发位置                                  | 实现位置                                           | 作用                                   |
| ---- | ------------------------------ | ----------- | ------------------------------------------------ | -------------------------------------------------- | -------------------------------------- |
| 6    | `trigger_hooks`                | 内部·跨文件 | [host.py:380](../src/agent_harness/host.py#L380) | [hooks.py:30](../src/agent_harness/hooks.py#L30)   | 触发 `Stop`                            |
| 7    | `evaluate_goal`                | 内部·同文件 | [host.py:382](../src/agent_harness/host.py#L382) | [host.py:234](../src/agent_harness/host.py#L234)   | 检查目标；判定为 `block` 就继续循环    |
| 8    | `remember_after_turn`          | 内部·同文件 | [host.py:405](../src/agent_harness/host.py#L405) | [host.py:151](../src/agent_harness/host.py#L151)   | 提取并整理记忆                         |
| 9    | `release_completed_assignment` | 内部·跨文件 | [host.py:407](../src/agent_harness/host.py#L407) | [tasks.py:485](../src/agent_harness/tasks.py#L485) | 释放已完成任务的分配记录，随后结束本轮 |

#### 2. 上下文如何刷新

主循环在 [host.py:318](../src/agent_harness/host.py#L318) 调用 `build_context`，
实现位于 [runtime_context.py:31](../src/agent_harness/runtime_context.py#L31)。
进入函数后，按下面的顺序查看：

| 顺序 | 函数或数据                  | 调用身份    | 使用位置                                                             | 实现或定义位置                                                       | 作用                          |
| ---- | --------------------------- | ----------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------- |
| 1    | `subagent.active_subagents` | 共享数据    | [runtime_context.py:39](../src/agent_harness/runtime_context.py#L39) | [subagent.py:125](../src/agent_harness/subagent.py#L125)             | 加锁读取活跃子 agent 名称     |
| 2    | `read_memory_index`         | 内部·跨文件 | [runtime_context.py:41](../src/agent_harness/runtime_context.py#L41) | [memory_runtime.py:197](../src/agent_harness/memory_runtime.py#L197) | 读取记忆索引                  |
| 3    | `load_memories`             | 内部·跨文件 | [runtime_context.py:42](../src/agent_harness/runtime_context.py#L42) | [memory_runtime.py:371](../src/agent_harness/memory_runtime.py#L371) | 加载相关记忆，具体选取流程见第 14 节 |
| 4    | `mcp_tools.mcp_clients`     | 共享数据    | [runtime_context.py:43](../src/agent_harness/runtime_context.py#L43) | [mcp_tools.py:54](../src/agent_harness/mcp_tools.py#L54)             | 读取已登记的 MCP 名称         |

上下文构造完成后，主循环进入 `call_llm`，再按下面的顺序使用它：

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                  | 实现位置                                                             | 作用                                      |
| ---- | ------------------------ | ----------- | ------------------------------------------------ | -------------------------------------------------------------------- | ----------------------------------------- |
| 5    | `prompt_values`          | 内部·跨文件 | [host.py:223](../src/agent_harness/host.py#L223) | [runtime_context.py:17](../src/agent_harness/runtime_context.py#L17) | 将上下文字段交给提示词组装函数            |
| 6    | `assemble_system_prompt` | 内部·跨文件 | [host.py:223](../src/agent_harness/host.py#L223) | [prompts.py:73](../src/agent_harness/prompts.py#L73)                 | 把记忆、MCP 和子 agent 信息拼进系统提示词 |

每次循环都会重新构造上下文。上一轮新增的 MCP 或子 agent，会在下一次构造时读到。
要观察刷新前后的区别，选择「pytest（当前文件）」，将 `"${file}"` 替换为 `"tests/test_runtime_context.py"`。

#### 3. goal 的 Stop 判定

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_goal.py::test_unmet_goal_continues_automatically_until_achieved"`。
测试会替换评估器；下面后两步是实际评估器的路径。
这是主循环在停止边界自动执行的内部评估。评估器会请求模型，但自身未登记为模型工具。

| 顺序 | 函数                                 | 调用身份        | 调用 / 触发位置                                                    | 实现位置                                         | 作用                           |
| ---- | ------------------------------------ | --------------- | ------------------------------------------------------------------ | ------------------------------------------------ | ------------------------------ |
| 1    | `evaluate_goal`                      | 内部·同文件     | [host.py:382](../src/agent_harness/host.py#L382)                   | [host.py:234](../src/agent_harness/host.py#L234) | 主模型不再请求工具时检查目标   |
| 2    | `GoalController.evaluate_after_turn` | 内部·跨文件     | [host.py:248](../src/agent_harness/host.py#L248)                   | [goal.py:356](../src/agent_harness/goal.py#L356) | 根据活动目标决定是否评估       |
| 3    | `PromptGoalEvaluator.evaluate`       | 内部·同文件     | [goal.py:384](../src/agent_harness/goal.py#L384)                   | [goal.py:184](../src/agent_harness/goal.py#L184) | 启动独立目标评估               |
| 4    | `_evaluate_sync`                     | 线程任务·同文件 | [goal.py:199](../src/agent_harness/goal.py#L199)（to_thread 目标） | [goal.py:202](../src/agent_harness/goal.py#L202) | 在线程中请求评估模型并解析判断 |

判定为 `block` 时，主循环在 [host.py:390](../src/agent_harness/host.py#L390) 追加继续提示；判定为 `achieved` 时，在 [goal.py:416](../src/agent_harness/goal.py#L416) 清空活动目标。

#### 4. 系统提示词怎么拼出来

选择「端到端自检（离线）」，在 [host.py:222](../src/agent_harness/host.py#L222) 下断点。

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                      | 实现位置                                                             | 作用                     |
| ---- | ------------------------ | ----------- | ---------------------------------------------------- | -------------------------------------------------------------------- | ------------------------ |
| 1    | `prompt_values`          | 内部·跨文件 | [host.py:223](../src/agent_harness/host.py#L223)     | [runtime_context.py:17](../src/agent_harness/runtime_context.py#L17) | 取出本轮上下文字段       |
| 2    | `assemble_system_prompt` | 内部·跨文件 | [host.py:223](../src/agent_harness/host.py#L223)     | [prompts.py:73](../src/agent_harness/prompts.py#L73)                 | 拼接固定规则和本轮上下文 |
| 3    | `list_skills`            | 内部·跨文件 | [prompts.py:89](../src/agent_harness/prompts.py#L89) | [skills.py:80](../src/agent_harness/skills.py#L80)                   | 加入可用 skill 目录      |

拼接完成后，在 [host.py:224](../src/agent_harness/host.py#L224) 发起模型请求。

#### 5. 工具调用与结果

选择「端到端自检（离线）」，在 [host.py:465](../src/agent_harness/host.py#L465) 下断点。以 `write_file` 为例：

| 顺序 | 函数                 | 调用身份                      | 调用 / 触发位置                                                                                                                                   | 实现位置                                                     | 作用                                     |
| ---- | -------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ---------------------------------------- |
| 1    | `call_tool_handler`  | 内部·跨文件                   | [host.py:466](../src/agent_harness/host.py#L466)                                                                                                  | [file_tools.py:285](../src/agent_harness/file_tools.py#L285) | 按工具名称取得的处理函数执行工具         |
| 2    | `run_agent_write`    | 工具处理器：`write_file`      | 登记 [registrations.py:192](../src/agent_harness/registrations.py#L192) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [file_tools.py:255](../src/agent_harness/file_tools.py#L255) | 根据 write_file 映射进入写文件处理器     |
| 3    | `_agent_cwd`         | 内部·同文件                   | [file_tools.py:261](../src/agent_harness/file_tools.py#L261)                                                                                      | [file_tools.py:223](../src/agent_harness/file_tools.py#L223) | 确定当前任务的工作目录                   |
| 4    | `assignment_cwd`     | 内部·跨文件                   | [file_tools.py:229](../src/agent_harness/file_tools.py#L229)                                                                                      | [tasks.py:450](../src/agent_harness/tasks.py#L450)           | 读取任务分配及 worktree 目录             |
| 5    | `run_write`          | 内部·同文件；子代理工具处理器 | [file_tools.py:262](../src/agent_harness/file_tools.py#L262)                                                                                      | [file_tools.py:167](../src/agent_harness/file_tools.py#L167) | 执行文件写入                             |
| 6    | `safe_path`          | 内部·同文件                   | [file_tools.py:174](../src/agent_harness/file_tools.py#L174)                                                                                      | [file_tools.py:26](../src/agent_harness/file_tools.py#L26)   | 检查文件路径是否在允许目录内             |
| 7    | `build_user_content` | 内部·同文件                   | [host.py:486](../src/agent_harness/host.py#L486)                                                                                                  | [host.py:170](../src/agent_harness/host.py#L170)             | 把执行结果写回历史，供下一次模型调用读取 |

执行前后的权限与 hooks 见第 10 节。

#### 6. 工具池装配

选择「端到端自检（离线）」，在 [host.py:319](../src/agent_harness/host.py#L319) 下断点。
启动时在 [host.py:111](../src/agent_harness/host.py#L111) 登记工具来源，之后每次循环按顺序装配。
表中“登记 → 执行”表示通过变量间接调用。工具来源回调提供工具定义，不是 LLM 可请求的工具。

| 顺序 | 函数                    | 调用身份     | 调用 / 触发位置                                                                                                                    | 实现位置                                                           | 作用                             |
| ---- | ----------------------- | ------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | -------------------------------- |
| 1    | `assemble_tool_pool`    | 内部·同文件  | [host.py:320](../src/agent_harness/host.py#L320)                                                                                   | [host.py:119](../src/agent_harness/host.py#L119)                   | 装配本轮工具                     |
| 2    | `ToolRegistry.snapshot` | 内部·跨文件  | [host.py:123](../src/agent_harness/host.py#L123)                                                                                   | [tool_registry.py:36](../src/agent_harness/tool_registry.py#L36)   | 统一收集工具描述、处理函数和权限 |
| 3    | `builtin_definitions`   | 工具来源回调 | 登记 [host.py:112](../src/agent_harness/host.py#L112) → [tool_registry.py:44](../src/agent_harness/tool_registry.py#L44)（source） | [registrations.py:373](../src/agent_harness/registrations.py#L373) | 读取内置工具定义                 |
| 4    | `discover_tools`        | 工具来源回调 | 登记 [host.py:113](../src/agent_harness/host.py#L113) → [tool_registry.py:44](../src/agent_harness/tool_registry.py#L44)（source） | [mcp_tools.py:147](../src/agent_harness/mcp_tools.py#L147)         | 读取已连接 MCP 的工具定义        |

注册器在同一调用位置依次执行各来源；Workflow 来源定义在 [host.py:114](../src/agent_harness/host.py#L114)。
`compact` 由主循环单独处理，见[运行相关](#运行相关)。

#### 7. 任务生命周期

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_subagents.py::test_task_lifecycle_and_assignment_release"`。

| 顺序 | 函数                           | 调用身份                    | 调用 / 触发位置                                                                                                                                   | 实现位置                                                           | 作用                               |
| ---- | ------------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------------------------- |
| 1    | `run_create_task`              | 工具处理器：`create_task`   | 登记 [registrations.py:236](../src/agent_harness/registrations.py#L236) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [registrations.py:44](../src/agent_harness/registrations.py#L44)   | 分发创建任务请求，格式化结果       |
| 1.1  | `create_task`                  | 工具底层·跨文件             | [registrations.py:54](../src/agent_harness/registrations.py#L54)                                                                                  | [tasks.py:129](../src/agent_harness/tasks.py#L129)                 | 创建 pending 任务                  |
| 2    | `run_claim_task`               | 工具处理器：`claim_task`    | 登记 [registrations.py:263](../src/agent_harness/registrations.py#L263) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [registrations.py:112](../src/agent_harness/registrations.py#L112) | 以主 agent 身份分发认领请求        |
| 2.1  | `claim_task`                   | 工具底层·跨文件             | [registrations.py:122](../src/agent_harness/registrations.py#L122)                                                                                | [tasks.py:358](../src/agent_harness/tasks.py#L358)                 | 认领任务，登记 owner 和工作目录    |
| 3    | `run_complete_task`            | 工具处理器：`complete_task` | 登记 [registrations.py:268](../src/agent_harness/registrations.py#L268) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [registrations.py:128](../src/agent_harness/registrations.py#L128) | 以主 agent 身份分发完成请求        |
| 3.1  | `complete_task`                | 工具底层·跨文件             | [registrations.py:138](../src/agent_harness/registrations.py#L138)                                                                                | [tasks.py:397](../src/agent_harness/tasks.py#L397)                 | 把任务标记为 completed             |
| 4    | `release_completed_assignment` | 内部·跨文件                 | [host.py:407](../src/agent_harness/host.py#L407)                                                                                                  | [tasks.py:485](../src/agent_harness/tasks.py#L485)                 | 本轮结束时释放已完成任务的分配记录 |

创建、认领、完成分别由模型请求对应工具触发；分配通常在本轮结束时释放，
Lead 成功移除自己的已完成 worktree 时也会提前释放，见第 8 节。
表中的处理器属于 Lead。子 agent的认领和完成工具由自己的执行器包装，复用 `tasks.py` 的业务函数。
测试会直接调用业务函数。

#### 8. 任务与 worktree 绑定

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_subagents.py::test_cross_process_claim_waits_for_worktree_binding"`。
测试使用临时 Git 仓库，子进程中的认领默认不进入父进程断点。

| 顺序 | 函数                  | 调用身份                      | 调用 / 触发位置                                                                                                                                   | 实现位置                                                           | 作用                                  |
| ---- | --------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------- |
| 1    | `run_create_worktree` | 工具处理器：`create_worktree` | 登记 [registrations.py:347](../src/agent_harness/registrations.py#L347) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [registrations.py:21](../src/agent_harness/registrations.py#L21)   | 分发创建任务工作目录的请求            |
| 1.1  | `create_worktree`     | 工具底层·跨文件               | [registrations.py:30](../src/agent_harness/registrations.py#L30)                                                                                  | [task_worktrees.py:13](../src/agent_harness/task_worktrees.py#L13) | 检查任务处于 pending 且未认领         |
| 2    | `run_git`             | 内部·跨文件                   | [task_worktrees.py:66](../src/agent_harness/task_worktrees.py#L66)                                                                                | [worktrees.py:107](../src/agent_harness/worktrees.py#L107)         | 执行 git worktree add，创建目录和分支 |
| 3    | `save_task`           | 内部·跨文件                   | [task_worktrees.py:93](../src/agent_harness/task_worktrees.py#L93)                                                                                | [tasks.py:231](../src/agent_harness/tasks.py#L231)                 | 把 worktree 名称绑定到任务            |
| 4    | `run_claim_task`      | 工具处理器：`claim_task`      | 登记 [registrations.py:263](../src/agent_harness/registrations.py#L263) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [registrations.py:112](../src/agent_harness/registrations.py#L112) | 分发主 agent 的认领请求               |
| 4.1  | `claim_task`          | 工具底层·跨文件               | [registrations.py:122](../src/agent_harness/registrations.py#L122)                                                                                | [tasks.py:358](../src/agent_harness/tasks.py#L358)                 | 认领已绑定任务，采用对应工作目录      |

任务完成后，Lead 可请求 `remove_worktree` 工具。它拒绝未提交、未跟踪和被忽略的文件，
也拒绝子 agent占用或运行中的后台命令。Git 移除成功后，才释放 Lead 自己匹配的已完成分配并解除绑定；
清理拒绝或 Git 失败时保留原分配。移除保留分支，不执行合并。
离线观察同一轮完成、清理和目录切换，运行
`"tests/test_worktree_cleanup.py::test_lead_completes_and_removes_in_one_model_round"`。

| 顺序 | 函数                           | 调用身份                      | 调用 / 触发位置                                                                                                                                   | 实现位置                                                             | 作用                                         |
| ---- | ------------------------------ | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------------------------- |
| 1    | `run_remove_worktree`          | 工具处理器：`remove_worktree` | 登记 [registrations.py:357](../src/agent_harness/registrations.py#L357) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [registrations.py:33](../src/agent_harness/registrations.py#L33)     | 处理 Lead 的非强制清理请求                   |
| 1.1  | `remove_worktree`              | 工具底层·跨文件               | [registrations.py:40](../src/agent_harness/registrations.py#L40)                                                                                  | [task_worktrees.py:103](../src/agent_harness/task_worktrees.py#L103) | 检查任务、占用和未提交改动                   |
| 2    | `run_git`                      | 内部·跨文件                   | [task_worktrees.py:165](../src/agent_harness/task_worktrees.py#L165)                                                                              | [worktrees.py:107](../src/agent_harness/worktrees.py#L107)           | 执行 git worktree remove，保留分支           |
| 3    | `release_completed_assignment` | 内部·跨文件                   | [task_worktrees.py:172](../src/agent_harness/task_worktrees.py#L172)                                                                              | [tasks.py:485](../src/agent_harness/tasks.py#L485)                   | 移除成功后释放 Lead 自己的已完成分配（如有） |
| 4    | `save_task`                    | 内部·跨文件                   | [task_worktrees.py:175](../src/agent_harness/task_worktrees.py#L175)                                                                              | [tasks.py:231](../src/agent_harness/tasks.py#L231)                   | 清除任务中的 worktree 绑定                   |

#### 9. 统一子 agent 生命周期与协议

离线调试选择「pytest（当前文件）」，运行 `"tests/test_subagent_lifetimes.py"`；该文件用 fake 模型和真实线程验证两种生命周期。

Lead 模型选择是否委派及 mode。once 同步交付最终结果；persistent 立即交付 started，保留同一历史等待下一次委派，只在当前进程内存活。子 agent 不具备递归创建或 worktree 创建/移除工具。

`spawn_subagent` 的可选 `tools` 省略时使用模式默认工具，`[]` 关闭工具。返回的 usage 累计执行与摘要请求的用量；persistent 的 started 只返回零快照。

**创建与共享执行：**

| 顺序 | 函数                           | 调用身份                   | 调用 / 触发位置                                                                                                                          | 实现位置                                                                 | 作用                                             |
| ---- | ------------------------------ | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------ |
| 1    | `run_spawn_subagent`           | 工具处理器：spawn_subagent | 登记 [registrations.py:304](../src/agent_harness/registrations.py#L304) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)   | [registrations.py:144](../src/agent_harness/registrations.py#L144)       | 把模型参数交给统一运行时，序列化结果             |
| 2    | `spawn_subagent`               | 工具底层·跨文件            | [registrations.py:147](../src/agent_harness/registrations.py#L147)                                                                       | [subagent.py:479](../src/agent_harness/subagent.py#L479)                 | 验证、登记、可选认领；按 mode 同步执行或启动线程 |
| 2.1  | `claim_task`                   | 内部·跨文件                | [subagent.py:535](../src/agent_harness/subagent.py#L535)                                                                                 | [tasks.py:358](../src/agent_harness/tasks.py#L358)                       | 只有显式指定初始 task_id 才认领                  |
| 3    | `create_subagent_session`      | 内部·同文件                | [subagent.py:561](../src/agent_harness/subagent.py#L561)                                                                                 | [subagent.py:261](../src/agent_harness/subagent.py#L261)                 | 为两种模式构造相同的模型执行器                   |
| 4    | `_execute_work`                | 内部·同文件                | [subagent.py:569](../src/agent_harness/subagent.py#L569)（once）；[subagent.py:447](../src/agent_harness/subagent.py#L447)（persistent） | [subagent.py:406](../src/agent_harness/subagent.py#L406)                 | 循环执行模型回合，直到最终回答                   |
| 5    | `SubagentSession.run`          | 内部·跨文件                | [subagent.py:417](../src/agent_harness/subagent.py#L417)                                                                                 | [subagent_session.py:223](../src/agent_harness/subagent_session.py#L223) | 按当前委派整理历史，请求模型并回填工具结果       |
| 5.1  | `SubagentSession._request`     | 内部·同文件                | [subagent_session.py:246](../src/agent_harness/subagent_session.py#L246)                                                                 | [subagent_session.py:267](../src/agent_harness/subagent_session.py#L267) | 每次请求携带当前委派，旧委派只作为历史参考       |
| 6    | `SubagentSession.execute_tool` | 内部·同文件                | [subagent_session.py:263](../src/agent_harness/subagent_session.py#L263)                                                                 | [subagent_session.py:204](../src/agent_harness/subagent_session.py#L204) | 检查计划门禁和 hooks，再执行工具                 |

**持久实例、通信与收尾：**

| 顺序 | 函数                           | 调用身份                   | 调用 / 触发位置                                                                                                                                | 实现位置                                                 | 作用                                           |
| ---- | ------------------------------ | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------------- |
| 1    | `_persistent_loop`             | 线程入口·跨回合            | [subagent.py:566](../src/agent_harness/subagent.py#L566)（target 回调）                                                                        | [subagent.py:443](../src/agent_harness/subagent.py#L443) | 每次委派共用 _execute_work，最终结果经信箱交付 |
| 2    | `send_message`                 | Lead 工具底层 / 子模型回调 | [registrations.py:158](../src/agent_harness/registrations.py#L158)（Lead）；[subagent.py:268](../src/agent_harness/subagent.py#L268)（子模型） | [subagent.py:341](../src/agent_harness/subagent.py#L341) | 是否发信及内容由发送方模型决定，程序投递       |
| 3    | `_receive`                     | 内部·同文件                | [subagent.py:412](../src/agent_harness/subagent.py#L412)                                                                                       | [subagent.py:365](../src/agent_harness/subagent.py#L365) | 在模型回合之间消费消息与协议                   |
| 4    | `release_completed_assignment` | 内部·跨文件                | [subagent.py:455](../src/agent_harness/subagent.py#L455)                                                                                       | [tasks.py:485](../src/agent_harness/tasks.py#L485)       | 最终回答后释放已完成任务目录，保留实例和历史   |
| 5    | `_wait_for_work`               | 内部·同文件                | [subagent.py:467](../src/agent_harness/subagent.py#L467)                                                                                       | [subagent.py:398](../src/agent_harness/subagent.py#L398) | IDLE 只等消息或关闭；不扫描任务板              |
| 6    | `_cleanup`                     | 内部·同文件                | [subagent.py:474](../src/agent_harness/subagent.py#L474)                                                                                       | [subagent.py:432](../src/agent_harness/subagent.py#L432) | 实例退出时清理登记、协议和任务分配             |
| 7    | `consume_lead_inbox`           | 内部·跨文件                | [host.py:518](../src/agent_harness/host.py#L518)                                                                                               | [subagent.py:192](../src/agent_harness/subagent.py#L192) | 后台事件线程收取子 agent 结果并唤醒 Lead       |

任务 A 完成后先汇报并释放分配，persistent 实例继续等待。Lead 发来 B 的 ID 和说明后，子模型显式 claim_task 才切换目录。任务未完成的最终文本只表示等待指导，仍占用原任务，不能认领 B。无 Task 的调查直接使用启动目录。

**计划审批与关闭：**

| 顺序 | 函数                     | 调用身份                          | 调用 / 触发位置                                                                                                                        | 实现位置                                                 | 作用                                     |
| ---- | ------------------------ | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------- |
| 1    | `submit_plan`            | 子模型工具回调                    | [subagent.py:269](../src/agent_harness/subagent.py#L269)                                                                               | [subagent.py:585](../src/agent_harness/subagent.py#L585) | 创建当前任务与版本对应的审批请求         |
| 2    | `run_review_plan`        | Lead 工具处理器：review_plan      | 登记 [registrations.py:334](../src/agent_harness/registrations.py#L334) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295) | [subagent.py:660](../src/agent_harness/subagent.py#L660) | 按 request_id 审核，参数没有 name        |
| 3    | `apply_plan_response`    | 内部·同文件                       | [subagent.py:380](../src/agent_harness/subagent.py#L380)                                                                               | [subagent.py:275](../src/agent_harness/subagent.py#L275) | 收信后校验身份、请求、任务版本并更新门禁 |
| 4    | `run_request_shutdown`   | Lead 工具处理器：request_shutdown | 登记 [registrations.py:319](../src/agent_harness/registrations.py#L319) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295) | [subagent.py:617](../src/agent_harness/subagent.py#L617) | 向持久实例发出关闭请求；目标参数为 name  |
| 5    | `apply_shutdown_request` | 内部·同文件                       | [subagent.py:372](../src/agent_harness/subagent.py#L372)                                                                               | [subagent.py:314](../src/agent_harness/subagent.py#L314) | 只接受有效关闭请求，确认后退出并清理     |

计划被拒绝必须重新提交，旧审批不能解锁新任务。once 不提供消息或计划工具，且拒绝 require_plan=True。


#### 10. 权限与 hooks

选择「pytest（当前文件）」，分别运行
`"tests/test_permissions.py::test_file_paths_must_stay_inside_the_workspace"` 和
`"tests/test_permissions.py::test_hooks_return_the_first_non_none_result"`。

| 顺序 | 函数                                | 调用身份    | 调用 / 触发位置                                                                                                        | 实现位置                                                     | 作用                           |
| ---- | ----------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ------------------------------ |
| 1    | `trigger_hooks（UserPromptSubmit）` | 内部·跨文件 | [cli.py:91](../src/agent_harness/cli.py#L91)                                                                           | [hooks.py:30](../src/agent_harness/hooks.py#L30)             | 用户提交问题时触发             |
| 2    | `trigger_hooks（PreToolUse）`       | 内部·跨文件 | [host.py:432](../src/agent_harness/host.py#L432)                                                                       | [hooks.py:30](../src/agent_harness/hooks.py#L30)             | 执行工具前按登记顺序检查       |
| 3    | `permission_hook`                   | hook 回调   | 登记 [hooks.py:118](../src/agent_harness/hooks.py#L118) → [hooks.py:38](../src/agent_harness/hooks.py#L38)（callback） | [hooks.py:45](../src/agent_harness/hooks.py#L45)             | 检查命令、文件路径和 MCP 权限  |
| 4    | `call_tool_handler`                 | 内部·跨文件 | [host.py:466](../src/agent_harness/host.py#L466)                                                                       | [file_tools.py:285](../src/agent_harness/file_tools.py#L285) | 权限通过后执行工具             |
| 5    | `trigger_hooks（PostToolUse）`      | 内部·跨文件 | [host.py:468](../src/agent_harness/host.py#L468)                                                                       | [hooks.py:30](../src/agent_harness/hooks.py#L30)             | 工具执行后触发                 |
| 6    | `trigger_hooks（Stop）`             | 内部·跨文件 | [host.py:380](../src/agent_harness/host.py#L380)                                                                       | [hooks.py:30](../src/agent_harness/hooks.py#L30)             | 后续模型回复不再请求工具时触发 |

上表按事件发生位置汇总不同分支的 hooks，不表示每轮都会按 1 到 6 依次执行；`UserPromptSubmit`、
工具执行前后和 `Stop` 分别对应不同的主循环边界。

hooks 是运行时回调，不是模型工具。钩子拒绝时停止后续检查，也不执行工具。
子 agent 执行器采用相同的工具 hooks。

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
| 1    | `should_run_background`           | 内部·跨文件   | [host.py:443](../src/agent_harness/host.py#L443)                                                                                           | [scheduler.py:36](../src/agent_harness/scheduler.py#L36)   | 检查 bash 的后台标志                 |
| 2    | `start_background_task`           | 内部·跨文件   | [host.py:447](../src/agent_harness/host.py#L447)                                                                                           | [scheduler.py:48](../src/agent_harness/scheduler.py#L48)   | 登记后台任务并启动线程               |
| 3    | `worker`                          | 线程入口·局部 | [scheduler.py:102](../src/agent_harness/scheduler.py#L102)（线程目标）→ [scheduler.py:104](../src/agent_harness/scheduler.py#L104)（启动） | [scheduler.py:58](../src/agent_harness/scheduler.py#L58)   | 在线程中执行命令，保存状态和输出     |
| 4    | `inject_background_notifications` | 内部·同文件   | [host.py:301](../src/agent_harness/host.py#L301)                                                                                           | [host.py:191](../src/agent_harness/host.py#L191)           | 后续循环中收取已完成通知             |
| 5    | `collect_background_results`      | 内部·跨文件   | [host.py:201](../src/agent_harness/host.py#L201)                                                                                           | [scheduler.py:114](../src/agent_harness/scheduler.py#L114) | 保存长输出并交付通知，成功后清理记录 |

**cron 创建：**

| 顺序 | 函数                | 调用身份                    | 调用 / 触发位置                                                                                                                                   | 实现位置                                                   | 作用                            |
| ---- | ------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------- |
| 1    | `run_schedule_cron` | 工具处理器：`schedule_cron` | 登记 [registrations.py:279](../src/agent_harness/registrations.py#L279) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [scheduler.py:460](../src/agent_harness/scheduler.py#L460) | 通过工具分发接收定时请求        |
| 2    | `schedule_job`      | 内部·同文件                 | [scheduler.py:467](../src/agent_harness/scheduler.py#L467)                                                                                        | [scheduler.py:316](../src/agent_harness/scheduler.py#L316) | 登记任务，按 durable 设置持久化 |
| 3    | `validate_cron`     | 内部·同文件                 | [scheduler.py:325](../src/agent_harness/scheduler.py#L325)                                                                                        | [scheduler.py:262](../src/agent_harness/scheduler.py#L262) | 校验时间表达式                  |

**cron 到期触发：**

| 顺序 | 函数                    | 调用身份        | 调用 / 触发位置                                                        | 实现位置                                                   | 作用                               |
| ---- | ----------------------- | --------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------- |
| 1    | `cron_scheduler_loop`   | 线程入口·同文件 | [scheduler.py:512](../src/agent_harness/scheduler.py#L512)（线程启动） | [scheduler.py:387](../src/agent_harness/scheduler.py#L387) | 服务启动后在线程中持续检查到期任务 |
| 2    | `cron_matches`          | 内部·同文件     | [scheduler.py:402](../src/agent_harness/scheduler.py#L402)             | [scheduler.py:202](../src/agent_harness/scheduler.py#L202) | 检查当前时间是否匹配               |
| 3    | `_enqueue_due_job`      | 内部·同文件     | [scheduler.py:403](../src/agent_harness/scheduler.py#L403)             | [scheduler.py:369](../src/agent_harness/scheduler.py#L369) | 把到期任务放入队列                 |
| 4    | `consume_cron_queue`    | 内部·跨文件     | [host.py:280](../src/agent_harness/host.py#L280)                       | [scheduler.py:409](../src/agent_harness/scheduler.py#L409) | 主循环取出到期任务并注入历史       |
| 5    | `call_llm`              | 内部·同文件     | [host.py:326](../src/agent_harness/host.py#L326)                       | [host.py:210](../src/agent_harness/host.py#L210)           | 把定时提示词交给模型               |
| 6    | `acknowledge_cron_jobs` | 内部·跨文件     | [host.py:350](../src/agent_harness/host.py#L350)                       | [scheduler.py:420](../src/agent_harness/scheduler.py#L420) | 模型响应成功后确认交付             |

**查看与取消是另外两个工具入口：**

| 顺序 | 函数              | 调用身份                  | 调用 / 触发位置                                                                                                                                   | 实现位置                                                   | 作用             |
| ---- | ----------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------- |
| 1    | `run_list_crons`  | 工具处理器：`list_crons`  | 登记 [registrations.py:282](../src/agent_harness/registrations.py#L282) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [scheduler.py:473](../src/agent_harness/scheduler.py#L473) | 查看当前定时任务 |
| 2    | `run_cancel_cron` | 工具处理器：`cancel_cron` | 登记 [registrations.py:287](../src/agent_harness/registrations.py#L287) → [file_tools.py:295](../src/agent_harness/file_tools.py#L295)（handler） | [scheduler.py:489](../src/agent_harness/scheduler.py#L489) | 接收取消请求     |
| 3    | `cancel_job`      | 内部·同文件               | [scheduler.py:495](../src/agent_harness/scheduler.py#L495)                                                                                        | [scheduler.py:347](../src/agent_harness/scheduler.py#L347) | 删除对应任务     |

#### 12. 工作流单次 agent 调用

选择「Workflow demo（离线，不联网）」。demo 和交互都经过统一 once 子 agent；demo 注入 DemoModelClient，运行记录写入启动目录的 .runtime/。

交互中主模型调用 `Workflow` 工具，处理器 [run_workflow_sync](../src/agent_harness/workflow_runtime.py#L863) 在 [host.py:115](../src/agent_harness/host.py#L115) 登记。
工作流脚本调用 `ctx.agent`；步骤顺序与并行由脚本决定，步骤内使用哪些工具由子模型决定。
默认提供 bash、读写、编辑和 glob；`tools=[]` 关闭工具，非空名单限制可用工具。

| 顺序 | 函数 | 调用身份 | 调用 / 触发位置 | 实现位置 | 作用 |
| --- | --- | --- | --- | --- | --- |
| 1 | `ExecutionState.agent` | 工作流 API（协程） | [workflow_demo.py:111](../src/agent_harness/workflow_demo.py#L111) | [workflow_runtime.py:534](../src/agent_harness/workflow_runtime.py#L534) | 接收步骤指令和工具配置 |
| 2 | `WorkflowJournal.key` | 内部·同文件 | [workflow_runtime.py:547](../src/agent_harness/workflow_runtime.py#L547) | [workflow_runtime.py:395](../src/agent_harness/workflow_runtime.py#L395) | 用指令、标签、schema、工具配置生成缓存键 |
| 3 | `_run_agent` | 内部·同文件 | [workflow_runtime.py:549](../src/agent_harness/workflow_runtime.py#L549) | [workflow_runtime.py:551](../src/agent_harness/workflow_runtime.py#L551) | 管理缓存、执行、格式校验和记账 |
| 4 | `WorkflowJournal.cached` | 内部·同文件 | [workflow_runtime.py:558](../src/agent_harness/workflow_runtime.py#L558) | [workflow_runtime.py:406](../src/agent_harness/workflow_runtime.py#L406) | 命中时直接重放，不创建子 agent |
| 5 | `Budget.remaining` | 内部·同文件 | [workflow_runtime.py:570](../src/agent_harness/workflow_runtime.py#L570) | [workflow_runtime.py:460](../src/agent_harness/workflow_runtime.py#L460) | 检查剩余预算 |
| 6 | `SubagentRunner.run` | 线程任务·跨文件 | [workflow_runtime.py:578](../src/agent_harness/workflow_runtime.py#L578)（to_thread 目标） | [workflow_runtime.py:292](../src/agent_harness/workflow_runtime.py#L292) | 将步骤接入统一 once 创建入口 |
| 7 | `spawn_subagent` | 内部·跨文件 | [workflow_runtime.py:304](../src/agent_harness/workflow_runtime.py#L304) | [subagent.py:479](../src/agent_harness/subagent.py#L479) | 登记步骤实例，按工具名单执行并清理 |
| 8 | `_execute_work` | 内部·同文件 | [subagent.py:569](../src/agent_harness/subagent.py#L569) | [subagent.py:406](../src/agent_harness/subagent.py#L406) | 多回合执行到最终回答 |
| 9 | `SubagentSession.run` | 内部·跨文件 | [subagent.py:417](../src/agent_harness/subagent.py#L417) | [subagent_session.py:223](../src/agent_harness/subagent_session.py#L223) | 压缩历史，请求模型，回填工具结果 |
| 10 | `SubagentSession.execute_tool` | 内部·同文件 | [subagent_session.py:263](../src/agent_harness/subagent_session.py#L263) | [subagent_session.py:204](../src/agent_harness/subagent_session.py#L204) | 校验可用工具、计划门禁和权限 hooks |
| 11 | `_parse_runner_json` | 内部·同文件 | [workflow_runtime.py:315](../src/agent_harness/workflow_runtime.py#L315) | [workflow_runtime.py:254](../src/agent_harness/workflow_runtime.py#L254) | 步骤返回后解析结构化文本 |
| 12 | `SimpleJsonSchema.validate` | 内部·同文件 | [workflow_runtime.py:582](../src/agent_harness/workflow_runtime.py#L582) | [workflow_runtime.py:198](../src/agent_harness/workflow_runtime.py#L198) | 校验结果；不合格时最多一次无工具格式修正 |
| 13 | `_record_usage` | 内部·同文件 | [workflow_runtime.py:600](../src/agent_harness/workflow_runtime.py#L600) | [workflow_runtime.py:607](../src/agent_harness/workflow_runtime.py#L607) | 成功和失败均累计实际消费 |
| 14 | `Budget.add` | 内部·同文件 | [workflow_runtime.py:613](../src/agent_harness/workflow_runtime.py#L613) | [workflow_runtime.py:444](../src/agent_harness/workflow_runtime.py#L444) | 累计预算并检查超额 |
| 15 | `WorkflowJournal.record` | 内部·同文件 | [workflow_runtime.py:602](../src/agent_harness/workflow_runtime.py#L602) | [workflow_runtime.py:414](../src/agent_harness/workflow_runtime.py#L414) | 只缓存成功且格式合格的步骤 |

格式修正只处理上次最终结果，关闭工具，文件操作不会因格式错误再执行一次。失败消费也保存在任务快照，恢复时继续计入预算。
整个流程的输出在 [workflow_runtime.py:740](../src/agent_harness/workflow_runtime.py#L740) 保存，任务快照在 [workflow_runtime.py:741](../src/agent_harness/workflow_runtime.py#L741) 保存。
真实客户端在 [host.py:108](../src/agent_harness/host.py#L108) 注入；离线客户端在 [cli.py:114](../src/agent_harness/cli.py#L114) 注入。

#### 13. journal 读写与 resume

选择「pytest（当前文件）」，将 `"${file}"` 替换为
`"tests/test_workflow.py::test_resume_replays_at_budget_limit_without_spending_again"`。

journal 的方法都是工作流内部调用。恢复通过 CLI 的 `workflow resume`，或 `Workflow` 工具的
`resume_from_run_id` 参数进入；LLM 不直接请求这些 journal 方法。

**首次运行：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                                  | 实现位置                                                                 | 作用                       |
| ---- | -------------------------- | ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------- |
| 1    | `WorkflowJournal.__init__` | 内部·同文件 | [workflow_runtime.py:702](../src/agent_harness/workflow_runtime.py#L702)（构造） | [workflow_runtime.py:330](../src/agent_harness/workflow_runtime.py#L330) | 创建新的 journal 文件      |
| 2    | `WorkflowJournal.cached`   | 内部·同文件 | [workflow_runtime.py:558](../src/agent_harness/workflow_runtime.py#L558)         | [workflow_runtime.py:406](../src/agent_harness/workflow_runtime.py#L406) | 检查本步骤是否已有记录     |
| 3    | `WorkflowJournal.record`   | 内部·同文件 | [workflow_runtime.py:602](../src/agent_harness/workflow_runtime.py#L602)         | [workflow_runtime.py:414](../src/agent_harness/workflow_runtime.py#L414) | 步骤成功后追加记录并 flush |

**恢复运行：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                                  | 实现位置                                                                 | 作用                             |
| ---- | -------------------------- | ----------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------- |
| 1    | `_read_snapshot`           | 内部·同文件 | [workflow_runtime.py:691](../src/agent_harness/workflow_runtime.py#L691)         | [workflow_runtime.py:761](../src/agent_harness/workflow_runtime.py#L761) | 读取原运行的任务和参数           |
| 2    | `WorkflowJournal.__init__` | 内部·同文件 | [workflow_runtime.py:699](../src/agent_harness/workflow_runtime.py#L699)（构造） | [workflow_runtime.py:330](../src/agent_harness/workflow_runtime.py#L330) | 读取 journal，恢复缓存和历史消费 |
| 3    | `WorkflowJournal.key`      | 内部·同文件 | [workflow_runtime.py:547](../src/agent_harness/workflow_runtime.py#L547)         | [workflow_runtime.py:395](../src/agent_harness/workflow_runtime.py#L395) | 生成与原步骤对应的缓存键         |
| 4    | `WorkflowJournal.cached`   | 内部·同文件 | [workflow_runtime.py:558](../src/agent_harness/workflow_runtime.py#L558)         | [workflow_runtime.py:406](../src/agent_harness/workflow_runtime.py#L406) | 命中后复用结果，不再执行 runner  |

缓存未命中时，在 [workflow_runtime.py:570](../src/agent_harness/workflow_runtime.py#L570) 检查包含历史消费的剩余预算。

缓存键包含工具名单。默认配置与显式列出全部基础工具等价，关闭或限制工具会使用不同的键。恢复重放结果和历史消费，不重新执行工具，也不重建已删除的文件。

#### 14. memory 选取 / 抽取

选择「Agent 交互（真实 API）」。没有已有记忆时跳过选取，正常回答结束后仍会进入抽取。
选取、抽取和整理由运行时自动触发。这些函数会请求模型，但没有登记为模型工具。

**请求模型前选取记忆：**

| 顺序 | 函数                       | 调用身份    | 调用 / 触发位置                                                      | 实现位置                                                             | 作用                              |
| ---- | -------------------------- | ----------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- | --------------------------------- |
| 1    | `load_memories`            | 内部·跨文件 | [runtime_context.py:42](../src/agent_harness/runtime_context.py#L42) | [memory_runtime.py:371](../src/agent_harness/memory_runtime.py#L371) | 按预算加载当前请求相关的记忆      |
| 2    | `select_relevant_memories` | 内部·同文件 | [memory_runtime.py:386](../src/agent_harness/memory_runtime.py#L386) | [memory_runtime.py:314](../src/agent_harness/memory_runtime.py#L314) | 从当前记忆库筛选相关记录          |
| 3    | `list_memory_files`        | 内部·同文件 | [memory_runtime.py:323](../src/agent_harness/memory_runtime.py#L323) | [memory_runtime.py:228](../src/agent_harness/memory_runtime.py#L228) | 读取记忆目录                      |
| 4    | `recent_user_text`         | 内部·同文件 | [memory_runtime.py:325](../src/agent_harness/memory_runtime.py#L325) | [memory_runtime.py:286](../src/agent_harness/memory_runtime.py#L286) | 取得最近用户文本作为查询          |
| 5    | `extract_json_array`       | 内部·同文件 | [memory_runtime.py:350](../src/agent_harness/memory_runtime.py#L350) | [memory_runtime.py:273](../src/agent_harness/memory_runtime.py#L273) | 解析模型选中的记录下标            |
| 6    | `read_memory_file`         | 内部·同文件 | [memory_runtime.py:388](../src/agent_harness/memory_runtime.py#L388) | [memory_runtime.py:216](../src/agent_harness/memory_runtime.py#L216) | 读取选中的记忆正文                |

筛选请求失败时，在 [memory_runtime.py:368](../src/agent_harness/memory_runtime.py#L368) 调用关键词匹配，实现位于 [memory_runtime.py:298](../src/agent_harness/memory_runtime.py#L298)。

**回答结束后抽取并整理记忆：**

| 顺序 | 函数                     | 调用身份    | 调用 / 触发位置                                                      | 实现位置                                                             | 作用                       |
| ---- | ------------------------ | ----------- | -------------------------------------------------------------------- | -------------------------------------------------------------------- | -------------------------- |
| 1    | `extract_memories`       | 内部·跨文件 | [host.py:160](../src/agent_harness/host.py#L160)                     | [memory_runtime.py:443](../src/agent_harness/memory_runtime.py#L443) | 从最近对话中抽取候选记忆   |
| 2    | `validate_memory_record` | 内部·同文件 | [memory_runtime.py:487](../src/agent_harness/memory_runtime.py#L487) | [memory_runtime.py:412](../src/agent_harness/memory_runtime.py#L412) | 检查候选记录是否合法       |
| 3    | `should_store_memory`    | 内部·同文件 | [memory_runtime.py:495](../src/agent_harness/memory_runtime.py#L495) | [memory_runtime.py:103](../src/agent_harness/memory_runtime.py#L103) | 检查是否值得保存及是否重复 |
| 4    | `write_memory_file`      | 内部·同文件 | [memory_runtime.py:497](../src/agent_harness/memory_runtime.py#L497) | [memory_runtime.py:149](../src/agent_harness/memory_runtime.py#L149) | 写入通过检查的记忆         |
| 5    | `rebuild_memory_index`   | 内部·同文件 | [memory_runtime.py:168](../src/agent_harness/memory_runtime.py#L168) | [memory_runtime.py:171](../src/agent_harness/memory_runtime.py#L171) | 更新索引                   |
| 6    | `consolidate_memories`   | 内部·跨文件 | [host.py:161](../src/agent_harness/host.py#L161)                     | [memory_runtime.py:513](../src/agent_harness/memory_runtime.py#L513) | 有新记忆写入时合并重复记录 |

#### 15. 启动与 `/goal` 拦截

选择「Agent 交互（真实 API）」。仅输入 `/goal`、`/goal clear` 和 `q` 可观察本地命令；
普通问题直接交给模型；/goal <条件> 保存目标后，也把条件交给模型执行。
`/goal` 是 CLI 识别的用户命令，不是 LLM 工具。目标控制器方法由 CLI 或主循环调用。

**启动与读取输入：**

| 顺序 | 函数                     | 调用身份        | 调用 / 触发位置                                                                                                | 实现位置                                                   | 作用                           |
| ---- | ------------------------ | --------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------ |
| 1    | `run_cli`                | CLI 入口        | [cli.py:120](../src/agent_harness/cli.py#L120)                                                                 | [cli.py:50](../src/agent_harness/cli.py#L50)               | 启动交互会话                   |
| 2    | `start_runtime_services` | 内部·跨文件     | [cli.py:58](../src/agent_harness/cli.py#L58)                                                                   | [scheduler.py:502](../src/agent_harness/scheduler.py#L502) | 恢复定时任务并启动调度线程     |
| 3    | `async_event_loop`       | 线程入口·跨文件 | [cli.py:70](../src/agent_harness/cli.py#L70)（线程目标）→ [cli.py:73](../src/agent_harness/cli.py#L73)（启动） | [host.py:505](../src/agent_harness/host.py#L505)           | 后台线程轮询事件并唤醒主 agent |
| 4    | `handle_goal_command`    | CLI 本地命令    | [cli.py:85](../src/agent_harness/cli.py#L85)                                                                   | [cli.py:22](../src/agent_harness/cli.py#L22)               | 读取输入后识别 /goal 命令      |

**`/goal` 的三个分支：**

| 顺序 | 函数                      | 调用身份    | 调用 / 触发位置                              | 实现位置                                         | 作用                                  |
| ---- | ------------------------- | ----------- | -------------------------------------------- | ------------------------------------------------ | ------------------------------------- |
| 1    | `GoalController.status`   | 内部·跨文件 | [cli.py:37](../src/agent_harness/cli.py#L37) | [goal.py:326](../src/agent_harness/goal.py#L326) | /goal 查看状态，跳过主循环            |
| 2    | `GoalController.clear`    | 内部·跨文件 | [cli.py:41](../src/agent_harness/cli.py#L41) | [goal.py:307](../src/agent_harness/goal.py#L307) | /goal clear 清除目标，跳过主循环      |
| 3    | `GoalController.set_goal` | 内部·跨文件 | [cli.py:44](../src/agent_harness/cli.py#L44) | [goal.py:275](../src/agent_harness/goal.py#L275) | /goal <条件> 设置目标，随后进入主循环 |

**普通问题或设置目标后：**

| 顺序 | 函数                    | 调用身份      | 调用 / 触发位置                              | 实现位置                                         | 作用                     |
| ---- | ----------------------- | ------------- | -------------------------------------------- | ------------------------------------------------ | ------------------------ |
| 1    | `trigger_hooks`         | 内部·跨文件   | [cli.py:91](../src/agent_harness/cli.py#L91) | [hooks.py:30](../src/agent_harness/hooks.py#L30) | 触发 UserPromptSubmit    |
| 2    | `agent_loop`            | 主循环·跨文件 | [cli.py:95](../src/agent_harness/cli.py#L95) | [host.py:260](../src/agent_harness/host.py#L260) | 处理已写入历史的用户请求 |
| 3    | `print_turn_assistants` | 内部·跨文件   | [cli.py:96](../src/agent_harness/cli.py#L96) | [host.py:491](../src/agent_harness/host.py#L491) | 展示本轮助手回复         |

`async_event_loop` 是在线程中运行的普通函数，不是 asyncio 协程。


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

- **模型输出额度**集中在 [core.py](../src/agent_harness/core.py)。主循环、子 agent和一次性子 agent使用
  `DEFAULT_MAX_TOKENS`；工作流、历史压缩、记忆筛选、抽取和合并各有专用常量。
  目标评估器的额度由 [PromptGoalEvaluator](../src/agent_harness/goal.py#L174) 的参数单独设置。
- **运行参数**：命令超时、工具输出上限、子代理轮数、重试退避和事件轮询间隔在
  [core.py](../src/agent_harness/core.py) 修改；记忆输入预算在
  [memory_runtime.py](../src/agent_harness/memory_runtime.py)，后台通知摘要长度在
  [scheduler.py](../src/agent_harness/scheduler.py)。输入字符预算与输出 token 额度分别设置。
  独立 agent 脚手架和参考模板使用各自文件顶部的配置，不依赖本项目的 `core.py`。
- **`compact` 没有 handler**。它是 `BUILTIN_TOOLS` 里的 schema，由
  [ManualCompaction.handle_tool_use](../src/agent_harness/compaction.py#L631) 处理，不进 handler 表。
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
- **`cli.py` 会起一个 daemon 线程** `async_event_loop`（cron / 子 agent）。断点若打在里面可能被
  非用户输入触发，排查交互流程时只打在 `agent_loop` 及其下游。
- **集成终端里方向键/中文输入偶尔异常**，改用「外部终端」那条配置。

## 五、未覆盖（需要真实 API）

自检刻意不碰这些，要手动验证：

- 真实模型下的交互问答（见「一、怎么跑」）
- 真实模型下的子 agent 协作决策（线程、信箱和关闭已离线验证）
- memory 抽取与合并
