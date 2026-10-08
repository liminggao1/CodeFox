#!/usr/bin/env python3
"""
agent.workflow：工作流运行时。

已保存的编排通过一次 Workflow 工具调用执行。host 将该工具加入工具池；
状态保存在启动目录的 .runtime/ 下。

    +-------------+       +--------------------------------+
    | Agent 主循环 | ----> | Workflow(name, args, run_id)  |
    +-------------+       +---------------+----------------+
                                          |
                           +--------------+--------------+
                           | agent | parallel | pipeline  |
                           +--------------+--------------+
                                          |
                                   journal + 结果
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import core

try:
    import fcntl
except ImportError:  # Windows 没有 fcntl 模块。
    fcntl = None

WORKFLOW_AGENT_ENVIRONMENT = (
    "Host OS: Windows. This workflow agent only responds to its supplied step; "
    "it has no direct shell or file tools."
    if os.name == "nt"
    else "Host OS: Unix-like. This workflow agent only responds to its supplied "
         "step; it has no direct shell or file tools."
)

# -- 运行时限制 --
AGENT_CAP = 1000                       # 每次运行最多调用 agent() 的次数
CONCURRENCY = 8                        # 并发上限（信号量）
MISS = object()                        # journal 缓存未命中标记
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$")


def store_dir() -> Path:
    return core.WORKDIR / ".runtime"


def _stable_hash(s: str) -> int:
    """返回跨进程稳定的哈希值；Python 的 hash() 每个进程都不同，
    会导致 `run` 与 `resume` 使用不同的恢复键。"""
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def create_run_id(meta) -> str:
    return f"wf_{meta['name']}_{secrets.token_hex(8)}"


def reserve_run_id(meta) -> str:
    """在截断任何 journal 之前先预留新的运行标识。"""
    store_dir().mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        run_id = validate_run_id(create_run_id(meta))
        snapshot_path = store_dir() / f"{run_id}.json"
        try:
            fd = os.open(snapshot_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_id
    raise WorkflowInputError("could not allocate a unique workflow runId")


def create_task_id(run_id) -> str:
    return f"local_workflow_{run_id}"


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("invalid workflow runId")
    return run_id


# -- 错误类型 --
class WorkflowInputError(Exception):
    """工作流、元数据或 schema 输入无效。"""


_run_locks_guard = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}
_fcntl_warned = False

def _warn_missing_fcntl():
    """跨进程锁降级为进程内锁时只提示一次。"""
    global _fcntl_warned
    if not _fcntl_warned:
        _fcntl_warned = True
        print("[workflow] fcntl unavailable; run lock is in-process only")



@contextmanager
def workflow_run_lock(run_id: str):
    """在整个生命周期中为同一次运行跨线程、跨 host 进程加锁。"""
    with _run_locks_guard:
        local_lock = _run_locks.setdefault(run_id, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise WorkflowInputError(f"workflow run {run_id} is already active")

    handle = None
    try:
        store_dir().mkdir(parents=True, exist_ok=True)
        handle = (store_dir() / f"{run_id}.lock").open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowInputError(
                    f"workflow run {run_id} is already active"
                ) from exc
        else:
            _warn_missing_fcntl()
        yield
    finally:
        if handle is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
            else:
                handle.close()
        local_lock.release()
        with _run_locks_guard:
            if not local_lock.locked() and _run_locks.get(run_id) is local_lock:
                _run_locks.pop(run_id, None)


# -- 元数据校验 --
def validate_meta(meta):
    """启动前校验名称、描述和可选阶段。"""
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta must be an object literal")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta requires `name` and `description`")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError(
            "meta.name must be a 1-64 character slug using letters, numbers, '.', '_', or '-'"
        )
    if not isinstance(meta["description"], str):
        raise WorkflowInputError("meta.description must be a string")
    if "phases" in meta:
        if not isinstance(meta["phases"], list) or not all(
            isinstance(phase, str) and phase for phase in meta["phases"]
        ):
            raise WorkflowInputError("meta.phases must be a list of non-empty strings")
    return meta


def check_permission(meta, settings=None):
    """启动工作流前执行允许/拒绝权限检查。"""
    settings = settings or {}
    if meta["name"] in settings.get("deny", []):
        raise WorkflowInputError(f"workflow '{meta['name']}' denied by settings")
    return "allow"


# -- 最小化 JSON Schema 校验 --
class SimpleJsonSchema:
    """校验 agent({schema}) 的基本类型和必填字段：
    object/array/string/boolean/number 与 required。"""

    def __init__(self, schema):
        self.schema = schema

    def validate(self, value, schema=None):
        schema = self.schema if schema is None else schema
        if "enum" in schema and value not in schema["enum"]:
            return False, f"expected one of {schema['enum']}"
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False, "expected object"
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"missing required key '{key}'"
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, err = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {err}"
            return True, None
        if t == "array":
            if not isinstance(value, list):
                return False, "expected array"
            items = schema.get("items")
            if items:
                for i, el in enumerate(value):
                    ok, err = self.validate(el, items)
                    if not ok:
                        return False, f"[{i}]: {err}"
            return True, None
        if t == "string":
            return (isinstance(value, str), None if isinstance(value, str) else "expected string")
        if t == "boolean":
            return (isinstance(value, bool), None if isinstance(value, bool) else "expected boolean")
        if t in ("number", "integer"):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return (ok, None if ok else "expected number")
        return True, None




# -- Agent 执行器 --


@dataclass(frozen=True)
class RunnerOutput:
    value: object
    tokens: int




def _response_text(response) -> str:
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ).strip()


def _parse_runner_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        raise WorkflowInputError("workflow agent returned invalid JSON")


class AnthropicAgentRunner:
    """工作流 agent 复用 host 的 API 客户端。"""

    def __init__(self, client, model):
        self.client = client
        self.model = model

    def run(self, prompt, schema=None, label=None):
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        response = self.client.messages.create(
            model=self.model,
            system=(
                "You are a focused workflow agent. Complete only the supplied step. "
                + f"{WORKFLOW_AGENT_ENVIRONMENT} "
                + "Do not claim access to files or results not included in the prompt."
            ),
            messages=[{"role": "user", "content": request}],
            max_tokens=2000,
        )
        text = _response_text(response)
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # 由 ExecutionState 的 schema 校验触发唯一一次重试。
                value = text
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0
        )
        return RunnerOutput(value, tokens)


RUNNER_FACTORY = lambda: AnthropicAgentRunner(core.client, core.MODEL)


# -- Journal 日志 --
class WorkflowJournal:
    """只追加写入 <runId>.journal.jsonl；恢复时，已有语义键的 agent() 调用
    直接从缓存重放，不再次执行。"""

    def __init__(self, run_id, resume, store=None):
        store = store_dir() if store is None else store
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self.resume = resume
        self.cache = {}
        self.key_locks = {}
        self.historical_tokens = 0
        self.historical_agents = 0
        if resume:
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    rec = json.loads(line)
                    if (
                        not isinstance(rec, dict)
                        or not isinstance(rec.get("key"), str)
                        or "value" not in rec
                    ):
                        raise ValueError("expected key/value record")
                except (json.JSONDecodeError, ValueError) as exc:
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    ) from exc
                tokens = rec.get("tokens")
                if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    )
                self.cache[rec["key"]] = rec["value"]
                self.historical_tokens += tokens
                self.historical_agents += 1
                cumulative_tokens = rec.get("cumulative_tokens")
                cumulative_agents = rec.get("cumulative_agents")
                if cumulative_tokens is not None:
                    if (not isinstance(cumulative_tokens, int)
                            or isinstance(cumulative_tokens, bool)
                            or cumulative_tokens < 0):
                        raise WorkflowInputError(
                            f"invalid resume journal record at line {line_number}"
                        )
                    self.historical_tokens = max(
                        self.historical_tokens, cumulative_tokens
                    )
                if cumulative_agents is not None:
                    if (not isinstance(cumulative_agents, int)
                            or isinstance(cumulative_agents, bool)
                            or cumulative_agents < 0):
                        raise WorkflowInputError(
                            f"invalid resume journal record at line {line_number}"
                        )
                    self.historical_agents = max(
                        self.historical_agents, cumulative_agents
                    )
            self._f = self.path.open("a", encoding="utf-8")
        else:
            self._f = self.path.open("w", encoding="utf-8")             # 新运行从空文件开始

    def key(self, kind, label, prompt, schema):
        # 语义键不依赖并发顺序，parallel/pipeline 调用在恢复时仍能得到相同的键。
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        return self.cache.get(key, MISS)

    def record(self, key, value, tokens, cumulative_usage):
        self._f.write(json.dumps({
            "key": key, "value": value, "tokens": tokens,
            "cumulative_tokens": cumulative_usage["tokens"],
            "cumulative_agents": cumulative_usage["agents"],
        }) + "\n")
        self._f.flush()
        self.cache[key] = value
        self.historical_tokens = cumulative_usage["tokens"]
        self.historical_agents = cumulative_usage["agents"]

    def close(self):
        self._f.close()


# -- Token 预算 --
class Budget:
    """提供 budget.total、spent() 和 remaining()；用量达到上限后，
    agent() 调用抛错，不会静默超支。"""

    def __init__(self, total=None, spent=0):
        self.total = total
        self._spent = spent

    def add(self, n):
        if self.total is not None and self._spent + n > self.total:
            raise WorkflowInputError(
                f"token budget exceeded ({self._spent + n} > {self.total})"
            )
        self._spent += n

    def spent(self):
        return self._spent

    def remaining(self):
        return float("inf") if self.total is None else max(0, self.total - self._spent)


# -- 工作流任务生命周期 --
class LocalWorkflowTask:
    """保存工作流状态、用量和进度事件。"""

    def __init__(self, task_id, run_id, meta):
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.status = "running"
        self.usage = {"agents": 0, "tokens": 0}
        self.cumulative_usage = {"agents": 0, "tokens": 0}
        self.progress = []

    def event(self, name, **data):
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  event      {name:<18} {line}")

    def progress_event(self, ptype, **data):
        self.progress.append({"type": ptype, **data})
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  progress   {ptype:<16} {line}")


# -- 工作流原语 --
class ExecutionLimits:
    """本次运行共享的限制，也适用于嵌套工作流。"""

    def __init__(self):
        self.agents = 0
        self.semaphore = asyncio.Semaphore(CONCURRENCY)

    def claim_agent(self):
        self.agents += 1
        if self.agents > AGENT_CAP:
            raise WorkflowInputError(f"agent() cap reached ({AGENT_CAP})")


class ExecutionState:
    """注入工作流脚本的编排原语容器。"""

    def __init__(self, task, journal, runner, budget, args, depth=0, limits=None):
        self.task = task
        self.journal = journal
        self.runner = runner
        self.budget = budget
        self.args = args
        self._depth = depth
        self._phase = None
        self._phases_seen = set()
        self._limits = limits or ExecutionLimits()

    def phase(self, title):
        """开启阶段，后续 agent() 归入该阶段；重复声明同一阶段时不再次播报。"""
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """发出一条 workflow_log 进度记录。"""
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        """启动一个子 agent；提供 schema 时要求结构化输出并校验，最多重试一次。
        恢复时若命中缓存键，直接返回缓存结果。"""
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        key = self.journal.key("agent", label, prompt, schema)
        async with self.journal.key_locks.setdefault(key, asyncio.Lock()):
            return await self._run_agent(prompt, schema, label, phase, key)

    async def _run_agent(self, prompt, schema, label, phase, key):
        cached = self.journal.cached(key)
        if cached is not MISS:
            if schema is not None:
                ok, err = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(
                        f"cached agent output failed schema validation: {err}"
                    )
            self.task.progress_event("workflow_agent", label=label,
                                     phase=phase or self._phase, status="cached")
            return cached

        if self.budget.remaining() <= 0:
            raise WorkflowInputError("token budget exceeded")

        async with self._limits.semaphore:
            run = await asyncio.to_thread(
                self.runner.run, prompt, schema, label
            )
            result = run.value
            tokens = run.tokens

        if schema is not None:
            ok, err = SimpleJsonSchema(schema).validate(result)
            if not ok:
                retry = await asyncio.to_thread(
                    self.runner.run,
                    prompt + "\n\nReturn valid JSON.",
                    schema,
                    label,
                )
                result = retry.value
                tokens += retry.tokens
                ok, err = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(f"agent({{schema}}) invalid output: {err}")

        self.budget.add(tokens)
        self.task.usage["agents"] += 1
        self.task.usage["tokens"] += tokens
        self.task.cumulative_usage["agents"] += 1
        self.task.cumulative_usage["tokens"] += tokens
        self.journal.record(key, result, tokens, self.task.cumulative_usage)
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    async def parallel(self, thunks):
        """同步点：并发执行所有任务，任一任务失败则整体失败。"""
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """逐项执行分阶段流程，阶段间没有同步点：A 可进入第三阶段，B 仍在第一阶段。
        每个阶段接收（上阶段结果，原始项，索引）；任一阶段抛错则工作流失败。"""
        async def run_item(item, idx):
            value = item
            for stage in stages:
                value = await stage(value, item, idx)
            return value
        return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])

    async def workflow(self, name, args=None):
        """以内联子流程运行已保存的工作流（只允许一层），
        共享本次运行的 journal、预算和 agent 计数器。"""
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in WORKFLOWS:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        meta, fn = WORKFLOWS[name]
        child = ExecutionState(self.task, self.journal, self.runner, self.budget,
                               args or {}, depth=self._depth + 1,
                               limits=self._limits)
        return await fn(child, args or {})


# -- Workflow 工具 --
class WorkflowTool:
    """Workflow 工具：.call() 校验 meta 和权限，创建 runId/taskId，
    注册 LocalWorkflowTask，在执行脚本时发出生命周期事件；返回结果和任务状态，
    并支持 resume。"""

    async def call(self, meta, script_fn, args=None, resume_from_run_id=None):
        validate_meta(meta)
        check_permission(meta)
        resuming = resume_from_run_id is not None
        if resuming:
            run_id = validate_run_id(resume_from_run_id)
        else:
            run_id = reserve_run_id(meta)
        with workflow_run_lock(run_id):
            return await self._call_locked(
                meta, script_fn, args, run_id, resuming
            )

    async def _call_locked(self, meta, script_fn, args, run_id, resuming):
        if resuming:
            snapshot = _read_snapshot(run_id)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("resume runId does not match workflow meta")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args
            elif args != saved_args:
                raise WorkflowInputError("resume args do not match the original run")
            journal = WorkflowJournal(run_id, resume=True)
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False)
        task_id = create_task_id(run_id)

        task = LocalWorkflowTask(task_id, run_id, meta)
        if resuming:
            task.cumulative_usage = {
                "agents": journal.historical_agents,
                "tokens": journal.historical_tokens,
            }
        # 工作流执行前先记录启动事件。
        launched = {"status": "async_launched", "taskId": task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task_id)
        task.event("task_started", workflow=meta["name"],
                   phases=",".join(meta.get("phases", [])) or "-",
                   resume=resuming)
        _write_json(store_dir() / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })

        try:
            ctx = ExecutionState(
                task, journal, RUNNER_FACTORY(),
                Budget(args.get("budget"), task.cumulative_usage["tokens"]), args
            )
            result = await script_fn(ctx, args)
            task.status = "completed"
        except Exception as e:                          # 失败或停止也要结束循环
            task.status = "failed"
            result = {"error": str(e)}
        finally:
            journal.close()

        _write_json(store_dir() / f"{run_id}.output.json", result)
        _write_json(store_dir() / f"{run_id}.json", {
            "runId": run_id,
            "workflowName": meta["name"],
            "args": args,
            "task": serialize_task(task),
        })
        _save_last_run(run_id)
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f".runtime/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": task}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _read_snapshot(run_id):
    path = store_dir() / f"{run_id}.json"
    if not path.exists():
        raise WorkflowInputError(f"resume snapshot not found for {run_id}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}") from exc
    if not isinstance(snapshot, dict):
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}")
    return snapshot


def _save_last_run(run_id):
    (store_dir() / "last_run.txt").write_text(run_id, encoding="utf-8")


def _read_last_run():
    p = store_dir() / "last_run.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


# -- Saved workflow registry and tool interface --






# 已保存的工作流注册表
WORKFLOWS = {}

WORKFLOW_TOOL = {
    "name": "Workflow",
    "description": "Run a saved workflow by name. Pass input in args.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "args": {"type": "object"},
            "resume_from_run_id": {"type": "string"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def serialize_task(task):
    return {
        "taskId": task.task_id,
        "taskType": "local_workflow",
        "runId": task.run_id,
        "workflowName": task.meta["name"],
        "status": task.status,
        "usage": dict(task.usage),
        "cumulativeUsage": dict(task.cumulative_usage),
        "progress": list(task.progress),
    }


async def run_workflow(name, args=None, resume_from_run_id=None):
    """面向模型的适配器：从 host 注册表定位可信代码。"""
    if not isinstance(name, str):
        raise WorkflowInputError("workflow name must be a string")
    if name not in WORKFLOWS:
        raise WorkflowInputError(f"unknown workflow '{name}'")
    if args is not None and not isinstance(args, dict):
        raise WorkflowInputError("workflow args must be an object")
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool().call(
        meta,
        script_fn,
        args=args,
        resume_from_run_id=resume_from_run_id,
    )
    return {
        "launched": out["launched"],
        "result": out["result"],
        "task": serialize_task(out["task"]),
    }


WORKFLOW_HANDLERS = {"Workflow": run_workflow}


def run_workflow_sync(**tool_input):
    """连接同步 host 分发器与异步工作流运行时。"""
    try:
        return json.dumps(asyncio.run(run_workflow(**tool_input)), default=str)
    except WorkflowInputError as exc:
        return f"Error: {exc}"


