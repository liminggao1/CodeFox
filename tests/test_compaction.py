"""上下文压缩测试：工具结果配对与分层缩减；失败时保留历史，不终止可用回合。"""

from __future__ import annotations

from types import SimpleNamespace
import copy
import json

import pytest

import codefox.context.compaction as compaction
import codefox.core as core
from codefox.runtime.interaction import (ExecutionContext, InteractionChannel,
                                                execution_context)
import codefox.runtime.scheduler as scheduler
from support.fakes import blocks, text_block, tool


def tool_use(tool_id: str) -> dict:
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": "bash",
         "input": {"command": "echo hi"}}]}


def tool_result(tool_id: str, text: str) -> dict:
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}


def token_text(tokens: int) -> str:
    """Use one CJK character per estimated token in threshold tests."""
    return "中" * tokens


def test_session_bound_compaction_artifacts_use_session_directory(host_factory, tmp_path) -> None:
    host_factory()
    session_id = "20261013-000000-deadbeef"
    context = ExecutionContext(InteractionChannel(), session_id, 0)
    with execution_context(context):
        output = compaction.save_output("call-1", "large output")
        transcript = compaction.write_transcript([{"role": "user", "content": "hello"}])

    root = core.session_artifacts_dir(session_id)
    assert output == root / "tool-results" / "call-1.txt"
    assert transcript == root / "transcripts" / transcript.name
    assert output.read_text(encoding="utf-8") == "large output"
    assert transcript.exists()
    assert not (tmp_path / ".task_outputs" / "tool-results" / "call-1.txt").exists()


