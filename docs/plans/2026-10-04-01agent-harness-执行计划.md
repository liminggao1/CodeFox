# agent-harness 合并项目：执行计划与结果

> 记录时间：2026-10-04
> 状态：已完成并验证
> 目标目录：`D:\01dpan\01project\life\learncc\agent-harness`
> 来源仓库：[learn-claude-code](/D:/01dpan/01project/life/learncc/learn-claude-code)
> 本文件位置：`docs/plans/`（原在 `docs/` 根目录）

> **已被后续计划取代的规则**：[2026-10-08-03 统一子 agent 实施计划](2026-10-08-03统一子agent实施计划.md) 已按用户决定取消 flat 生成机制，移除生成文件、构建脚本、专用测试和自检项。本文 `scripts/build_flat.py`、`flat/agent_full.py` 与相关维护步骤不再适用。原文保留为当时实施记录。

## 一、目标

以 s15 集成宿主为基础，把 s16 的 workflow 运行时和 s17 的 goal loop 原生并入，做成一个自包含项目，另附一份自动拼接的单文件教学版。

定位是**学习调试**：代码近原样搬运，保留分段注释和全局状态，不做异步化重构。

> 后续变更：定位升级为**自有的 code agent 项目**，并迁移到 `src/` 布局、加 console 入口，
> 见「十一」。当前结构与用法以 [README.md](/D:/01dpan/01project/life/learncc/agent-harness/README.md) 为准。

## 二、项目布局

```
agent-harness/
  src/codefox/
    cli.py                 入口：交互 + `/goal` + `workflow demo|resume`（97 行）
    __main__.py            `python -m codefox` 入口
    host.py                s15 主体（3349 行）
    memory_runtime.py      s09 整份拷贝（785 行）
    workflow.py            s16 主体（824 行）
    goal.py                s17 的 goal 层（423 行）
    llm_client.py          客户端 shim（379 行）
    skills/                s07/s15 技能资产（包数据）
  scripts/build_flat.py    拼接脚本（236 行）
  flat/agent_full.py       生成物（5397 行）
  tests/                   4 个测试文件 + conftest
  docs/  pyproject.toml  .env  .env.example  .gitignore  .editorconfig  .gitattributes  README.md
```

## 三、关键改动

### 1. memory 改为正常导入

s15 原先按路径 `importlib` 加载 `s09_memory/code.py`。现在整份拷为 [src/codefox/context/memory_runtime.py](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/context/memory_runtime.py)，host 直接导入并共享 client / model / workspace：

- [host.py:88](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:88) `from . import memory_runtime as MEMORY_RUNTIME`
- 随后设 `WORKDIR` / `MEMORY_DIR` / `MEMORY_INDEX` / `client` / `MODEL`，等价于原先 [:92-110](/D:/01dpan/01project/life/learncc/learn-claude-code/s15_integrated_harness/code.py:92) 的行为。

### 2. workflow 原生化

删掉 `install_workflow_tool` / `load_integrated_host` 的 monkeypatch 方案，让 `Workflow` 直接进工具池：

- [host.py:2814](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:2814) 在 `assemble_tool_pool()` 内追加 `WORKFLOW_TOOL`
- [host.py:2815](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:2815) 注册 `run_workflow_sync`
- `run_workflow_sync` 继续用 `asyncio.run` 桥接同步宿主与异步运行时

### 3. goal 接入 Stop 边界

只改「模型不再请求工具」的收尾分支（原 [code.py:3210](/D:/01dpan/01project/life/learncc/learn-claude-code/s15_integrated_harness/code.py:3210)）：

- [host.py:3160](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:3160) `evaluate_goal()` 调 `GoalController.evaluate_after_turn()`
- [host.py:3241](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:3241) `block` → 补一条 `[Goal still active]` 消息后 `continue`，不落 memory、不释放 assignment
- [host.py:3252](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:3252) 其余终态打印 `[goal] <status>` 后走原收尾路径
- 评估器模型顺序：`GOAL_EVALUATOR_MODEL_ID` → `ANTHROPIC_DEFAULT_HAIKU_MODEL` → `MODEL`
- 沿用 s17 默认：`block_cap=8`、`MAX_GOAL_LENGTH=4000`；goal 状态只在内存

### 4. 资源归属规则

- 运行时状态跟随**启动目录**：`.tasks` / `.memory` / `.mailboxes` / `.worktrees` / `.runtime` / `.scheduled_tasks.json` / `.transcripts` / `.task_outputs`
  - 含把 `workflow.STORE` 从代码目录改为 [workflow.py:47](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/workflow.py:47) 的 `WORKDIR / ".runtime"`
