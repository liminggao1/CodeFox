# 子 agent 能力与任务边界改造验收（2026-10-14）

日期：2026-10-14。状态：改造与复核修正已实施；初版真实 Responses API 的 once 只读审计通过。
恢复会话：`01a119fd-4a90-79d3-afe3-5020b0fb18e1`。

## 问题与结果

原运行中，只有 `read_file` 的子 agent 收到了包含 shell 与文件发现工具的通用提示，
反复读取重叠范围，并因尝试读取会话工具归档触发路径审批。
本次按批准范围统一了实际工具能力与提示，增加工作预算、稳定身份、结果查询、进度与终止原因。
当前行为和参数以 [仓库事实第 4 节](../architecture/facts.md#4-统一子-agent2026-10-14) 为唯一契约；
运行和断点步骤见 [调试笔记第 9 节](../../note/dev-notes.md#9-统一子-agent-生命周期与协议)。

预算耗尽直接交付部分结果，无额外总结请求。persistent 结束当前工作后仍可接受下一次委派。
会话内部工具归档在子 agent 的通用读取工具进入路径审批前拒绝。
工作流接入同一契约，能够保留部分结果对应的真实失败原因和用量。

本次没有增加专用内容搜索工具、递归 agent、跨进程恢复或 worktree 能力。
Lead 重试策略、总体权限规则和界面布局保持原有边界。

## 离线验证

验证使用 WSL2 conda `learncc`，从源码运行。
新增覆盖包括双预算、多工具结果配对、只读白名单、摘要请求计数、persistent 续做、
ID 通信与协议、查询等待及深拷贝、停止通知、归档路径拦截和终态历史回放。
工作流另验证参数失败无模型请求，以及预算耗尽时保留原因、结算用量且不缓存失败结果。
复核修正另覆盖审批等待时的预算耗尽、旧审批不重置预算、新委派接受前的结果查询、
最新工具证据、结果交付期间的新计划门禁、审批与新消息并发、排队委派合并及投递失败。
列表与事件的状态字段改为共用结果快照。

- 复核修正新增 10 个回归场景；定向子 agent 与工作流回归：180 项通过。
- 最终全量 pytest：728 项通过，耗时 67.17 秒。
- `scripts/debug_pass.py`：10/10 通过，包含工具、goal、任务、两种子 agent 生命周期、工作流、worktree 和架构图刷新。架构图产物无新增差异。
- `git diff --check` 通过；改动文本均为 UTF-8 无 BOM、LF。

```sh
/home/gaogao/miniconda3/envs/learncc/bin/python -m pytest -q
/home/gaogao/miniconda3/envs/learncc/bin/python scripts/debug_pass.py
```

## 真实 API 验收

用户开启本机代理后，在 WSL 显式设置该进程的 `HTTP_PROXY` 与 `HTTPS_PROXY`，
使用当前 `.env` 的 `deepseek-v4.1-flash` 模型和 Responses 端点。
成功会话为 `20261014-114207-b40fdc9a`，子 agent ID 为 `b81ccaa5346b4bdf963bf992249fecf2`。

自动用例将当前 `subagent.py` 复制到临时目录，并按函数和启动分支定位范围。
Lead 只创建一次 `read-only-audit`，子 agent 使用 `glob` 发现文件，
再以 `offset` / `limit=34` 读取启动分支并回答 once 与 persistent 的启动区别。
两项工作预算均设为 6。

| 验证项 | 实际结果 |
| --- | --- |
| pytest | `1 passed in 74.75s` |
| Lead 与子 agent 终态 | 均为 `completed` |
| 子 agent 模型请求 | 3 次，未超预算 |
| 子 agent 工具调用 | 2 次：glob、read_file 各一次 |
| 子 agent token 用量 | 输入 16,911，输出 455，总计 17,366 |
| Lead 用量 | 2 次请求；输入 12,702，输出 574 |
| 路径审批 | 0 次 |
| 文件影响 | 临时副本内容未改变 |
| 查询与返回 | 按 agent ID 查询与创建工具返回完全一致 |
| 退出清理 | 无残留活跃子 agent |

复测命令在 WSL 项目根目录执行：

```sh
HTTP_PROXY=http://127.0.0.1:7892 HTTPS_PROXY=http://127.0.0.1:7892 \
LIVE_API=1 /home/gaogao/miniconda3/envs/learncc/bin/python -m pytest -q -s \
tests/live/test_real_api.py::test_streaming_bounded_read_only_subagent_audit
```

此结果来自复核修正前，验证 once 的真实委派、能力白名单、范围读取、预算内完成、零审批、结果查询与清理。
本轮复核修正使用离线模型与真实线程验证，未新增真实 API 请求。
persistent 的真实通信和关闭、真实预算耗尽、TUI 人工交互及 memory 抽取仍未覆盖，
见 [待验证事项](../plans/2026-10-11-01待验证事项.md)。

## 网络故障证据与限制

成功前多次审计请求遇到 `SSL: UNEXPECTED_EOF_WHILE_READING`。
会话 `20261014-112135-11728307` 和开启代理后的 `20261014-113237-32f6bc57` 均记录：
子 agent 首次请求失败、未执行工具，返回 `network_error`，该次模型计数回滚为 0；
随后 Lead 请求也因同类连接错误失败，均无审批。

代理下的小请求同步与流式调用成功。对照子 agent 请求也曾成功完成两项工具，
随后在探针的 30 秒读取超时内未收到最终回答。最终完整用例使用项目原有的请求超时后通过。
这些证据不足以定位 TLS 中断的具体原因，也不说明开启代理就能消除所有连接故障。
本次没有修改网络重试策略、持久代理设置或超时默认值；临时网络探针在收尾删除。

## 原运行证据

原会话 `20261014-051345-ed8daaf3` 约运行于上海时间 2026-10-14 13:13 至 13:18。
第一次提交遇到 `Connection refused`，没有工具调用或 token 用量。用户重试后，
Lead 创建 `mode=once`、`tools=[read_file]` 的 `read-only-audit`。
保存的诊断中包含 34 次 read_file、约 15 次模型请求或工作状态更新；
约 25,468 字符的工具结果被归档，子 agent 两次读取归档尝试触发路径审批。
用户在最终结果产生前手动停止，因此不能推断它会触及当时的轮数上限。
