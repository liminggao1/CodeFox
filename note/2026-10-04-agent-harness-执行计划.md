# agent-harness 合并项目：执行计划与结果

> 记录时间：2026-10-04
> 状态：已完成并验证
> 目标目录：`D:\01dpan\01project\life\learncc\agent-harness`
> 来源仓库：[learn-claude-code](/D:/01dpan/01project/life/learncc/learn-claude-code)

## 一、目标

以 s15 集成宿主为基础，把 s16 的 workflow 运行时和 s17 的 goal loop 原生并入，做成一个自包含项目，另附一份自动拼接的单文件教学版。

定位是**学习调试**：代码近原样搬运，保留分段注释和全局状态，不做异步化重构。

## 二、项目布局

```
agent-harness/
  cli.py                   入口：交互 + `/goal` + `workflow demo|resume`
  agent/__init__.py
  agent/host.py            s15 主体（3358 行）
  agent/memory_runtime.py  s09 整份拷贝（792 行）
  agent/workflow.py        s16 主体（824 行）
  agent/goal.py            s17 的 goal 层（423 行）
  skills/                  s07/s15 技能资产
  tools/build_flat.py      拼接脚本（234 行）
  flat/agent_full.py       生成物（5409 行）
  tests/                   4 个测试文件 + conftest
  llm_client.py  requirements.txt  .env  .env.example  .gitignore  README.md
```

## 三、关键改动

### 1. memory 改为正常导入

s15 原先按路径 `importlib` 加载 `s09_memory/code.py`。现在整份拷为 [agent/memory_runtime.py](/D:/01dpan/01project/life/learncc/agent-harness/agent/memory_runtime.py)，host 直接导入并共享 client / model / workspace：

- [host.py:96](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:96) `from . import memory_runtime as MEMORY_RUNTIME`
- 随后设 `WORKDIR` / `MEMORY_DIR` / `MEMORY_INDEX` / `client` / `MODEL`，等价于原先 [:92-110](/D:/01dpan/01project/life/learncc/learn-claude-code/s15_integrated_harness/code.py:92) 的行为。

### 2. workflow 原生化

删掉 `install_workflow_tool` / `load_integrated_host` 的 monkeypatch 方案，让 `Workflow` 直接进工具池：

- [host.py:2821](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:2821) 在 `assemble_tool_pool()` 内追加 `WORKFLOW_TOOL`
- [host.py:2823](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:2823) 注册 `run_workflow_sync`
- `run_workflow_sync` 继续用 `asyncio.run` 桥接同步宿主与异步运行时

### 3. goal 接入 Stop 边界

只改「模型不再请求工具」的收尾分支（原 [code.py:3210](/D:/01dpan/01project/life/learncc/learn-claude-code/s15_integrated_harness/code.py:3210)）：

- [host.py:3168](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:3168) `evaluate_goal()` 调 `GoalController.evaluate_after_turn()`
- [host.py:3249](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:3249) `block` → 补一条 `[Goal still active]` 消息后 `continue`，不落 memory、不释放 assignment
- [host.py:3260](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:3260) 其余终态打印 `[goal] <status>` 后走原收尾路径
- 评估器模型顺序：`GOAL_EVALUATOR_MODEL_ID` → `ANTHROPIC_DEFAULT_HAIKU_MODEL` → `MODEL`
- 沿用 s17 默认：`block_cap=8`、`MAX_GOAL_LENGTH=4000`；goal 状态只在内存

### 4. 资源归属规则

- 运行时状态跟随**启动目录**：`.tasks` / `.memory` / `.mailboxes` / `.worktrees` / `.runtime` / `.scheduled_tasks.json` / `.transcripts` / `.task_outputs`
  - 含把 `workflow.STORE` 从代码目录改为 [workflow.py:47](/D:/01dpan/01project/life/learncc/agent-harness/agent/workflow.py:47) 的 `WORKDIR / ".runtime"`
- 资产跟随**项目目录**：[host.py:75](/D:/01dpan/01project/life/learncc/agent-harness/agent/host.py:75) 的 `SKILLS_DIR = PROJECT_ROOT / "skills"`

