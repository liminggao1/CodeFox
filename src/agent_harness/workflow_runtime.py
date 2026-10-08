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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import core
from . import subagent
from .subagent_session import selected_tool_names

try:
    import fcntl
except ImportError:  # Windows 没有 fcntl 模块。
    fcntl = None

# -- 运行时限制 --
AGENT_CAP = 1000                       # 每次运行最多调用 agent() 的次数
CONCURRENCY = 8                        # 并发上限（信号量）
WORKFLOW_ID_ALLOCATION_ATTEMPTS = 32    # 新运行 ID 冲突时的重试上限
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
    """在打开新 journal 前，独占创建快照占位文件以预留运行 ID。

    输入：meta 为工作流元数据。
    返回：已预留的运行 ID；达到 WORKFLOW_ID_ALLOCATION_ATTEMPTS 后抛出 WorkflowInputError。
        创建 .runtime 和空快照文件，避免重复运行 ID 覆盖旧 journal。
    """
    store_dir().mkdir(parents=True, exist_ok=True)
    for _ in range(WORKFLOW_ID_ALLOCATION_ATTEMPTS):
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
    """在整个工作流运行期间持有同 ID 的独占锁。

    输入：run_id 为已校验的运行 ID。
    输出：供 with 使用的上下文管理器；当前运行已被占用时立即抛出 WorkflowInputError。
        使用进程内锁和 POSIX 文件锁，退出时释放；无 fcntl 时仅进程内有效。
    """
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
    """校验启动工作流需要的名称、描述和可选阶段。

    输入：meta 为元数据字典，name 为合法名称，description 为字符串。
    返回：校验通过后原样返回 meta；字段无效时抛出 WorkflowInputError。
    """
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
    """检查工作流是否被传入的设置明确拒绝。

    输入：meta 为已校验元数据；settings 为可选设置字典，当前只读取 deny 名单。
    返回：未被拒绝时为 allow；名称在 deny 中时抛出 WorkflowInputError。
    """
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
        """递归检查输出值是否满足项目支持的 schema 子集。

        输入：value 为候选结果；schema 为可选局部定义，默认使用实例定义。
        返回：(是否通过, 错误文本或 None)，检查 enum、类型、required、properties 和 items。
            不实现完整 JSON Schema；当前 integer 与 number 均按非布尔数值判断。
        """
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


class WorkflowStepError(WorkflowInputError):
    """An execution failure carrying consumption that must still be billed."""

    def __init__(self, message, tokens=0):
        super().__init__(message)
        self.tokens = tokens


class SubagentRunner:
    """Adapt a workflow step to the public once child interface."""

    def __init__(self, client=None, model=None):
        self.client = core.client if client is None else client
        self.model = core.MODEL if model is None else model

    def run(self, prompt, schema=None, label=None, *, tools=None):
        """Run one child and return parsed output plus its measured token usage.

        Input: step instructions, optional schema/label and tool whitelist.
        Return: RunnerOutput; child failures raise WorkflowStepError with usage.
        """
        request = f"Step label: {label or 'step'}\n{prompt}"
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        result = subagent.spawn_subagent(
            f"wf-{uuid.uuid4().hex}", request, "once", role="workflow", tools=tools,
            client=self.client, model=self.model, max_tokens=core.WORKFLOW_AGENT_MAX_TOKENS)
        tokens = result["usage"]["tokens"]
        if result["status"] != "completed":
            raise WorkflowStepError(result["text"], tokens)
        text = result["text"]
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # 由 ExecutionState 的 schema 校验触发唯一一次重试。
                value = text
        return RunnerOutput(value, tokens)


RUNNER_FACTORY = SubagentRunner