def test_prepare_persists_oversized_result_before_trimming(host_factory) -> None:
    host = host_factory()
    messages = [
        {"role": "user", "content": "go"},
        tool_use("t1"),
        tool_result("t1", token_text(core.COMPACT_TRIGGER // 2 + 1000)),
        tool_use("t2"),
        tool_result("t2", token_text(core.COMPACT_TRIGGER // 2 + 1000)),
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": "next"},
    ]

    result = compaction.prepare_history(messages, "go")

    assert result.applied and result.reason is None
    rendered = json.dumps(messages, default=str)
    assert "<persisted-output>" in rendered or "[Earlier tool result saved at" in rendered


def test_tool_result_budget_reduces_batch_below_individual_limit(host_factory) -> None:
    host = host_factory()
    large = "x" * (core.PERSIST_THRESHOLD + 1)
    messages = [tool_result("large", large)]
    compaction.tool_result_budget(messages)
    preview = messages[0]["content"][0]["content"]
    assert preview.startswith("<persisted-output>")
    assert f"Preview:\n{'x' * core.TOOL_PREVIEW_CHARS}\n" in preview
    assert (core.tool_results_dir() / "large.txt").read_text(encoding="utf-8") == large

    output_chars = min(core.PERSIST_THRESHOLD - 1, int(core.TOOL_RESULT_BUDGET * 0.4))
    output = "中" * output_chars
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{index}", "content": output}
        for index in range(3)
    ]}]

    compaction.tool_result_budget(messages)

    results = [block["content"] for block in messages[0]["content"]]
    assert results[0].startswith("<persisted-output>")
    assert results[1:] == [output, output]
    assert sum(compaction.estimate_text_tokens(text) for text in results) <= core.TOOL_RESULT_BUDGET


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

    compaction.micro_compact(messages, target_tokens=1)

    # 模型尚未处理的结果保持原文。
    assert messages[5]["content"][0]["content"] == "b" * 5000
    assert messages[11]["content"][0]["content"] == "e" * 5000
    # 已处理的结果移到磁盘，并通过路径引用。
    assert messages[2]["content"][0]["content"].startswith(
        "[Earlier tool result saved at "
    )
    messages.append({"role": "assistant", "content": "consumed"})
    compaction.micro_compact(messages, target_tokens=1)
    assert messages[5]["content"][0]["content"].startswith("[Earlier tool result saved at ")
    assert messages[-2]["content"][0]["content"] == "e" * 5000


def test_snip_compact_keeps_head_and_tail_tool_pairs(host_factory) -> None:
    host = host_factory()
    messages = [{"role": "user", "content": "go"}]
    for index in range(30):
        messages.append(tool_use(f"t{index}"))
        messages.append(tool_result(f"t{index}", f"r{index}"))

    compacted = compaction.snip_compact(messages, max_messages=20)

    assert len(compacted) == 20
    assert compacted[0] == {"role": "user", "content": "go"}
    assert compacted[1]["content"][0]["type"] == "tool_use"
    assert compacted[2]["content"][0]["type"] == "tool_result"
    archive = compacted[3]["content"]
    assert "42 messages archived at" in archive
    assert compacted[-2]["content"][0]["type"] == "tool_use"
    assert compacted[-1]["content"][0]["type"] == "tool_result"
    archives = list(core.transcript_dir().glob("transcript_*.jsonl"))
    assert len(archives) == 1
    assert [json.loads(line) for line in archives[0].read_text(encoding="utf-8").splitlines()] == messages


def test_reactive_compaction_keeps_tail_tool_use_with_its_result(host_factory, monkeypatch):
    host_factory()
    monkeypatch.setattr(core, "COMPACTION_TAIL_MESSAGES", 1)
    summarized = []
    monkeypatch.setattr(compaction, "summarize_history",
                        lambda messages: summarized.append(copy.deepcopy(messages)) or "summary")
    messages = [{"role": "user", "content": "earlier request"},
                {"role": "assistant", "content": "earlier answer"},
                tool_use("tail"), tool_result("tail", "latest evidence")]
    original = copy.deepcopy(messages)

    result = compaction.reactive_compact(messages, "continue")

    assert result.applied
    assert summarized == [original[:2]]
    assert messages[-2:] == original[-2:]
    assert messages[0]["content"].startswith("[Reactive compact]")
    assert "continue" in messages[0]["content"]


def test_manual_compact_runs_once_per_user_turn(host_factory, monkeypatch) -> None:
    host = host_factory()
    monkeypatch.setattr(compaction, "summarize_history", lambda _: "summary")

    responses = [
        blocks(tool("compact", "first"), stop_reason="tool_use"),
        blocks(tool("compact", "duplicate"), stop_reason="tool_use"),
        blocks(text_block("done")),
        blocks(tool("compact", "next-turn"), stop_reason="tool_use"),
        blocks(text_block("done")),
    ]
    offered_tools = []

    def create(**kwargs):
        offered_tools.append({tool["name"] for tool in kwargs["tools"]})
        return responses.pop(0)

    core.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    messages = [{"role": "user", "content": "Compress the context"}]

    host.agent_loop(messages, "Compress the context")

    assert len(list(core.transcript_dir().glob("transcript_*.jsonl"))) == 1
    assert "Compress the context" in messages[0]["content"]
    assert messages[-1]["content"][0].text == "done"

    messages.append({"role": "user", "content": "Compress again"})
    host.agent_loop(messages, "Compress again")

    assert len(list(core.transcript_dir().glob("transcript_*.jsonl"))) == 2
    assert ["compact" in names for names in offered_tools] == [
        True, True, True, True, True,
    ]
    assert responses == []


@pytest.mark.parametrize("context_error", [False, True])
def test_automatic_failure_allows_main_call_without_repeat(host_factory, monkeypatch, context_error):
    host = host_factory()
    summaries, main_calls = [], []
    history = [{"role": "user", "content": token_text(core.COMPACT_TRIGGER)}]
    original = copy.deepcopy(history)

    def fail_summary(messages, **kwargs):
        summaries.append(True)
        raise RuntimeError("summary unavailable")

    def create(**kwargs):
        main_calls.append(copy.deepcopy(kwargs["messages"]))
        if len(main_calls) == 1:
            assert kwargs["messages"][:len(original)] == original
            assert "Runtime context update" in kwargs["messages"][-1]["content"]
            if context_error:
                raise RuntimeError("context_length_exceeded")
            return blocks(tool("read_file", "read", path="missing.txt"), stop_reason="tool_use")
        return blocks(text_block("done"))

    monkeypatch.setattr(compaction, "summarize_history", fail_summary)
    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    host.agent_loop(history, "go")
    assert len(main_calls) == 2
    assert len(summaries) == (2 if context_error else 1)
    assert history[-1]["content"][0].text == "done"
    if context_error:
        assert history[0]["content"].startswith("[Reactive compact]")
    else:
        host.agent_loop(history, "next turn")
        assert len(summaries) == 2


def test_failed_candidate_preserves_nested_blocks_and_list_identity(host_factory, monkeypatch):
    host = host_factory()
    block = {"type": "tool_result", "tool_use_id": "x", "content": "original"}
    history = [{"role": "user", "content": [block]}]
    before = copy.deepcopy(history)

    def mutate_candidate(messages):
        messages[0]["content"][0]["content"] = "partial replacement"
        return messages

    def fail_archive(messages):
        raise OSError("archive failed")

    monkeypatch.setattr(compaction, "tool_result_budget", mutate_candidate)
    monkeypatch.setattr(compaction, "snip_compact", fail_archive)
    result = compaction.prepare_history(history, "go")
    assert not result.applied and "archive failed" in result.reason
    assert history == before
    assert history[0]["content"][0] is block
    assert compaction.prepare_history(history, "go", enabled=False).reason is None


def test_manual_failure_preserves_other_results_and_never_reruns_tools(host_factory, monkeypatch, no_approval):
    host = host_factory()
    calls, summaries = [], []
    responses = [blocks(
        tool("write_file", "write", path="once.txt", content="written"),
        tool("compact", "compact"), stop_reason="tool_use"), blocks(text_block("done"))]

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return responses.pop(0)

    def fail_summary(messages, **kwargs):
        summaries.append(copy.deepcopy(messages))
        raise RuntimeError("summary unavailable")

    monkeypatch.setattr(compaction, "summarize_history", fail_summary)
    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    history = [{"role": "user", "content": "write once"}]
    host.agent_loop(history, "write once")
    results = calls[1]["messages"][-1]["content"]
    assert "Wrote" in results[0]["content"]
    assert "Compaction failed" in results[1]["content"]
    assert results[1]["is_error"] is True
    assert (core.WORKDIR / "once.txt").read_text() == "written"
    assert len(summaries) == 1
    assert any(tool["name"] == "compact" for tool in calls[1]["tools"])


def test_reactive_archive_failure_ends_with_cron_restored(host_factory, monkeypatch):
    host = host_factory()
    job = scheduler.schedule_job("* * * * *", "cron work", recurring=False)
    scheduler._enqueue_due_job(job)

    def context_error(**kwargs):
        raise RuntimeError("context_length_exceeded")

    def fail_archive(messages):
        raise OSError("disk unavailable")

    monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=context_error)))
    monkeypatch.setattr(compaction, "write_transcript", fail_archive)
    history = [{"role": "user", "content": "go"}]
    host.agent_loop(history, "go")
    assert not any(message.get("role") == "assistant" and "Reactive compaction failed" in str(message.get("content"))
                   for message in history)
    assert scheduler._cron_queue == [job]
    assert history[0]["content"] == "go"


def test_summary_requires_complete_nonempty_text(host_factory, monkeypatch):
    host = host_factory()
    responses = [blocks(text_block("")), blocks(text_block("partial"), stop_reason="max_tokens"),
                 blocks(tool("read_file", "tool", path="x"), stop_reason="tool_use")]
    for value in responses:
        monkeypatch.setattr(core, "client", SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: value)))
        with pytest.raises(ValueError, match="complete, non-empty"):
            compaction.summarize_history([])
