"""上下文压缩测试：工具结果配对与分层缩减。"""

from __future__ import annotations

import json
from types import SimpleNamespace


def tool_use(tool_id: str) -> dict:
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "bash",
         "input": {"command": "echo hi"}}]}


def tool_result(tool_id: str, text: str) -> dict:
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}


def test_context_thresholds_follow_the_main_budget(host_factory) -> None:
    core = host_factory().core

    assert core.CONTEXT_LIMIT > 0
    assert core.COMPACT_TRIGGER == int(core.CONTEXT_LIMIT * 0.8)
    assert core.COMPACT_TARGET == int(core.CONTEXT_LIMIT * 0.7)
    assert core.TOOL_RESULT_BUDGET == int(core.CONTEXT_LIMIT * 0.16)
    assert core.PERSIST_THRESHOLD == int(core.CONTEXT_LIMIT * 0.08)
    assert core.TOOL_PREVIEW_CHARS == int(core.CONTEXT_LIMIT * 0.02)


def test_prepare_compacts_only_above_trigger(host_factory, monkeypatch) -> None:
    host = host_factory()
    requests = []

    def compact_history(messages, active_request):
        requests.append(active_request)
        return [{"role": "user", "content": "summary"}]

    monkeypatch.setattr(host.compaction, "compact_history", compact_history)
    below = [{"role": "user", "content": "x" * (host.core.COMPACT_TRIGGER - 100)}]
    above = [{"role": "user", "content": "x" * host.core.COMPACT_TRIGGER}]

    host.compaction.prepare_history(below, "below")
    host.compaction.prepare_history(above, "above")

    assert requests == ["above"]
    assert above == [{"role": "user", "content": "summary"}]


def test_prepare_stops_below_trigger_without_forcing_target(
        host_factory, monkeypatch) -> None:
    host = host_factory()
    targets = []

    def micro_compact(messages, target_chars):
        targets.append(target_chars)
        return [{"role": "user", "content": "x" * (host.core.COMPACT_TRIGGER - 100)}]

    def unexpected(*args):
        raise AssertionError("already below the compression trigger")

    monkeypatch.setattr(host.compaction, "micro_compact", micro_compact)
    monkeypatch.setattr(host.compaction, "fit_tool_results", unexpected)
    monkeypatch.setattr(host.compaction, "compact_history", unexpected)
    messages = [{"role": "user", "content": "x" * host.core.COMPACT_TRIGGER}]

    host.compaction.prepare_history(messages, "go")

    assert targets == [host.core.COMPACT_TARGET]
    assert host.compaction.estimate_size(messages) > host.core.COMPACT_TARGET
    assert host.compaction.estimate_size(messages) < host.core.COMPACT_TRIGGER


def test_prepare_preserves_results_below_the_pressure_limit(host_factory) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}, tool_use("t1"),
                tool_result("t1", "x" * 500)]

    result = host.compaction.prepare_history(messages, "go")

    assert not result.applied and result.reason is None
    assert messages[2]["content"][0]["content"] == "x" * 500


def test_prepare_persists_oversized_result_before_trimming(host_factory) -> None:
    host = host_factory()
    messages = [
        {"role": "user", "content": "go"},
        tool_use("t1"),
        tool_result("t1", "y" * (host.core.COMPACT_TRIGGER // 2 + 1000)),
        tool_use("t2"),
        tool_result("t2", "z" * (host.core.COMPACT_TRIGGER // 2 + 1000)),
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": "next"},
    ]

    result = host.compaction.prepare_history(messages, "go")

    assert result.applied and result.reason is None
    rendered = json.dumps(messages, default=str)
    assert "<persisted-output>" in rendered or "[Earlier tool result saved at" in rendered


def test_tool_result_budget_persists_a_single_large_result(host_factory) -> None:
    host = host_factory()
    output = "x" * (host.core.PERSIST_THRESHOLD + 1)
    messages = [tool_result("large", output)]

    host.compaction.tool_result_budget(messages)

    result = messages[0]["content"][0]["content"]
    assert result.startswith("<persisted-output>")
    assert f"Preview:\n{'x' * host.core.TOOL_PREVIEW_CHARS}\n" in result
    assert (host.core.tool_results_dir() / "large.txt").read_text(encoding="utf-8") == output


def test_tool_result_budget_reduces_batch_below_individual_limit(host_factory) -> None:
    host = host_factory()
    output = "x" * (host.core.PERSIST_THRESHOLD - 1000)
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{index}", "content": output}
        for index in range(3)
    ]}]

    host.compaction.tool_result_budget(messages)

    results = [block["content"] for block in messages[0]["content"]]
    assert results[0].startswith("<persisted-output>")
    assert results[1:] == [output, output]
    assert sum(map(len, results)) <= host.core.TOOL_RESULT_BUDGET


def test_micro_compact_keeps_unseen_tool_result_batch(host_factory) -> None:
    host = host_factory()
    messages = [
        {"role": "user", "content": "go"},
        tool_use("t1"),
        tool_result("t1", "a" * 5000),
        {"role": "assistant", "content": [{"type": "text", "text": "seen"}]},
        tool_use("t2"),
        tool_result("t2", "b" * 5000),
        tool_use("t3"),
        tool_result("t3", "c" * 5000),
        tool_use("t4"),
        tool_result("t4", "d" * 5000),
        tool_use("t5"),
        tool_result("t5", "e" * 5000),
    ]

    host.compaction.micro_compact(messages, target_chars=1)

    # 模型尚未处理的结果保持原文。
    assert messages[5]["content"][0]["content"] == "b" * 5000
    assert messages[11]["content"][0]["content"] == "e" * 5000
    # 已处理的结果移到磁盘，并通过路径引用。
    assert messages[2]["content"][0]["content"].startswith(
        "[Earlier tool result saved at "
    )


def test_micro_compact_releases_batch_after_model_consumes_it(
    host_factory,
) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}]
    for index in range(6):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", "x" * 5000))
        messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "seen"}]}
        )

    host.compaction.micro_compact(messages, target_chars=1)

    saved = {path.name for path in host.core.tool_results_dir().glob("*.txt")}
    assert {"t0.txt", "t1.txt", "t2.txt"} <= saved
    # 除最新结果外，其余内容都已移到磁盘。
    for result_index in (2, 5):
        assert messages[result_index]["content"][0]["content"].startswith(
            "[Earlier tool result saved at "
        )
    assert messages[-2]["content"][0]["content"] == "x" * 5000


