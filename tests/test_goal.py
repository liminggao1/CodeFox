"""目标层测试：控制器判定与 host 主循环集成。

控制器级用例覆盖判定规则；会话级用例用脚本化客户端驱动
codefox.host 主循环。
"""

from __future__ import annotations

import codefox.core as core


import asyncio
import json
from types import SimpleNamespace

import pytest

from codefox.agents import goal as goal_module
from codefox.agents import goal_evaluator as evaluator_module
from support.fakes import response, tool


# -- 测试夹具 --


class ScriptedEvaluator:
    def __init__(self, results):
        self.results = list(results)
        self.seen: list = []

    async def evaluate(self, condition, messages):
        self.seen.append(list(messages))
        if not self.results:
            raise AssertionError("no scripted evaluation left")
        return self.results.pop(0)


class FakeMessages:
    def __init__(self, outer):
        self._outer = outer

    def create(self, **kwargs):
        self._outer.calls.append(kwargs)
        if not self._outer.responses:
            raise AssertionError("no scripted response left")
        return self._outer.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []
        self.messages = FakeMessages(self)


def run_host_turn(host_factory, responses, evaluations, condition="tests pass"):
    host = host_factory()
    client = FakeClient(responses)
    core.client = client
    evaluator = ScriptedEvaluator(evaluations)
    host.GOAL = goal_module.GoalController(evaluator=evaluator)
    host.GOAL.set_goal(condition)

    messages = [{"role": "user", "content": "Make the tests pass."}]
    host.agent_loop(messages, "Make the tests pass.")
    return host, client, evaluator, messages


# -- 控制器级测试 --


def test_block_cap_returns_control_but_keeps_goal_active():
    evaluator = ScriptedEvaluator([goal_module.GoalEvaluation(False, "not yet")] * 3)
    gc = goal_module.GoalController(evaluator=evaluator, block_cap=2)
    gc.set_goal("ship it")

    decisions = [asyncio.run(gc.evaluate_after_turn([])) for _ in range(3)]

    assert [d.action for d in decisions] == ["block", "block", "limit"]
    assert gc.active is not None
    assert gc.active.condition == "ship it"


def test_impossible_goal_is_recorded_as_failed():
    evaluator = ScriptedEvaluator([
        goal_module.GoalEvaluation(False, "cannot run", impossible=True)
    ])
    gc = goal_module.GoalController(evaluator=evaluator)
    gc.set_goal("run the missing binary")

    decision = asyncio.run(gc.evaluate_after_turn([]))

    assert decision.action == "failed"
    assert gc.active is None
    assert gc.last_status["failed"] is True


def test_background_work_defers_evaluation():
    evaluator = ScriptedEvaluator([goal_module.GoalEvaluation(True, "looks done")])
    gc = goal_module.GoalController(evaluator=evaluator)
    gc.set_goal("finish")

    decision = asyncio.run(gc.evaluate_after_turn([], background_running=True))

    assert decision.action == "defer"
    assert evaluator.seen == []
    assert gc.active is not None


def test_restore_reinstalls_only_an_active_goal():
    evaluator = ScriptedEvaluator([])
    events = [{
        "type": "goal_status",
        "condition": "keep going",
        "active": True,
        "met": False,
        "failed": False,
        "reason": "goal set",
    }]

    restored = goal_module.GoalController.restore(evaluator, events)
    assert restored.active is not None
    assert restored.active.condition == "keep going"

    closed = [dict(events[0], active=False, met=True, reason="done")]
    finished = goal_module.GoalController.restore(evaluator, closed)
    assert finished.active is None
    assert finished.last_status["met"] is True
    for invalid in ("   ", "x" * (goal_module.MAX_GOAL_LENGTH + 1)):
        with pytest.raises(goal_module.GoalError):
            restored.set_goal(invalid)
    for alias in sorted(goal_module.CLEAR_ALIASES):
        restored.set_goal("something")
        assert "已清除目标" in restored.clear(alias)
        assert restored.active is None


def test_prompt_evaluator_uses_a_tool_free_json_response():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(
                type="text",
                text='{"ok": false, "reason": "missing test output", "impossible": false}',
            )]
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    evaluator = evaluator_module.PromptGoalEvaluator(client=client, model="judge")

    result = asyncio.run(evaluator.evaluate("tests pass", [
        {"role": "user", "content": "ran pytest"},
    ]))

    assert result.ok is False
    assert result.reason == "missing test output"
    assert "tools" not in calls[0]
    assert calls[0]["model"] == "judge"
    assert "no tools" in calls[0]["system"].lower()
    with pytest.raises(goal_module.GoalError, match="both ok and impossible"):
        evaluator_module._parse_json_object('{"ok":true,"reason":"done","impossible":true}')
    with pytest.raises(goal_module.GoalError, match="invalid JSON"):
        evaluator_module._parse_json_object("not json")


