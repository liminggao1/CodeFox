"""Teammate execution is testable without waiting for a real thread."""

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("fail", [False, True, "truncated_text", "truncated_tool"])
def test_teammate_completion_and_failure_release_assignment(host_factory, monkeypatch, fail):
    host = host_factory()
    task = host.tasks.create_task("Session task")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if fail is True:
            raise RuntimeError("model failed")
        if fail == "truncated_text":
            return SimpleNamespace(stop_reason="max_tokens", content=[
                SimpleNamespace(type="text", text="Partial result")])
        if fail == "truncated_tool":
            return SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(
                type="tool_use", id="finish", name="complete_task", input={"task_id": task.id}
            )])
        if len(calls) == 1:
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use", id="finish", name="complete_task", input={"task_id": task.id}
            )])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Finished")])

    monkeypatch.setattr(host.core, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(host.teams.threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))

    def shutdown(name, timeout):
        host.teams.run_request_shutdown(name)
        return host.teams.BUS.read_inbox(name)

    monkeypatch.setattr(host.teams.BUS, "wait_for_messages", shutdown)
    host.teams.spawn_teammate_thread("alice", "tester", "Finish the task", task.id)

    assert "alice" not in host.tasks.teammate_assignments
    assert "alice" not in host.teams.active_teammates
    assert host.tasks.load_task(task.id).status == ("pending" if fail else "completed")
    inbox = host.teams.BUS.read_inbox("lead")
    assert any(msg["type"] == ("error" if fail else "result") for msg in inbox)
    if fail:
        assert not any(msg["type"] == "result" for msg in inbox)
        assert len(calls[0]["messages"]) == 1
    assert "[Assigned task" in calls[0]["messages"][0]["content"]
