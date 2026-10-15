"""真实 Responses 流式 API 的稳定端到端场景。"""

from __future__ import annotations

from support.live import drain_runtime_closed, run_submission


def test_streaming_text_turn(live_runtime):
    runtime, store = live_runtime
    _, finished, events, _ = run_submission(
        runtime, "请只用一句中文回答：真实流式文本测试通过。")

    assert finished.payload["outcome"]["status"] == "completed"
    assert [event for event in events if event.type == "assistant_text_delta"]
    assert any(event.type == "display_record" for event in events)
    assert runtime.session.session_id is not None
    assert store.load(runtime.session.session_id)["messages"]


def test_streaming_write_and_read_tools(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请调用 write_file 在当前目录创建 live-api-test.txt，内容写入 "
        "live stream tool ok；然后调用 read_file 读回并确认内容。")

    assert finished.payload["outcome"]["status"] == "completed"
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    tool_names = [record["payload"].get("name") for record in records
                  if record["type"] == "tool_call"]
    assert "write_file" in tool_names
    assert "read_file" in tool_names
    target = runtime.session.store.workdir / "live-api-test.txt"
    assert target.read_text(encoding="utf-8").strip() == "live stream tool ok"


def test_streaming_edit_and_glob_tools(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请依次调用 write_file 创建 live-api-edit.txt，内容为 alpha；"
        "再调用 edit_file 把 alpha 替换为 beta；最后调用 glob 查找 live-api-edit.txt。")

    assert finished.payload["outcome"]["status"] == "completed"
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    tool_names = [record["payload"].get("name") for record in records
                  if record["type"] == "tool_call"]
    assert {"write_file", "edit_file", "glob"}.issubset(tool_names)
    target = runtime.session.store.workdir / "live-api-edit.txt"
    assert target.read_text(encoding="utf-8") == "beta"