def test_snip_compact_keeps_head_and_tail_tool_pairs(host_factory) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}]
    for index in range(30):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", f"r{index}"))

    compacted = host.compaction.snip_compact(messages, max_messages=20)

    assert len(compacted) == 20
    assert compacted[0] == {"role": "user", "content": "go"}
    assert compacted[1]["content"][0]["type"] == "tool_use"
    assert compacted[2]["content"][0]["type"] == "tool_result"
    archive = compacted[3]["content"]
    assert "42 messages archived at" in archive
    assert compacted[-2]["content"][0]["type"] == "tool_use"
    assert compacted[-1]["content"][0]["type"] == "tool_result"


def test_snip_compact_archives_the_complete_history(host_factory) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}]
    for index in range(30):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", f"r{index}"))

    before = set(host.core.transcript_dir().glob("transcript_*.jsonl"))
    compacted = host.compaction.snip_compact(messages, max_messages=20)

    written = set(host.core.transcript_dir().glob("transcript_*.jsonl")) - before
    assert len(written) == 1
    lines = written.pop().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 61
    assert "42 messages archived at" in compacted[3]["content"]


def test_reactive_compact_keeps_the_tail_tool_pair(host_factory, monkeypatch) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}]
    for index in range(8):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", f"r{index}"))

    monkeypatch.setattr(host.compaction, "summarize_history", lambda _: "summary")
    result = host.compaction.reactive_compact(messages, "go")

    assert result.applied
    assert messages[-1]["content"][0]["type"] == "tool_result"
    assert messages[-2]["content"][0]["type"] == "tool_use"
    assert messages[0]["content"].startswith("[Reactive compact]")


def test_reactive_compact_summarizes_only_old_history(host_factory) -> None:
    host = host_factory()
    seen = {}

    def respond(**kwargs):
        seen["messages"] = kwargs["messages"]
        from types import SimpleNamespace
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="summary")])

    host.core.client = SimpleNamespace(
        messages=SimpleNamespace(create=respond))
    messages = [{"role": "user", "content": "old"}]
    for index in range(8):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", f"r{index}"))

    host.compaction.reactive_compact(messages, "go")

    summarized = json.dumps(seen["messages"], default=str)
    assert "r7" not in summarized
    assert "r6" not in summarized


def test_manual_compact_runs_once_per_user_turn(host_factory, monkeypatch) -> None:
    host = host_factory()
    monkeypatch.setattr(host.compaction, "summarize_history", lambda _: "summary")

    def compact_response(tool_id: str):
        return SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(
                type="tool_use", id=tool_id, name="compact", input={}
            )],
        )

    def text_response():
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
        )

    responses = [
        compact_response("first"),
        compact_response("duplicate"),
        text_response(),
        compact_response("next-turn"),
        text_response(),
    ]
    offered_tools = []

    def create(**kwargs):
        offered_tools.append({tool["name"] for tool in kwargs["tools"]})
        return responses.pop(0)

    host.core.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    messages = [{"role": "user", "content": "Compress the context"}]

    host.agent_loop(messages, "Compress the context")

    assert len(list(host.core.transcript_dir().glob("transcript_*.jsonl"))) == 1
    assert "Compress the context" in messages[0]["content"]
    assert messages[-1]["content"][0].text == "done"

    messages.append({"role": "user", "content": "Compress again"})
    host.agent_loop(messages, "Compress again")

    assert len(list(host.core.transcript_dir().glob("transcript_*.jsonl"))) == 2
    assert ["compact" in names for names in offered_tools] == [
        True, False, False, True, False,
    ]
    assert responses == []


def test_manual_compact_handles_duplicate_calls_in_one_batch(
        host_factory, monkeypatch) -> None:
    host = host_factory()
    summaries = []

    def compact_history(messages, active_request):
        summaries.append((list(messages), active_request))
        return [{"role": "user", "content": "summary"}]

    monkeypatch.setattr(host.compaction, "compact_history", compact_history)
    manual = host.compaction.ManualCompaction()
    tools = [{"name": "compact"}, {"name": "bash"}]
    assert manual.available_tools(tools) == tools
    assert manual.handle_tool_use(SimpleNamespace(name="bash", id="other")) is None

    first = manual.handle_tool_use(SimpleNamespace(name="compact", id="first"))
    duplicate = manual.handle_tool_use(SimpleNamespace(name="compact", id="second"))
    assert first == {"type": "tool_result", "tool_use_id": "first",
                     "content": "[Compaction requested. This completed turn will be summarized.]"}
    assert duplicate == {"type": "tool_result", "tool_use_id": "second",
                         "content": "[Compaction already requested for this user turn.]"}
    assert manual.available_tools(tools) == [{"name": "bash"}]

    messages = [{"role": "user", "content": [first, duplicate]}]
    manual.apply_after_results(messages, "ask")
    manual.apply_after_results(messages, "ask")
    assert summaries == [([{"role": "user", "content": [first, duplicate]}], "ask")]
    assert messages == [{"role": "user", "content": "summary"}]
