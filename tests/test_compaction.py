"""Context compaction tests: tool-result pairing and layered shrinking."""

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


def test_prepare_preserves_results_below_the_pressure_limit(host_factory) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}, tool_use("t1"),
                tool_result("t1", "x" * 500)]

    prepared = host.prepare_context(messages, "go")

    assert prepared[2]["content"][0]["content"] == "x" * 500


def test_prepare_persists_oversized_result_before_trimming(host_factory) -> None:
    host = host_factory()
    messages = [
        {"role": "user", "content": "go"},
        tool_use("t1"),
        tool_result("t1", "y" * (host.core.PERSIST_THRESHOLD + 1000)),
        tool_use("t2"),
        tool_result("t2", "z" * (host.core.PERSIST_THRESHOLD + 1000)),
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": "next"},
    ]

    prepared = host.prepare_context(messages, "go")

    rendered = json.dumps(prepared, default=str)
    assert "<persisted-output>" in rendered or "[Earlier tool result saved at" in rendered


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

    # Results the model has not answered yet stay verbatim.
    assert messages[5]["content"][0]["content"] == "b" * 5000
    assert messages[11]["content"][0]["content"] == "e" * 5000
    # The one consumed result is moved to disk and referenced by path.
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
    # Everything except the most recent results was moved to disk.
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


def test_reactive_compact_keeps_the_tail_tool_pair(host_factory) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}]
    for index in range(8):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", f"r{index}"))

    compacted = host.compaction.reactive_compact(messages, "go")

    assert compacted[-1]["content"][0]["type"] == "tool_result"
    assert compacted[-2]["content"][0]["type"] == "tool_use"
    assert compacted[0]["content"].startswith("[Reactive compact]")


def test_reactive_compact_summarizes_only_old_history(host_factory) -> None:
    host = host_factory()
    seen = {}

    def respond(**kwargs):
        seen["messages"] = kwargs["messages"]
        from types import SimpleNamespace
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="summary")])

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
