# 交接：项目结构初始化与工程化（2026-10-04 会话）

日期：2026-10-04
状态：**全部完成并验证通过**，工作区干净，无未提交改动。
本文件由结构整理会话产出，供新会话接手功能开发使用。

## 0. 一句话背景

起点是一个**能跑但没有任何工程结构**的合并项目：s15 集成宿主 + s16 工作流运行时 +
s17 目标循环挤在扁平目录里，`git` 只有一个 message 为 `1` 的初始提交。
本会话把它整理成一个可安装、可调试、可开源的标准 Python 项目，**没有改动任何业务逻辑**。

## 1. 本会话结论

项目现在有：`src/` 布局、`pyproject.toml`、console 入口、`docs/`（含 ADR/plans/handoff
骨架）、`note/`（安装与调试笔记）、56 个离线测试、9 项离线端到端自检、VS Code
Remote-WSL 调试配置、以及 agent 协作约定（`AGENTS.md` + `docs/agents/`）。

**功能一行没加，行为一行没改**——所有提交都是结构、文档、配置、工具链。

## 2. 已完成（不要重做）

细节见提交历史与计划文档，这里只列清单和落点。本阶段历史现保留两个里程碑：
`5f525e1` 为起始快照，`301d618` 为工程化初始化，包含下表全部交付。

| 主题 | 落点 | 参考 |
| --- | --- | --- |
| 目录改名 + 工程化外壳 | `docs/ scripts/ pyproject.toml .editorconfig .gitattributes` | 工程化初始化 |
| 迁移到 `src/agent_harness/` + console 入口 | `pyproject.toml` 的 `[project.scripts]`、`__main__.py` | 计划「十一」 |
| 项目约定文档 | `AGENTS.md`（本机环境 + 不变量）、`GLOSSARY.md`（术语 + 范围）、`docs/agents/` | 本机环境、运行不变量与协作规则 |
| docs 骨架与索引 | `docs/{adr,plans,research,handoff,guides,agents}/`、`docs/README.md` | 项目文档分类与入口 |
| 离线端到端自检 | `scripts/debug_pass.py`（9 项） | 说明在 `note/dev-notes.md` |
| VS Code 调试 | `.vscode/{launch,settings,extensions}.json` | Remote-WSL 调试配置 |
| 安装说明面向通用读者 | `note/env-setup.md` | 通用环境安装教程 |
| README 去来源化 + 使用者视角 | `README.md` | 计划「十五」 |
| 调试笔记二合一 | `note/dev-notes.md` | 调试操作与运行路径集中记录 |
| `tmp/` 本地临时目录 | `.gitignore` 中 `tmp/` | 本地内容不入库 |

不在上表、但值得知道的几个决定：

- **`.env` 留在项目根，未搬进 `configs/`**。理由：`load_dotenv()` 从包文件位置向上查找，
  搬走就要在 `host.py` 和 `memory_runtime.py` 两处硬编码路径；且根目录 `.env` 是通用约定。
- **`tmp/` 不跟踪**（整体忽略），代价是全新克隆后需手动 `mkdir tmp`。
- **改名的只有目录，代码里没有兼容层**。`from agent import ...` 之类的旧写法已全部改写；
  `python cli.py` 这个入口不再可用。
- **来源事实只保留在本计划文档与 `docs/handoff/`**。`README`、`pyproject` 描述、CLI 横幅、
  模块 docstring、测试 docstring、`GLOSSARY.md` 里的章节编号（s07/s09/s15/s16/s17）已清除。
  例外：`src/agent_harness/skills/agent-builder/` 内含章节引用的参考资产**刻意保留**。

## 3. 验证证据（本会话已跑）

```sh
cd /mnt/d/01dpan/01project/life/learncc/agent-harness
PY=/home/gaogao/miniconda3/envs/learncc/bin/python

$PY -m pytest -q              # 56 passed
$PY scripts/debug_pass.py     # 9/9 passed
```

