"""Compaction failures preserve history and do not terminate usable model turns."""

import copy
from types import SimpleNamespace

import pytest


def text_response(text="done", stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def tool_response(*calls):
    return SimpleNamespace(stop_reason="tool_use", content=[
        SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)
        for call_id, name, args in calls
    ])


@pytest.mark.parametrize("context_error", [False, True])
def test_automatic_failure_allows_main_call_without_repeat(host_factory, monkeypatch, context_error):
    host = host_factory()
    summaries, main_calls = [], []
    history = [{"role": "user", "content": "x" * host.core.COMPACT_TRIGGER}]
    original = copy.deepcopy(history)

    def fail_summary(messages):
        summaries.append(True)
        raise RuntimeError("summary unavailable")

    def create(**kwargs):
        main_calls.append(copy.deepcopy(kwargs["messages"]))
        if len(main_calls) == 1:
            assert kwargs["messages"] == original
            if context_error:
                raise RuntimeError("context_length_exceeded")
            return tool_response(("read", "read_file", {"path": "missing.txt"}))
        return text_response()

    monkeypatch.setattr(host.compaction, "summarize_history", fail_summary)
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
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

    monkeypatch.setattr(host.compaction, "tool_result_budget", mutate_candidate)
    monkeypatch.setattr(host.compaction, "snip_compact", fail_archive)
    result = host.compaction.prepare_history(history, "go")
    assert not result.applied and "archive failed" in result.reason
    assert history == before
    assert history[0]["content"][0] is block
    assert host.compaction.prepare_history(history, "go", enabled=False).reason is None


def test_manual_failure_preserves_other_results_and_never_reruns_tools(host_factory, monkeypatch):
    host = host_factory()
    calls, summaries = [], []
    responses = [tool_response(
        ("write", "write_file", {"path": "once.txt", "content": "written"}),
        ("compact", "compact", {})), text_response()]

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return responses.pop(0)

    def fail_summary(messages):
        summaries.append(copy.deepcopy(messages))
        raise RuntimeError("summary unavailable")

    monkeypatch.setattr(host.compaction, "summarize_history", fail_summary)
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    history = [{"role": "user", "content": "write once"}]
    host.agent_loop(history, "write once")
    results = calls[1]["messages"][-1]["content"]
    assert "Wrote" in results[0]["content"]
    assert "Compaction failed" in results[1]["content"]
    assert (host.core.WORKDIR / "once.txt").read_text() == "written"
    assert len(summaries) == 1
    assert all(tool["name"] != "compact" for tool in calls[1]["tools"])


def test_reactive_archive_failure_ends_with_cron_restored(host_factory, monkeypatch):
    host = host_factory()
    job = host.scheduler.schedule_job("* * * * *", "cron work", recurring=False)
    host.scheduler._enqueue_due_job(job)

    def context_error(**kwargs):
        raise RuntimeError("context_length_exceeded")

    def fail_archive(messages):
        raise OSError("disk unavailable")

    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=context_error)))
    monkeypatch.setattr(host.compaction, "write_transcript", fail_archive)
    history = [{"role": "user", "content": "go"}]
    host.agent_loop(history, "go")
    assert "Reactive compaction failed" in history[-1]["content"][0]["text"]
    assert host.scheduler.cron_queue == [job]
    assert history[0]["content"] == "go"


@pytest.mark.parametrize("response", [text_response(""), text_response("partial", "max_tokens"),
                                    tool_response(("tool", "read_file", {"path": "x"}))])
def test_summary_requires_complete_nonempty_text(host_factory, monkeypatch, response):
    host = host_factory()
    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: response)))
    with pytest.raises(ValueError, match="complete, non-empty"):
        host.compaction.summarize_history([])
