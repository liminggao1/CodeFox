# 环境安装

从零装好本项目：适用于全新克隆、换机器、他人复现。

本文件只讲**怎么把环境装起来**。用法、命令和参数见 [README.md](../README.md)。

## 前置条件

- **Python 3.10 或更高**（`pyproject.toml` 的 `requires-python`；本项目在 3.10 与 3.12 上验证通过）
- **git**
- 任选一种环境隔离工具：conda、venv 或其他等价工具

## 1. 获取代码

```sh
git clone <仓库地址> codefox
cd codefox
```

## 2. 创建并激活环境

下面任选一种，之后所有命令都在激活后的环境里执行。

**conda**

```sh
conda create -n codefox python=3.12 -y
conda activate codefox
```

**venv**

```sh
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
```

## 3. 安装依赖

```sh
pip install -e ".[dev]"
```

- `-e` 是 editable 安装：改源码即时生效，不用重装。
- `[dev]` 额外装上测试用的 `pytest`（见 `pyproject.toml` 的 `dev`）。
- 依赖清单以 `pyproject.toml` 为唯一来源，本文件不重复维护版本号。

只想跑起来、不跑测试：

```sh
pip install -e .
```

不想安装成包（纯源码运行）：

```sh
pip install anthropic python-dotenv pyyaml
```

## 4. 配置 API

```sh
cp .env.example .env
```

编辑 `.env`：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 是 | API key |
| `MODEL_ID` | 是 | 模型 ID |
| `ANTHROPIC_BASE_URL` | 否 | 兼容端点时填写 |
| `LLM_API` | 否 | 设 `responses` 走 OpenAI Responses 协议 |
| `GOAL_EVALUATOR_PROVIDER` | 否 | 设 `jev` 使用 Jev；默认使用 Prompt evaluator |
| `JEV_API_KEY` | 使用 Jev 时必填 | Jev API key |
| `JEV_MODEL` | 否 | 默认 `typesafe/jev-1.13` |
| `JEV_API_URL` | 否 | 默认 `https://thejevai.com/v1/systemone` |
| `JEV_COMPLETION_CONFIDENCE_THRESHOLD` | 否 | 默认 `0.9`；只有 `choice=complete` 且整体 `confidence` 高于该值才结束目标 |
| `GOAL_EVALUATOR_MAX_TRANSCRIPT_CHARACTERS` | 否 | 送入目标评估器的对话字符上限，默认 `24000`；这是字符数，不是 token 数 |

`.env` 已在 `.gitignore` 中，不会入库。

## 5. 验证

**离线检查，不联网、不消耗额度：**

```sh
python -m pytest -q                     # 期望全部通过
python -m codefox workflow demo   # 期望输出 status=completed
```

**真实 API 交互：**

```sh
python -m codefox
```

如果第 3 步选的是"不安装成包"，运行 `python -m codefox` 前要先让源码可见：

```sh
export PYTHONPATH=src        # Windows PowerShell: $env:PYTHONPATH="src"
```

装了包就不需要这一步，也可以用命令 `codefox` 代替 `python -m codefox`。

## 可选：断点调试

```sh
pip install debugpy
```

VS Code 的具体配置见 [dev-notes.md](dev-notes.md)。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `ModuleNotFoundError: codefox` | 第 3 步没做，或纯源码运行时没设 `PYTHONPATH=src` |
| `KeyError: 'MODEL_ID'` | 第 4 步的 `.env` 没建，或没放在项目根目录 |
| 命令 `codefox` 找不到 | 没安装成包；改用 `python -m codefox` |
| 断点不命中，显示为空心圆 | 见 [dev-notes.md](dev-notes.md) 的踩坑记录 |