另有一次**全新环境复现**：把仓库复制到 `/tmp`、建全新 venv（Python **3.10.12**，即
`requires-python` 下限）、按 `note/env-setup.md` 走完五步，`pytest` 与 `workflow demo`
均通过、console script 可用。该临时副本已删除。

`git status` 干净，`master` 分支，**无 remote**（未推送任何内容）。

## 4. 未验证（下一会话可按需补）

自检特意不碰需要真实 API 的路径，因此以下仍未实测：

- **真实 API 下的交互问答**（`python -m agent_harness`）——最高优先级，主路径未端到端跑过
- 队友线程端到端协作
- memory 抽取与合并

`.env` 已配好（`ANTHROPIC_API_KEY` / `MODEL_ID` / `ANTHROPIC_BASE_URL` / `LLM_API` 均有值），
可直接跑真实调用。注意每轮都会消耗额度。

## 5. 下一会话的起点

功能开发尚未开始。若承接**新功能**，按主流程走（见下节 skills）。若只是**验证现状**，
先跑第 3 节两条命令，再做第 4 节的真实 API 交互。

已知可做的下一步（均未开始，择一）：

1. 跑一次真实 API 端到端交互，确认主路径无问题——成本最低、价值最高。
2. 补 `docs/adr/` 第一条决策记录（目录已建、内容为空）。
3. 往 `.scratch/` 建第一个议题（`docs/agents/issue-tracker.md` 的约定已就位）。
4. 给 `debugpy` 加进 `pyproject.toml` 的 `dev` 依赖（当前只在环境里，未声明）。

## 6. 环境与命令速查

- WSL2 `Ubuntu-22.04`，conda 环境 `learncc`，Python 3.12。
- 项目路径（WSL）：`/mnt/d/01dpan/01project/life/learncc/agent-harness`
  （Windows：`D:\01dpan\01project\life\learncc\agent-harness`，同一份文件）。
- **本机按源码运行，未安装成包**。CLI 要 `PYTHONPATH=src`：
  ```sh
  PYTHONPATH=src $PY -m agent_harness
  ```
- `pytest` 复用 `tests/conftest.py` 注入的 `src`，无需 `PYTHONPATH`。
- 改完 `src/agent_harness/` 必须重跑 `python scripts/build_flat.py`，否则测试与自检会报
  生成物过期。
- 完整命令与踩坑：`note/dev-notes.md`；环境约定：`AGENTS.md`；通用安装：`note/env-setup.md`。

## 7. 建议调用的 skills

按 `ask-matt` 的主流程，视下一会话目标选择：

| 场景 | skill |
| --- | --- |
| 有了新想法，要先磨清楚 | `/grill-with-docs`（会写 `GLOSSARY.md` 和 ADR） |
| 想法已清楚、要拆成可执行任务 | `/to-spec` → `/to-tickets` |
| 按票据实现 | `/implement`（复杂多票据用 `/implement-spec`） |
| 只想先把某个行为写出来 | `/tdd` |
| 改完要审 | `/code-review`（Standards + Spec 双轴） |
| 要固化术语或补决策记录 | `/domain-modeling` |
| 要查外部资料 | `/research` |

本仓库已按 `setup-matt-pocock-skills` 配好：议题走 `.scratch/`（`docs/agents/issue-tracker.md`）、
分诊标签五个（`docs/agents/triage-labels.md`）、领域文档为单上下文（`docs/agents/domain.md`）。

## 8. 相关文档

- 当前架构与行为事实：`docs/architecture/facts.md`（结构初始化段的原始决策记录由 Git 历史保留）
- 术语与项目范围：`GLOSSARY.md`
- 项目结构与命令：`README.md`
- 本机环境约定：`AGENTS.md`
- 运行与调试笔记：`note/dev-notes.md`
- 通用安装：`note/env-setup.md`
- docs 索引：`docs/README.md`
