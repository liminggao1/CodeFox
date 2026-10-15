# scripts：开发辅助脚本

这里保存手动运行的开发脚本。以下命令都在仓库根目录、已准备好项目依赖的 Python 环境中执行。

| 文件 | 用途 |
| --- | --- |
| [debug_pass.py](debug_pass.py) | 离线端到端自检，覆盖真实 CLI 子进程、工具回环、goal、任务、子 agent、工作流、Git 工作树和架构图 |
| [debug_chat.py](debug_chat.py) | 使用固定模型响应运行真实 CLI，便于逐轮观察输入和工具调用 |
| [arch_views.py](arch_views.py) | 静态扫描 Python 导入，生成包级依赖图及循环依赖说明 |

## 常用命令

```sh
# 离线端到端自检
python scripts/debug_pass.py

# 模拟交互：输入“你好”或“写文件”，输入 q 退出
python scripts/debug_chat.py

# 更新架构图
python scripts/arch_views.py

# 只查看 Mermaid 文本，不写文件
python scripts/arch_views.py --stdout
```

自检和模拟交互使用模拟模型，不请求真实 API。脚本会自行加入 `src/` 搜索路径，无需安装项目包。

自检大多在临时目录运行；架构图检查会刷新 `docs/architecture/deps.md` 和 `deps.mmd`。
模拟交互的文件和会话记录在临时目录中，正常退出后清理。架构图也可以通过 `--out-dir <目录>` 写到其他位置。

模块级测试与覆盖归属见 [tests/](../tests/README.md)，图文件说明见 [架构文档](../docs/architecture/README.md)。
仓库入口见 [项目 README](../README.md)。
