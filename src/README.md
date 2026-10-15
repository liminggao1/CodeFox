# src：Python 源码

本项目使用 `src/` 布局。该目录是 Python 包的搜索根目录，应用代码和随包资源放在 `codefox/` 中。

| 目录 | 内容 |
| --- | --- |
| [codefox/](codefox/README.md) | CodeFox 应用包；启动装配、模块职责和下一层阅读入口 |

安装项目后可使用 `codefox` 命令。已有依赖时，也可以在仓库根目录通过 `PYTHONPATH=src python -m codefox` 从源码启动。
完整安装与使用步骤见 [项目 README](../README.md)，测试入口见 [tests/](../tests/README.md)。