# -- Journal 日志 --
class WorkflowJournal:
    """只追加写入 <runId>.journal.jsonl；恢复时，已有语义键的 agent() 调用
    直接从缓存重放，不再次执行。"""

    def __init__(self, run_id, resume, store=None):
        """打开运行 journal，并按需恢复缓存和历史用量。

        输入：run_id 为运行 ID；resume 为是否恢复；store 为可选状态目录。
        返回：None。新运行创建空 journal；恢复逐行校验记录并以追加方式打开。
            journal 缺失或记录不合法时抛出 WorkflowInputError，不将坏记录当作缓存。
        """
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

    def key(self, kind, label, prompt, schema, tools=None):
        """生成不依赖并发执行顺序的 journal 语义键。

        Input: call kind, label, prompt, schema and the normalized tool whitelist.
        Return: a stable semantic key for deduplication and resume replay.
        """
        # 语义键不依赖并发顺序，parallel/pipeline 调用在恢复时仍能得到相同的键。
        names = sorted(selected_tool_names(tools))
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}|{json.dumps(names)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        """读取已完成步骤的缓存值。

        输入：key 为语义键。
        返回：已记录结果，未命中返回 MISS；None 也可以是有效缓存值。
        """
        return self.cache.get(key, MISS)

    def record(self, key, value, tokens, cumulative_usage):
        """追加成功步骤结果并刷新 journal 与内存缓存。

        输入：key 为语义键；value 为结果；tokens 为步骤消费；cumulative_usage 为累计用量。
        返回：None。写入一行 JSON 并 flush，再更新缓存和历史 token/agent 计数。
            序列化或写入失败时异常继续抛出。
        """
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
        """在步骤返回后将其 token 消费计入预算。

        输入：n 为新增消费量。
        返回：None。超出上限时抛出 WorkflowInputError，并保持原计数。
            校验发生在模型请求完成后，不撤销已经发生的真实 API 消费。
        """
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
        """设置后续步骤默认所属阶段。

        输入：title 为阶段标题。
        返回：None。更新当前阶段，同一标题只登记一次 workflow_phase 进度事件。
        """
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """记录工作流脚本的进度说明。

        输入：message 为日志正文。
        返回：None。追加并打印 workflow_log 进度事件。
        """
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None, *, tools=None):
        """执行或重放一个工作流 agent 步骤。

        Input: step instructions, schema, stable label, phase and optional tools.
        Return: cached or newly executed output. Defaults to the five base tools;
            tools=[] disables them. Invalid output gets one tool-free format repair.
        """
        try:
            tools = list(selected_tool_names(tools))
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from exc
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        key = self.journal.key("agent", label, prompt, schema, tools)
        async with self.journal.key_locks.setdefault(key, asyncio.Lock()):
            return await self._run_agent(prompt, schema, label, phase, key, tools)

    async def _run_agent(self, prompt, schema, label, phase, key, tools):
        """在语义键锁内检查缓存、执行步骤并提交结果和消费量。

        输入：prompt/schema/label/phase 为步骤配置；key 为已加锁的 journal 语义键。
        返回：缓存或有效执行结果。真实调用在线程中运行；成功后累计预算及用量并写日志。
            缓存不合格、无剩余额度、重试仍无效或累计超预算时抛出 WorkflowInputError。
        """
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

        tokens = 0
        async with self._limits.semaphore:
            if self.budget.remaining() <= 0:
                raise WorkflowInputError("token budget exceeded")
            try:
                run = await asyncio.to_thread(self.runner.run, prompt, schema, label, tools=tools)
                result = run.value
                tokens += run.tokens
                if schema is not None:
                    ok, err = SimpleJsonSchema(schema).validate(result)
                    if not ok:
                        if self.budget.remaining() <= tokens:
                            raise WorkflowInputError("token budget exceeded before format retry")
                        repair = ("The step has already executed. Correct only its final output format.\n"
                                  f"Validation error: {err}\nPrevious output:\n"
                                  f"{json.dumps(result, ensure_ascii=False)}")
                        retry = await asyncio.to_thread(
                            self.runner.run, repair, schema, label, tools=[])
                        result = retry.value
                        tokens += retry.tokens
                        ok, err = SimpleJsonSchema(schema).validate(result)
                        if not ok:
                            raise WorkflowInputError(f"agent({{schema}}) invalid output: {err}")
            except WorkflowStepError as exc:
                tokens += exc.tokens
                raise
            finally:
                self._record_usage(tokens)

        self.journal.record(key, result, tokens, self.task.cumulative_usage)
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    def _record_usage(self, tokens):
        """Bill completed and failed executions before enforcing the budget."""
        self.task.usage["agents"] += 1
        self.task.usage["tokens"] += tokens
        self.task.cumulative_usage["agents"] += 1
        self.task.cumulative_usage["tokens"] += tokens
        self.budget.add(tokens)

    async def parallel(self, thunks):
        """并发启动一组异步任务，并等待全部结果。

        输入：thunks 为无参函数列表，每个调用返回可等待对象。
        返回：按输入顺序排列的结果列表；任一异常会传播给工作流。
            使用 asyncio.gather，不在这里主动取消其他已启动任务。
        """
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """让各输入项并发进入流水线，每项内部依次执行各阶段。

        输入：items 为原始条目；stages 为异步阶段函数，接收 (上阶段结果, 原始项, 索引)。
        返回：按条目原顺序排列的最终结果；阶段异常向外传播。
            阶段之间无全局等待点，一个条目进入下一阶段时其他条目可仍在前一阶段。
        """
        async def run_item(item, idx):
            value = item
            for stage in stages:
                value = await stage(value, item, idx)
            return value
        return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])

    async def workflow(self, name, args=None):
        """以内联方式执行注册的子工作流。

        输入：name 为已注册名称；args 为可选子流程参数。
        返回：子脚本的返回值，共享父运行的任务、journal、runner、预算和限制。
            只允许一层嵌套，名称不存在或继续嵌套时抛出 WorkflowInputError。
        """
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
        """校验工作流输入、预留或恢复运行 ID，并持锁执行完整运行。

        输入：meta 为元数据；script_fn 为可信异步脚本；args 为参数字典；
            resume_from_run_id 为可选恢复 ID。
        返回：含 launched、result 和 LocalWorkflowTask 的字典。
            输入不合法或同一运行已被占用时抛出 WorkflowInputError，不并发写同一 journal。
        """
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
        """在运行锁内创建上下文、执行脚本并保存最终状态。

        输入：meta/script_fn/args 为运行配置；run_id 为已锁定 ID；resuming 为恢复标志。
        返回：launched、result 和任务对象。恢复时校验名称与原参数并加载历史预算。
            脚本异常记录为 failed 结果；关闭 journal 后保存输出、快照和 last_run。
            初始化或文件保存错误仍可向外抛出。
        """
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
            saved_usage = snapshot.get("task", {}).get("cumulativeUsage", {})
            task.cumulative_usage = {
                "agents": max(journal.historical_agents, saved_usage.get("agents", 0)),
                "tokens": max(journal.historical_tokens, saved_usage.get("tokens", 0)),
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
    """读取恢复运行所需的快照字典。

    输入：run_id 为已校验运行 ID。
    返回：快照字典；文件缺失、JSON 不合法或不是对象时抛出 WorkflowInputError。
    """
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
    """将工作流任务转换为工具返回和快照保存使用的字典。

    输入：task 为 LocalWorkflowTask。
    返回：包含标识、名称、状态、本次/累计用量和进度的新字典，复制可变容器。
    """
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
    """按注册名称运行工作流，并把任务对象转换为工具返回字段。

    输入：name 为 WORKFLOWS 中的名称；args 为可选参数字典；
        resume_from_run_id 为可选恢复运行 ID。
    返回：含 launched、result 和序列化 task 的字典；不接受模型提供的任意脚本代码。
        名称或参数无效时抛出 WorkflowInputError。
    """
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
    """将同步工具分发连接到异步工作流入口，并等待运行结果。

    输入：tool_input 为 name、args 和可选 resume_from_run_id 关键字参数。
    返回：工作流结果的 JSON 字符串；WorkflowInputError 转成错误文本，其他异常继续抛出。
    """
    try:
        return json.dumps(asyncio.run(run_workflow(**tool_input)), default=str)
    except WorkflowInputError as exc:
        return f"Error: {exc}"