def test_streaming_bash_tool_round_trip(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, approvals = run_submission(
        runtime, "请调用 bash 执行 printf 'live bash ok'，然后报告命令输出。")

    assert finished.payload["outcome"]["status"] == "completed"
    assert ("bash", "approve") in approvals
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    tool_names = [record["payload"].get("name") for record in records
                  if record["type"] == "tool_call"]
    assert "bash" in tool_names
    tool_results = [record for record in records if record["type"] == "tool_result"]
    assert any("live bash ok" in record["payload"].get("content", "")
               for record in tool_results)


def test_streaming_bash_denial_reaches_the_model(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, approvals = run_submission(
        runtime,
        "请调用 bash 执行 touch live-api-denied.txt；即使被拒绝也请简短说明。",
        approve_tools=False)

    assert finished.payload["outcome"]["status"] == "completed"
    assert ("bash", "deny") in approvals
    assert not (runtime.session.store.workdir / "live-api-denied.txt").exists()
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    assert any(record["type"] == "tool_result"
               and record["payload"].get("is_error") for record in records)


def test_streaming_missing_file_error_reaches_the_model(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请调用 read_file 读取 definitely-missing-live-file.txt；收到错误后说明文件不存在。")

    assert finished.payload["outcome"]["status"] == "completed"
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    assert any(record["type"] == "tool_call"
               and record["payload"].get("name") == "read_file" for record in records)
    assert any(record["type"] == "tool_result"
               and record["payload"].get("is_error") for record in records)


def test_streaming_todo_tool_updates_lead_state(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请调用 todo_write，创建一个待办：真实 API 待办验证，状态设为 pending。")

    assert finished.payload["outcome"]["status"] == "completed"
    assert any(event.type == "display_record"
               and event.payload["record"]["type"] == "todo_updated"
               for event in events)
    assert any(todo["content"] == "真实 API 待办验证"
               and todo["status"] == "pending"
               for todo in runtime.lead_context.todos)


def test_streaming_task_tool_persists_task_state(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请调用 create_task 创建任务，标题为真实 API 任务验证，描述写成 live test。")

    assert finished.payload["outcome"]["status"] == "completed"
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    assert any(record["type"] == "tool_call"
               and record["payload"].get("name") == "create_task" for record in records)
    assert list((runtime.session.store.workdir / ".tasks").glob("task_*.json"))


def test_streaming_task_board_can_list_and_complete_created_task(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请创建一个任务，标题为真实 API 任务板生命周期，描述为 live lifecycle；"
        "拿到任务 ID 后调用 list_tasks 查看它，再 claim_task 认领，最后 complete_task 完成。")

    assert finished.payload["outcome"]["status"] == "completed"
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    tool_names = [record["payload"].get("name") for record in records
                  if record["type"] == "tool_call"]
    assert {"create_task", "list_tasks", "claim_task", "complete_task"}.issubset(tool_names)
    task_files = list((runtime.session.store.workdir / ".tasks").glob("task_*.json"))
    assert task_files
    import json
    assert any(json.loads(path.read_text(encoding="utf-8"))["status"] == "completed"
               for path in task_files)


def test_streaming_multiple_turns_preserve_history_and_usage(live_runtime):
    runtime, store = live_runtime
    _, first, first_events, _ = run_submission(
        runtime, "请只回答：第一轮真实流式完成。")
    _, second, second_events, _ = run_submission(
        runtime, "请只回答：第二轮真实流式完成。")

    assert first.payload["outcome"]["status"] == "completed"
    assert second.payload["outcome"]["status"] == "completed"
    assert [event for event in first_events + second_events
            if event.type == "assistant_text_delta"]
    snapshot = store.load(runtime.session.session_id)
    assert len(snapshot["messages"]) >= 4
    assert snapshot["messages"][0]["role"] == "user"
    assert snapshot["messages"][1]["role"] == "assistant"
    assert first.payload["summary"]["lead_usage"]["model_calls"] >= 1
    assert second.payload["summary"]["lead_usage"]["model_calls"] >= 1
    assert runtime.turns.usage_snapshot()["model_calls"] >= 2


def test_streaming_new_and_resume_commands_switch_sessions(live_runtime):
    runtime, store = live_runtime
    _, first, _, _ = run_submission(runtime, "请只回答：旧会话已保存。")
    assert first.payload["outcome"]["status"] == "completed"
    old_session_id = runtime.session.session_id

    _, created, _, _ = run_submission(runtime, "/new")
    assert created.payload["status"] == "completed"
    new_session_id = runtime.session.session_id
    assert new_session_id != old_session_id
    assert store.load(old_session_id)["messages"]

    _, resumed, _, _ = run_submission(runtime, f"/resume {old_session_id}")
    assert resumed.payload["status"] == "completed"
    assert runtime.session.session_id == old_session_id
    assert runtime.session.history == store.load(old_session_id)["messages"]


def test_streaming_session_can_be_restored(live_runtime):
    runtime, store = live_runtime
    _, first, _, _ = run_submission(runtime, "请只回答：第一轮已保存。")
    assert first.payload["outcome"]["status"] == "completed"
    session_id = runtime.session.session_id
    assert session_id is not None
    assert store.load(session_id)["messages"]
    assert drain_runtime_closed(runtime)

    from codefox.runtime.interactive_runtime import InteractiveRuntime

    restored = InteractiveRuntime(store=store)
    restored.start()
    try:
        _, command, _, _ = run_submission(restored, f"/resume {session_id}")
        assert command.payload["status"] == "completed"
        assert restored.session.session_id == session_id
        assert restored.session.history == store.load(session_id)["messages"]
    finally:
        assert drain_runtime_closed(restored)


def test_streaming_once_subagent_round_trip(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "请调用 spawn_subagent 创建一个一次性子 agent：name 用 live_helper，"
        "description 写‘Reply child live’，"
        "prompt 写‘只回复 child live ok’，mode 设为 once，role 设为 coder，"
        "tools 设为空数组。等待子 agent 完成后，简短报告结果。")

    assert finished.payload["outcome"]["status"] == "completed"
    records = [event.payload["record"] for event in events
               if event.type == "display_record"]
    assert any(record["type"] == "tool_call"
               and record["payload"].get("name") == "spawn_subagent"
               for record in records)
    assert any(event.type == "subagent_status" for event in events)


def test_streaming_bounded_read_only_subagent_audit(live_runtime):
    """Audit real source ranges with a discover/read whitelist and bounded work."""
    import json
    from pathlib import Path
    from codefox.agents import subagent

    runtime, store = live_runtime
    source = (Path(__file__).resolve().parents[2] / "src" / "codefox" /
              "agents" / "subagent.py").read_text(encoding="utf-8")
    target = store.workdir / "src" / "codefox" / "agents" / "subagent.py"
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    lines = source.splitlines()
    start = next(index for index, line in enumerate(lines)
                 if line.startswith("def spawn_subagent("))
    branch = next(index for index in range(start, len(lines))
                  if lines[index].strip() == 'if mode == "persistent":'
                  and any("def run_persistent" in item for item in lines[index:index + 5]))
    delegation = ("先用 glob 查找 src/**/subagent.py，然后只用 read_file 读取 "
                  f"src/codefox/agents/subagent.py，offset={branch}、limit=34。"
                  "只根据这些源码简短说明 once 与 persistent 如何启动。"
                  "最多调用 glob 和 read_file 各一次，不读整文件、其他文件或会话归档。"
                  "不创建任务或工作树，不修改文件，完成后直接给出结论。")
    _, finished, events, approvals = run_submission(
        runtime,
        "请实际调用 spawn_subagent：name=read-only-audit，description='Audit child startup'，"
        "mode=once，tools=[read_file, glob]，max_rounds=6，max_tool_calls=6。"
        f"子 agent 的 prompt 使用以下委派：{delegation}"
        "只创建一次子 agent；等待结果后简短报告结论，出错就报告原因并结束。",
        timeout=300,
    )
    assert finished.payload["outcome"]["status"] == "completed", finished.payload["outcome"]
    calls = [event for event in events if event.type == "tool_call"]
    creation = [event for event in calls if event.payload["name"] == "spawn_subagent"]
    assert len(creation) == 1
    results = [json.loads(event.payload["content"]) for event in events
               if event.type == "tool_result" and event.payload.get("tool_use_id") == creation[0].payload["id"]]
    assert len(results) == 1
    result = results[0]
    assert result["status"] == "completed" and result["text"].strip(), result
    assert result["description"] == "Audit child startup"
    assert 1 <= result["rounds"] <= 6 and result["tool_calls"] <= 6
    child_calls = [event for event in calls if event.source == "read-only-audit"]
    assert {event.payload["name"] for event in child_calls} == {"glob", "read_file"}
    reads = [event.payload["input"] for event in child_calls if event.payload["name"] == "read_file"]
    assert len(reads) == 1 and reads[0]["offset"] == branch and reads[0]["limit"] == 34
    assert approvals == []
    assert target.read_text(encoding="utf-8") == source
    assert subagent.get_subagent_output(result["agent_id"], block=False) == result
    assert not subagent.active_names()
    print("\nRead-only subagent audit:", json.dumps(
        {key: result[key] for key in ("agent_id", "status", "rounds", "tool_calls", "usage")},
        ensure_ascii=False))


def test_streaming_goal_followup_is_evaluated(live_runtime):
    runtime, _ = live_runtime
    _, finished, events, _ = run_submission(
        runtime,
        "/goal 回复中必须包含短语：真实 API 目标已完成。",
        terminal_types=("turn_finished",))

    assert finished.payload["outcome"]["status"] == "completed"
    assert any(event.type == "command_result"
               and event.payload.get("command") == "/goal"
               for event in events)
    assert any(event.type == "execution_status"
               and event.payload.get("phase") == "goal"
               and event.payload.get("state") == "completed"
               for event in events)
    snapshots = [event.payload for event in events if event.type == "state_snapshot"]
    assert snapshots and snapshots[-1]["goal"]["active"] is None


def test_streaming_memory_extracts_a_durable_preference(live_runtime):
    runtime, _ = live_runtime
    import codefox.core as core

    core.MEMORY_ENABLED = True
    _, finished, events, _ = run_submission(
        runtime,
        "请记住一个跨会话的长期偏好：用户希望所有回答都使用简体中文。"
        "请只用一句中文确认。")

    assert finished.payload["outcome"]["status"] == "completed"
    assert any(event.type == "execution_status"
               and event.payload.get("phase") == "memory"
               for event in events)
    memory_index = runtime.session.store.workdir / ".memory" / "MEMORY.md"
    assert memory_index.exists()
    assert list((runtime.session.store.workdir / ".memory").glob("*.md"))