### 5. CLI

- `/goal` 在写入 history 前本地拦截（[cli.py](/D:/01dpan/01project/life/learncc/agent-harness/cli.py)），支持 `/goal`、`/goal <条件>`、`/goal clear|stop|off|reset|none|cancel`
- 提示符改为 `agent-harness >> `
- `python cli.py workflow demo|resume` 走 s16 原 `run_demo`

### 6. 跨平台

[workflow.py:32](/D:/01dpan/01project/life/learncc/agent-harness/agent/workflow.py:32) 用 try/except 包 `fcntl`；无 fcntl 时 [workflow.py:97](/D:/01dpan/01project/life/learncc/agent-harness/agent/workflow.py:97) 打印一次提示并退化为进程内锁。

### 7. 单文件生成

[tools/build_flat.py](/D:/01dpan/01project/life/learncc/agent-harness/tools/build_flat.py) 按 [SECTIONS](/D:/01dpan/01project/life/learncc/agent-harness/tools/build_flat.py:24) = `memory_runtime → goal → workflow → host` 拼接，剥离模块头、导入和 `__main__` 块，并注入自举 `sys.path`，输出可独立运行的 `flat/agent_full.py`。

### 8. 工程化

`git init` + `.gitignore`（忽略 `.env` 和全部运行时状态目录），未产生任何提交。

## 四、测试计划与结果

| 文件 | 用例数 | 说明 |
| --- | --- | --- |
| `tests/test_goal.py` | 19 | 控制器用例直接移植；会话级用例改用脚本化 client 驱动 `agent_loop` |
| `tests/test_workflow.py` | 16 | 原 17 个中，`install/load` 类改为断言真实工具池与 runner 接线 |
| `tests/test_agent_teams.py` | 16 | 只保留适用合并宿主的运行时用例，丢弃跨章节路径与对比 |
| `tests/test_flat_build.py` | 5 | 拼接确定性、与生成物一致、可编译、可运行 |

```sh
python -m pytest -q
# 56 passed in 2.5s（全部离线，不消耗 API）
```

## 五、实测验证

- `python cli.py workflow demo` → `resume`：journal 命中，`status=completed agents=0 tokens=0`，`.runtime/` 落在启动目录
- `python flat/agent_full.py workflow demo`：单文件版可独立跑通
- `python cli.py`：`/goal` 状态查询与 `/goal clear` 正常，提示符正确

## 六、与原计划的偏差

1. **拼接顺序**改为 `memory → goal → workflow → host`（原写「内存/常量 → memory → host → workflow → goal」）。
   原因：host 在模块级就配置 `RUNNER_FACTORY` 和 `GOAL`，必须最后执行，否则被覆盖。
2. **demo 用 mock runner**：`cli.py` 显式设 `workflow.RUNNER_FACTORY = MockAgentRunner`（[cli.py:92](/D:/01dpan/01project/life/learncc/agent-harness/cli.py:92)）。
   原因：`import agent.host` 会把 runner 永久改成真实 API 版本，导致 demo 与测试走网络；实测发现后修正。

## 七、未验证项

以下沿用 s15 原逻辑，本次未改动，也未实测：

- 真实 API 下的交互问答
- 队友线程端到端协作
- worktree 的 git 操作（需要仓库环境）
- memory 抽取与合并（依赖真实模型）

## 八、假设

- 运行环境为 WSL/Linux；原生 Windows 仅保证可导入，workflow 跨进程锁能力降级
- 沿用 conda 环境 `learncc`，不新建虚拟环境
- s15 全部机制保留（teams / cron / MCP / memory / worktrees / background / compact），不裁剪
- 不做打包（无 pyproject），入口为 `python cli.py`
- 无新依赖引入

## 九、维护提示

- 改 `agent/` 下任一模块后要重跑 `python tools/build_flat.py`；
  `tests/test_flat_build.py::test_committed_flat_file_is_current` 会在生成物过期时报错。
- `flat/agent_full.py` 是生成物，不要直接编辑。