"""目标层测试：控制器判定与 host 主循环集成。

控制器级用例覆盖判定规则；会话级用例用脚本化客户端驱动
agent_harness.host 主循环。
"""

from __future__ import annotations

import agent_harness.core as core


import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_harness.agents import goal as goal_module
from agent_harness.agents import goal_evaluator as evaluator_module


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


def goal_reply(text: str):
    """目标用例使用的文本响应，带固定 usage。"""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def goal_tool_reply(name: str, arguments: dict, tool_use_id: str = "tool-1"):
    """目标用例使用的工具调用响应，带固定 usage。"""
    return SimpleNamespace(
        content=[SimpleNamespace(
            type="tool_use", id=tool_use_id, name=name, input=arguments
        )],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


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


def controller(**kwargs):
    return goal_module.GoalController(evaluator=ScriptedEvaluator([]), **kwargs)


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


def test_evaluator_error_returns_control_and_keeps_goal():
    class Boom:
        async def evaluate(self, condition, messages):
            raise RuntimeError("evaluator offline")

    gc = goal_module.GoalController(evaluator=Boom())
    gc.set_goal("anything")

    decision = asyncio.run(gc.evaluate_after_turn([]))

    assert decision.action == "error"
    assert gc.active is not None
    assert "evaluator offline" in gc.last_status["reason"]


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


@pytest.mark.parametrize("alias", sorted(goal_module.CLEAR_ALIASES))
def test_clear_aliases(alias):
    gc = controller()
    gc.set_goal("something")
    assert "已清除目标" in gc.clear(alias)
    assert gc.active is None


def test_goal_length_is_bounded():
    gc = controller()
    with pytest.raises(goal_module.GoalError):
        gc.set_goal("x" * (goal_module.MAX_GOAL_LENGTH + 1))
    with pytest.raises(goal_module.GoalError):
        gc.set_goal("   ")


def test_evaluator_rejects_conflicting_terminal_states():
    payload = '{"ok": true, "reason": "done", "impossible": true}'
    with pytest.raises(goal_module.GoalError, match="both ok and impossible"):
        evaluator_module._parse_json_object(payload)
    with pytest.raises(goal_module.GoalError, match="invalid JSON"):
        evaluator_module._parse_json_object("not json")


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


@pytest.mark.parametrize(
    ("choice", "ok", "impossible"),
    [("complete", True, False), ("incomplete", False, False),
     ("impossible", False, True)],
)
def test_jev_evaluator_maps_typed_choice_to_goal_evaluation(
    choice, ok, impossible
):
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

    assert (result.ok, result.impossible) == (ok, impossible)
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


def test_goal_event_records_evaluator_confidence():
    gc = goal_module.GoalController(ScriptedEvaluator([
        goal_module.GoalEvaluation(True, "verified", confidence=0.97),
    ]))
    gc.set_goal("tests pass")

    decision = asyncio.run(gc.evaluate_after_turn([]))

    assert decision.action == "achieved"
    assert gc.last_status["confidence"] == 0.97


def test_host_can_select_jev_goal_evaluator_from_configuration(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setenv("GOAL_EVALUATOR_PROVIDER", "jev")
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    goal = host._create_goal()

    assert isinstance(goal.evaluator, evaluator_module.JevGoalEvaluator)


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


def test_transcript_keeps_recent_messages_whole():
    messages = [{"role": "user", "content": f"turn {i} " + "x" * 50}
                for i in range(10)]
    text = evaluator_module.transcript_text(messages, max_characters=200)
    assert "turn 9" in text
    assert "turn 0" not in text


def test_transcript_trims_the_middle_of_one_oversized_message():
    messages = [{"role": "user", "content": "START" + "y" * 5000 + "END"}]
    text = evaluator_module.transcript_text(messages, max_characters=400)
    assert "START" in text
    assert "END" in text
    assert "middle omitted" in text


def test_transcript_uses_configured_default_limit(monkeypatch):
    monkeypatch.setenv("GOAL_EVALUATOR_MAX_TRANSCRIPT_CHARACTERS", "120")
    messages = [{"role": "user", "content": "x" * 500}]

    text = evaluator_module.transcript_text(messages)

    assert len(text) <= 120


# -- Host 主循环集成测试 --


def test_unmet_goal_continues_automatically_until_achieved(host_factory):
    host, client, evaluator, messages = run_host_turn(
        host_factory,
        responses=[goal_reply("step 1"), goal_reply("step 2")],
        evaluations=[
            goal_module.GoalEvaluation(False, "no evidence yet"),
            goal_module.GoalEvaluation(True, "verified by pytest"),
        ],
    )

    assert len(client.calls) == 2
    assert any(
        "[Goal still active]" in str(message.get("content"))
        for message in messages
    )
    assert host.GOAL.active is None
    assert host.GOAL.last_status["met"] is True
    assert len(evaluator.seen) == 2


def test_worker_tool_result_reaches_the_goal_evaluator(host_factory):
    _, client, evaluator, _ = run_host_turn(
        host_factory,
        responses=[
            goal_tool_reply("todo_write", {"todos": [
                {"content": "write the test", "status": "pending"},
            ]}),
            goal_reply("test written"),
        ],
        evaluations=[goal_module.GoalEvaluation(True, "todo recorded")],
    )

    assert len(client.calls) == 2
    transcript = evaluator_module.transcript_text(evaluator.seen[0])
    assert "tool_use todo_write" in transcript
    assert "write the test" in transcript


def test_goal_is_not_consulted_without_an_active_condition(host_factory):
    host = host_factory()
    client = FakeClient([goal_reply("done")])
    core.client = client
    evaluator = ScriptedEvaluator([])
    host.GOAL = goal_module.GoalController(evaluator=evaluator)

    messages = [{"role": "user", "content": "just answer"}]
    host.agent_loop(messages, "just answer")

    assert evaluator.seen == []
    assert len(client.calls) == 1