- 资产随包分发：[host.py:67](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/host.py:67) 的 `SKILLS_DIR = PACKAGE_DIR / "skills"`

### 5. CLI

- `/goal` 在写入 history 前本地拦截（[src/codefox/frontends/cli.py](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/frontends/cli.py)），支持 `/goal`、`/goal <条件>`、`/goal clear|stop|off|reset|none|cancel`
- 提示符改为 `agent-harness >> `
- `agent-harness workflow demo|resume` 走 s16 原 `run_demo`

### 6. 跨平台

[workflow.py:31](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/workflow.py:31) 用 try/except 包 `fcntl`；无 fcntl 时 [workflow.py:97](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/workflow.py:97) 打印一次提示并退化为进程内锁。

### 7. 单文件生成

[scripts/build_flat.py](/D:/01dpan/01project/life/learncc/agent-harness/scripts/build_flat.py) 按 [SECTIONS](/D:/01dpan/01project/life/learncc/agent-harness/scripts/build_flat.py:24) = `memory_runtime → goal → workflow → host` 拼接，剥离模块头、导入和 `__main__` 块，并注入自举 `sys.path`，输出可独立运行的 `flat/agent_full.py`。

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

- `agent-harness workflow demo` → `resume`：journal 命中，`status=completed agents=0 tokens=0`，`.runtime/` 落在启动目录
- `python flat/agent_full.py workflow demo`：单文件版可独立跑通
- `agent-harness`：`/goal` 状态查询与 `/goal clear` 正常，提示符正确

## 六、与原计划的偏差

1. **拼接顺序**改为 `memory → goal → workflow → host`（原写「内存/常量 → memory → host → workflow → goal」）。
   原因：host 在模块级就配置 `RUNNER_FACTORY` 和 `GOAL`，必须最后执行，否则被覆盖。
2. **demo 用 mock runner**：`cli.py` 显式设 `workflow.RUNNER_FACTORY = MockAgentRunner`（[cli.py:90](/D:/01dpan/01project/life/learncc/agent-harness/src/codefox/frontends/cli.py:90)）。
   原因：`import codefox.host` 会把 runner 永久改成真实 API 版本，导致 demo 与测试走网络；实测发现后修正。

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
- 打包方式：见「十一」，已改为 `src/` 布局 + `pyproject.toml` + console 入口
- 无新依赖引入

## 九、维护提示

- 改 `src/codefox/` 下任一模块后要重跑 `python scripts/build_flat.py`；
  `tests/test_flat_build.py::test_committed_flat_file_is_current` 会在生成物过期时报错。
- `flat/agent_full.py` 是生成物，不要直接编辑。

## 十、结构规范化

在保留全部导入路径与扁平布局的前提下补齐通用工程化外壳：

- `note/` → `docs/`，`tools/` → `scripts/`，引用同步更新。
- 新增 `pyproject.toml`：项目元数据、运行时依赖、`pytest` 配置。删除 `requirements.txt`，
  依赖以 `pyproject.toml` 为唯一来源，安装改为 `pip install -e ".[dev]"`。
- 新增 `.editorconfig`（UTF-8 / LF / Python 4 空格）与 `.gitattributes`（统一 LF、标记生成物）。
- 未引入 linter/formatter：`src/codefox/` 是课程原样代码，`black --check` 会重排 13 个文件，
  与「近原样搬运」定位冲突。
- 目录布局以 [README.md](/D:/01dpan/01project/life/learncc/agent-harness/README.md) 的「目录」表为准，本节不重复维护。

## 十一、`src/` 布局与打包

项目定位从「教学复刻」转为「自有的 code agent 项目」，因此按 Python 打包惯例重构：

- 代码移入 `src/codefox/`：`cli.py`、`host.py`、`goal.py`、`workflow.py`、
  `memory_runtime.py`、`llm_client.py`、`skills/`；`llm_client.py` 从顶层模块变为包内模块。
- 包内改用相对导入：`from .llm_client import Anthropic`、`from . import host, workflow`；
  删掉 `sys.path` 自举补丁（原 `_repo_root` hack）。
- `skills/` 从项目根移入包内并声明为包数据；`SKILLS_DIR` 改为
  `Path(__file__).resolve().parent / "skills"`，editable 与 wheel 安装都能解析。
- 新增 `src/codefox/__main__.py`；`pyproject.toml` 增加
  `[project.scripts] agent-harness = "codefox.frontends.cli:main"` 和
  `[tool.setuptools.packages.find] where = ["src"]`；`main(argv=None)` 兼容无参调用。
