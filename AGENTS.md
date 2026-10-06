## 默认运行环境

- 本项目在 WSL2 的 conda 环境 `learncc` 中运行（`/home/gaogao/miniconda3/envs/learncc`）。
- WSL 项目路径：`/mnt/d/01dpan/01project/life/learncc/agent-harness`。
- 不在 Windows Python 环境安装依赖或跑测试；`learncc` 不可用时报告问题，不静默切换。
- 运行与测试走源码，不依赖安装：CLI 用 `PYTHONPATH=src ... python -m agent_harness`，pytest 由
  `tests/conftest.py` 注入 `src`。命令见 `README.md`；通用安装步骤见 `note/env-setup.md`。
- 断点调试用 VS Code 的 **Remote-WSL 窗口**（文件仍在 D:，执行在 WSL），配置在 `.vscode/`，
  见 `note/dev-notes.md`。

## 项目不变量

- `flat/agent_full.py` 是 `scripts/build_flat.py` 的生成物，不要直接编辑；改完
  `src/agent_harness/` 必须重跑 `python scripts/build_flat.py`，`tests/test_flat_build.py`
  会在生成物过期时报错。
- 改动后除 `python -m pytest -q` 外，再跑 `python scripts/debug_pass.py`（离线端到端自检），
  操作说明见 `note/dev-notes.md`。
- 运行时状态（`.tasks` / `.memory` / `.runtime` / `.mailboxes` / `.worktrees` / `.transcripts`
  / `.task_outputs` / `.scheduled_tasks.json`）跟随**启动目录**；`skills/` 随包，位于
  `src/agent_harness/skills`。
- 代码保持现有风格与分段注释：不做异步化重构，不引入 linter/formatter。

## 文件写入与编码

- 仓库文本统一 UTF-8 无 BOM + LF，由 `.gitattributes` 的 `* text=auto eol=lf` 固定；
  改动前后用 `git ls-files --eol <路径>` 核对。
- PowerShell 5.1 的 `>` 写 UTF-16LE、`Set-Content -Encoding UTF8` 写带 BOM；写仓库文件用
  `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`。
- 读含中文的文件用 `Get-Content -Encoding UTF8`。

## 工具通道

- 编辑文件用 `apply_patch`；含换行的补丁不要经 `.bat` 包装器（`%*` 只保留第一行，会报
  `The last line of the patch must be '*** End Patch'`），直接调 codex.exe 的
  `--codex-run-as-apply-patch`。
- 不要把多行命令塞进 `wsl.exe -- bash -c "<多行>"`，参数会丢失；改用 `wsl.exe -- bash <脚本文件>`。

## 文档位置

- `docs/`：项目级记录，索引见 `docs/README.md`；合并执行计划在 `docs/plans/`，
  其余 `adr/`、`research/`、`handoff/`、`guides/` 已建目录待填。
- `docs/agents/`：agent 协作约定，见下方 Agent skills。
- `.scratch/`：本地 Markdown 议题与规格的存放目录。
- `tmp/`：本地临时文件，不入库（需要时手动创建）。
- `note/`：环境、操作等个人笔记。
- `GLOSSARY.md`：项目术语表，另含范围、现状与布局。
  
## 用户提示
- Git 提交是本项目的默认收尾动作：每次完成一个开发任务（代码改动 + 测试通过）后，默认直接
  `git commit`，不需要事先询问；是否推送仍按全局规则，未经要求不推送。

## Agent skills

### Issue tracker

议题与规格以 Markdown 文件存放在 `.scratch/<feature-slug>/`。详见 `docs/agents/issue-tracker.md`。

### Triage labels

分诊状态使用五个默认标签。详见 `docs/agents/triage-labels.md`。

### Domain docs

采用单一领域上下文，术语表在根目录 `GLOSSARY.md`。详见 `docs/agents/domain.md`。
