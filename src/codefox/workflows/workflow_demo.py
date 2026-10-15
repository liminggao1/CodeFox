"""Saved review workflow and deterministic offline demonstration."""

import json
from types import SimpleNamespace

from . import workflow_runtime
from .workflow_runtime import WorkflowInputError, _stable_hash

def _fill_schema(schema, seed):
    """为模拟器未特殊处理的 schema 生成确定性的通用填充值。"""
    t = schema.get("type")
    if t == "object":
        keys = schema.get("required") or list(schema.get("properties", {}))
        return {k: _fill_schema(schema["properties"][k], f"{seed}/{k}") for k in keys}
    if t == "array":
        return [_fill_schema(schema["items"], f"{seed}/0")]
    if t == "boolean":
        return _stable_hash(seed) % 4 != 0
    if t in ("number", "integer"):
        return _stable_hash(seed) % 5
    return seed.rsplit("/", 1)[-1]

class DemoModelClient:
    """Generate deterministic model responses for the real child execution path."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        """Return a complete fake response; never make a network request."""
        # 运行通知可能追加在任务后；示例模型只解析实际的 Workflow 步骤请求。
        prompt = next(message["content"] for message in reversed(kwargs["messages"])
                      if message["role"] == "user" and isinstance(message["content"], str)
                      and message["content"].startswith("Step label: "))
        marker = "Return only one JSON object matching this schema:\n"
        schema = json.loads(prompt.rsplit(marker, 1)[1]) if marker in prompt else None
        label = prompt.splitlines()[0].removeprefix("Step label: ")
        value = self._value(prompt, schema, label)
        text = json.dumps(value) if schema is not None else value
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=len(prompt) // 4, output_tokens=len(text) // 4))

    def _value(self, prompt, schema, label):
        if schema is None:
            return f"[mock] {(label or prompt)[:60]}"
        props = schema.get("properties", {})
        if "findings" in props:
            n = 1 + (_stable_hash(prompt) % 2)
            sev = ["high", "medium", "low"]
            value = {"findings": [
                {"title": f"{label or 'audit'} #{i + 1}",
                 "severity": sev[_stable_hash(prompt + str(i)) % 3]}
                for i in range(n)
            ]}
        elif "isReal" in props:
            real = _stable_hash(prompt) % 4 != 0
            value = {"isReal": real,
                     "reason": "reproduced" if real else "could not reproduce"}
        else:
            value = _fill_schema(schema, prompt)
        return value

FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "severity"],
        "properties": {
            "title": {"type": "string"},
            "severity": {
                "type": "string", "enum": ["high", "medium", "low"]
            },
        }}}},
}

VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}

SAMPLE_META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": ["Review", "Verify"],
}

DIMENSIONS = ["correctness", "security", "performance", "style"]

DEMO_CHANGES = (
    "def load_user(user_id):\n"
    "    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n"
    "    return db.execute(query).fetchone()\n"
)

async def sample_workflow(ctx, args):
    """按预设维度审查输入变更，再并发验证发现。
    输入：ctx 为编排上下文；args.changes 为供审查的代码或变更文本。
    返回：含 confirmed 的字典，只保留验证步骤确认的问题，并按严重程度排序。
    Steps use the unified child runtime with the installed real or offline model client.
    """
    # 标记当前工作流阶段为 Review（用于日志、计时、状态追踪）
    ctx.phase("Review")
    # 获取待审查的变更文本
    changes = args.get("changes", "")
    # 参数类型校验：变更内容必须是字符串，否则抛出工作流输入异常
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes must be a string")
    # 去除首尾空白；空输入时替换为占位提示文本
    review_input = changes.strip() or "No change context was supplied."

    # 单维度审计协程：针对单个审查维度调用agent扫描问题
    async def audit(_value, dimension, _idx):
        # 调用子Agent，针对当前维度扫描变更内容，输出固定schema结构的发现项
        out = await ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        # 返回该维度名称 + 扫描得到的问题列表
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _idx):
        # 切换工作流阶段标记为 Verify，用于日志、链路追踪
        ctx.phase("Verify")
        # 每个发现由独立的对抗式子 agent 并发验证。
        verdicts = await ctx.parallel([
            (lambda f=f: ctx.agent(
                f"Adversarially verify this {dimension} finding against the "
                "supplied change context.\n\n"
                f"Change context:\n{review_input}\n\n"
                f"Finding:\n{json.dumps(f, ensure_ascii=True)}",
                schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}", phase="Verify"))
            for f in audited["findings"]])
        # 将原始发现与验证结果配对，仅保留isReal标记为true的真实问题，过滤假阳性
        confirmed = [f for f, v in zip(audited["findings"], verdicts)
                     if v and v.get("isReal")]
        return {"dimension": dimension, "confirmed": confirmed}

    # 工作流流水线：遍历DIMENSIONS，先执行audit审计，再执行verify复核
    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    # 扁平化聚合所有维度中确认有效的问题，嵌入dimension标记来源维度
    confirmed = [{"dimension": r["dimension"], **f}
                 for r in results if r for f in r["confirmed"]]
    # 按严重程度排序：high最高优先级，medium次之，low最低，未知severity排末尾
    confirmed.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))
    # 日志输出最终确认的问题数量
    ctx.log(f"confirmed {len(confirmed)} real finding(s)")
    # 返回最终结果，仅包含经过对抗验证确认的问题
    return {"confirmed": confirmed}


async def run_demo(argv):
    """Run or resume the saved review workflow and print its final state.

    Input: CLI arguments following workflow; resume reads last_run.
    Return: None. Prints findings, status, usage and journal location.
        CLI injects DemoModelClient through the real SubagentRunner for offline runs.
    """
    resume_id = None
    if argv and argv[0] == "resume":
        resume_id = workflow_runtime._read_last_run()
        if not resume_id:
            print("nothing to resume; run `codefox workflow demo` first.")
            return
        print(f"resuming {resume_id}; unchanged agent() calls use the journal cache\n")
    else:
        print("launching workflow `review-changes`\n")

    out = await workflow_runtime.WORKFLOW_HANDLERS["Workflow"](
        name="review-changes",
        args={"budget": None, "changes": DEMO_CHANGES},
        resume_from_run_id=resume_id,
    )

    print("\nresult:")
    for f in out["result"].get("confirmed", []):
        print(f"  [{f['severity']:<6}] {f['dimension']}: {f['title']}")
    task = out["task"]
    usage = task["usage"]
    print(f"\nstatus={task['status']}  agents={usage['agents']}  "
          f"tokens={usage['tokens']}  journal=.runtime/{task['runId']}.journal.jsonl")


def register_workflows() -> None:
    """在应用入口显式登记随包提供的审查示例。

    返回：None。将元数据和 sample_workflow 写入 WORKFLOWS，同名登记会替换原项。
    """
    workflow_runtime.WORKFLOWS[SAMPLE_META["name"]] = (SAMPLE_META, sample_workflow)