- 入口改为 `agent-harness` 或 `python -m codefox`；`python cli.py` 不再可用。
- 单文件版自举路径改指 `<repo>/src`，其 `llm_client` 导入改为
  `from codefox.providers.llm_client import Anthropic`。
- 验证：`PYTHONPATH=src python -m codefox workflow demo|resume` 通过；
  `scan_skills()` 从包内解析出 4 个技能；56 个离线测试全绿。

## 十二、项目约定与文档

参考 `channelagent` 的结构，补齐项目级约定与文档入口：

- `AGENTS.md`：WSL `learncc` 环境、源码运行命令、flat 生成物不变量、UTF-8/LF 与工具通道注意事项。
- `GLOSSARY.md`：项目术语表（host / workflow / goal / runner / journal / flat），另含范围、现状与布局。
- `note/env-setup.md`：从零安装环境的步骤；内容以该文件为准，见「十四」。
- `docs/README.md`：`docs/` 索引。
- 本地运行方式回到源码，不依赖 `pip install -e .`（本机未安装）。

## 十三、目录骨架

按 `channelagent` 的文档组织补齐空目录，先建骨架、内容待填：

- `docs/adr/`、`docs/plans/`、`docs/research/`、`docs/handoff/`、`docs/guides/`，
  各放一个 `.gitkeep` 以便入库。
- `docs/agents/issue-tracker.md`：议题看板约定占位，标注**尚未启用**。
- `.scratch/`：本地 Markdown 议题看板根目录，带 `.gitkeep`。
- 索引与约定同步到 `docs/README.md` 和 `AGENTS.md`；执行计划随后移入 `docs/plans/`，见「十四」。

## 十四、文档定位修正

把 `note/env-setup.md` 从"本机环境记录"改成**面向全新克隆的通用安装教程**，以便开源后
他人可依此装起环境：

- 移除本机事实：绝对路径、conda 环境名 `learncc`、已装版本号、"当前未安装"等状态描述。
- 改为通用步骤：前置条件 → 获取代码 → 建环境（conda 或 venv）→ 装依赖 → 配 `.env` → 验证，
  末尾附常见问题表；命令使用相对路径与通用解释器。
- 本机环境事实（WSL、`learncc`、源码运行方式）唯一权威来源为 `AGENTS.md`，其他文件改为指向它。
- 依赖版本只在 `pyproject.toml` 声明，命令用法只在 `README.md`，本文件不重复。
- 复现验证：在 `/tmp` 用全新副本 + 全新 venv（Python 3.10，即 `requires-python` 下限）
  按本文件走完五步，`pytest` 与 `workflow demo` 均通过，console script 可用。

## 十五、README 面向读者与 tmp 目录

- `README.md` 从"本机口吻"改为**面向使用者**：运行段以"已安装"为主路径
  （`agent-harness`），源码运行（`PYTHONPATH=src` + `python -m codefox`）作为替代。
- **从 README 移除来源事实**：不再提"教学仓库""原课程""移植"，以及 s07/s09/s15/s16/s17
  这类章节编号。原「相对原课程的改动」一节删除：其中属来源历史的（memory 改导入、
  workflow 原生化）不再出现；属运行时行为的（资源归属、Stop 边界、goal 仅内存、跨平台）
  改写为「行为要点」。来源与移植过程只在本计划文档保留。
- 新增 `tmp/`：本地临时文件目录，`.gitignore` 中整体忽略，不入库；需要时手动 `mkdir tmp`。
  （曾试过 `tmp/*` + `!tmp/.gitkeep` 让目录入库，最终选择更简单的纯忽略。）
- 模块描述改为功能性表述（如 `host.py` = "agent 主循环"，`memory_runtime.py` =
  "memory 运行时"），不再依赖章节编号。
- **代码与元数据同步去来源化**：`pyproject.toml` 描述、CLI 启动横幅、`build_flat` 的
  flat 横幅、各模块 docstring（host / workflow / goal / memory_runtime / llm_client /
  `__init__`）、memory 的 `__main__` 提示词、测试文件 docstring（test_goal /
  test_workflow / test_agent_teams）、`GLOSSARY.md` 布局表与 `docs/README.md` 索引，
  全部改为不含章节编号与来源的表述。`agent-builder` 技能资产内的章节引用保持不变。
- 复检：`flat/agent_full.py` 已重新生成且与源码一致；除本计划文档与技能资产外，仓库内
  已无来源痕迹；56 个测试与 9 项自检全部通过。
