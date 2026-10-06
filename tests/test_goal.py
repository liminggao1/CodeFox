"""目标层测试：控制器判定与 host 主循环集成。

控制器级用例覆盖判定规则；会话级用例用脚本化客户端驱动
agent_harness.host 主循环。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_harness import goal as goal_module


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


def text_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def tool_response(name: str, arguments: dict, tool_use_id: str = "tool-1"):
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
    host.core.client = client
    evaluator = ScriptedEvaluator(evaluations)
    host.GOAL = goal_module.GoalController(evaluator=evaluator)
    host.GOAL.set_goal(condition)

    messages = [{"role": "user", "content": "Make the tests pass."}]
    context = host.update_context({}, messages)
    host.agent_loop(messages, context, "Make the tests pass.")
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
    assert "cleared" in gc.clear(alias)
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
        goal_module._parse_json_object(payload)
    with pytest.raises(goal_module.GoalError, match="invalid JSON"):
        goal_module._parse_json_object("not json")


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
    evaluator = goal_module.PromptGoalEvaluator(client=client, model="judge")

    result = asyncio.run(evaluator.evaluate("tests pass", [
        {"role": "user", "content": "ran pytest"},
    ]))

    assert result.ok is False
    assert result.reason == "missing test output"
    assert "tools" not in calls[0]
    assert calls[0]["model"] == "judge"
    assert "no tools" in calls[0]["system"].lower()


def test_transcript_keeps_recent_messages_whole():
    messages = [{"role": "user", "content": f"turn {i} " + "x" * 50}
                for i in range(10)]
    text = goal_module.transcript_text(messages, max_characters=200)
    assert "turn 9" in text
    assert "turn 0" not in text


def test_transcript_trims_the_middle_of_one_oversized_message():
    messages = [{"role": "user", "content": "START" + "y" * 5000 + "END"}]
    text = goal_module.transcript_text(messages, max_characters=400)
    assert "START" in text
    assert "END" in text
    assert "middle omitted" in text


# -- Host 主循环集成测试 --


def test_unmet_goal_continues_automatically_until_achieved(host_factory):
    host, client, evaluator, messages = run_host_turn(
        host_factory,
        responses=[text_response("step 1"), text_response("step 2")],
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
            tool_response("todo_write", {"todos": [
                {"content": "write the test", "status": "pending"},
            ]}),
            text_response("test written"),
        ],
        evaluations=[goal_module.GoalEvaluation(True, "todo recorded")],
    )

    assert len(client.calls) == 2
    transcript = goal_module.transcript_text(evaluator.seen[0])
    assert "tool_use todo_write" in transcript
    assert "write the test" in transcript


def test_goal_is_not_consulted_without_an_active_condition(host_factory):
    host = host_factory()
    client = FakeClient([text_response("done")])
    host.core.client = client
    evaluator = ScriptedEvaluator([])
    host.GOAL = goal_module.GoalController(evaluator=evaluator)

    messages = [{"role": "user", "content": "just answer"}]
    host.agent_loop(messages, host.update_context({}, messages), "just answer")

    assert evaluator.seen == []
    assert len(client.calls) == 1
