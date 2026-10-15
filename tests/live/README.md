# live：真实 API 测试

这里的测试使用项目配置的真实模型 API，默认被根目录 `tests/conftest.py` 忽略。
它们用于验证流式响应、工具回环、会话恢复、子 agent、goal 和 memory 等真实服务边界。

## 运行

在项目要求的 WSL2 `learncc` 环境中执行：

```sh
LIVE_API=1 PYTHONPATH=src python -m pytest -q tests/live
```

这些测试会消耗 API 额度，并使用 `.env` 中的服务配置。测试夹具会把会话文件隔离到临时目录；真实工作目录和 API 配置仍按当前环境提供。

测试共用的等待和关闭逻辑在 [support/live.py](../support/live.py)，用例集中在 [test_real_api.py](test_real_api.py)。