def test_evaluator_transcript_preserves_recent_evidence_within_budget(monkeypatch):
    messages = [{"role": "user", "content": f"turn {index} " + "x" * 50}
                for index in range(10)]
    recent = evaluator_module.transcript_text(messages, max_characters=200)
    assert len(recent) <= 200 and "turn 9" in recent and "turn 0" not in recent
    assert recent.endswith(messages[-1]["content"])

    monkeypatch.setenv("GOAL_EVALUATOR_MAX_TRANSCRIPT_CHARACTERS", "120")
    oversized = [{"role": "user", "content": "START" + "x" * 5000 + "END"}]
    text = evaluator_module.transcript_text(oversized)
    assert len(text) <= 120
    assert "START" in text and "middle omitted" in text and text.endswith("END")


def test_jev_evaluator_maps_typed_choice_to_goal_evaluation():
    for choice, ok, impossible in [
        ("complete", True, False), ("incomplete", False, False), ("impossible", False, True),
    ]:
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "code": 0,
                    "message": "ok",
                    "data": {"result": {"answers": {
                        "completion": {
                            "type": "choice", "choice": choice,
                            "probabilities": {
                                "complete": 0.95 if choice == "complete" else 0.02,
                                "incomplete": 0.03 if choice == "complete" else 0.95,
                                "impossible": 0.02 if choice == "complete" else 0.03,
                            },
                            "confidence": 0.95
                        }
                    }}},
                }).encode("utf-8")

        def opener(request, timeout):
            calls.append((request, timeout))
            return Response()

        evaluator = evaluator_module.JevGoalEvaluator(
            api_key="test-key",
            model="typesafe/jev-1.13",
            endpoint="https://jev.test/v1/systemone",
            opener=opener,
        )
        result = asyncio.run(evaluator.evaluate("tests pass", [
            {"role": "user", "content": "run pytest"},
        ]))

        assert (result.ok, result.impossible) == (ok, impossible), choice
        assert result.confidence == 0.95
        assert result.reason
        request, timeout = calls[0]
        assert timeout == 30
        assert request.get_header("Authorization") == "Bearer test-key"
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == "typesafe/jev-1.13"
        assert body["state"]["completion_condition"] == "tests pass"
        assert body["questions"]["completion"]["type"] == "choice"


def test_jev_completion_below_confidence_threshold_is_not_accepted():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "data": {"result": {"answers": {
                    "completion": {
                        "type": "choice", "choice": "complete",
                        "probabilities": {
                            "complete": 0.95,
                            "incomplete": 0.04,
                            "impossible": 0.01,
                        },
                        "confidence": 0.9
                    }
                }}},
            }).encode("utf-8")

    evaluator = evaluator_module.JevGoalEvaluator(
        api_key="test-key",
        completion_confidence_threshold=0.9,
        opener=lambda request, timeout: Response(),
    )
    result = asyncio.run(evaluator.evaluate("tests pass", []))

    assert result.ok is False
    assert result.impossible is False
    assert result.confidence == 0.9
    assert "confidence" in result.reason


def test_host_logs_raw_jev_response_to_session_diagnostics(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setenv("GOAL_EVALUATOR_PROVIDER", "jev")
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    published = []
    monkeypatch.setattr(
        host.interaction,
        "publish",
        lambda event_type, **payload: published.append((event_type, payload)),
    )
    raw_response = json.dumps({
        "data": {"result": {"answers": {"completion": {
            "choice": "complete",
            "probabilities": {"complete": 0.99, "incomplete": 0.01},
            "confidence": 0.98,
        }}}},
    })

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return raw_response.encode("utf-8")

    goal = host._create_goal()
    goal.evaluator._opener = lambda request, timeout: Response()

    result = asyncio.run(goal.evaluator.evaluate("tests pass", []))

    assert result.ok is True
    assert published == [("log", {"text": f"[Jev raw response]\n{raw_response}"})]


# -- Host 主循环集成测试 --


def test_unmet_goal_continues_automatically_until_achieved(host_factory):
    host, client, evaluator, messages = run_host_turn(
        host_factory,
        responses=[response("step 1", usage=(10, 5)), response("step 2", usage=(10, 5))],
        evaluations=[
            goal_module.GoalEvaluation(False, "no evidence yet"),
            goal_module.GoalEvaluation(True, "verified by pytest", confidence=0.97),
        ],
    )

    assert len(client.calls) == 2
    assert any(
        "[Goal still active]" in str(message.get("content"))
        for message in messages
    )
    assert host.GOAL.active is None
    assert host.GOAL.last_status["met"] is True
    assert host.GOAL.last_status["confidence"] == 0.97
    assert len(evaluator.seen) == 2


def test_worker_tool_result_reaches_the_goal_evaluator(host_factory):
    _, client, evaluator, _ = run_host_turn(
        host_factory,
        responses=[
            response(None, tools=[tool("todo_write", "tool-1", todos=[
                {"content": "write the test", "status": "pending"},
            ])], stop="tool_use", usage=(10, 5)),
            response("test written", usage=(10, 5)),
        ],
        evaluations=[goal_module.GoalEvaluation(True, "todo recorded")],
    )

    assert len(client.calls) == 2
    transcript = evaluator_module.transcript_text(evaluator.seen[0])
    assert "tool_use todo_write" in transcript
    assert "write the test" in transcript
