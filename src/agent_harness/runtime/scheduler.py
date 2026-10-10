"""后台任务与 cron 调度器。

后台命令在工作线程中执行工具调用，并通过 task_notification 返回结果。
cron 任务持久化到启动目录的 .scheduled_tasks.json，再通过 cron_queue 送入主循环。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from ..tools.tool_result import ToolResult
from .. import core
from ..tools import file_tools
from . import hooks, tool_execution
from ..workspace import tasks
from ..context import compaction
from . import interaction

# -- 后台任务 --
BACKGROUND_SUMMARY_CHARS = 200

# 慢工具先返回占位 tool_result，实际结果随后作为 task_notification 注入主循环。
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()
_background_threads: list[threading.Thread] = []


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """判断工具请求是否应由后台线程执行。

    输入：tool_name 为工具名；tool_input 为参数字典。
    返回：仅 bash 且 run_in_background 明确为 True 时返回 True。
    """
    return (
        tool_name == "bash"
        and tool_input.get("run_in_background") is True
    )


def start_background_task(block, handlers: dict) -> str:
    """登记后台命令并启动守护线程，立即向主循环返回任务 ID。

    输入：block 为 bash 工具请求；handlers 为分发表，当前实现直接执行共享 shell 函数。
    返回：bg_ 前缀的 ID。启动时固定当前任务目录，结果稍后由收集器交付。
        启动线程失败会撤销登记并抛出异常；权限须由调用方提前检查。
    """
    global _bg_counter
    command = block.input.get("command", block.name)
    parent_context = interaction.current_context()
    context = replace(parent_context.delegate("background", "background"),
                      model_call_id=parent_context.model_call_id, tool_call_id=block.id) if parent_context else None

    def worker():
        """在固定的任务目录执行后台命令，并登记最终结果。

        输入：使用外层捕获的 block、cwd、cwd_error 和 bg_id。
        返回：None。执行命令和 PostToolUse，写入 completed/failed 及文本结果；
            执行或 hook 错误会转换成失败结果，已被移除的登记不再写回。
        """
        def run_command(**arguments):
            if block.name != "bash":
                raise ValueError("only bash can run in the background")
            if cwd_error:
                raise ValueError(cwd_error)
            return file_tools.run_bash(str(arguments["command"]), cwd)
        execution = tool_execution.execute_tool(block, run_command)
        result = execution.result.content
        status = "completed" if execution.result.status == "success" else "failed"
        hook_error = ""
        if execution.post_error is not None:
            hook_error = f"{type(execution.post_error).__name__}: {execution.post_error}"
            result = f"Error: PostToolUse hook failed: {hook_error}\n{result}"
            status = "failed"
        with background_lock:
            task = background_tasks.get(bg_id)
            if task is None:
                return
            task["status"] = status
            task["command_status"] = execution.result.status
            task["hook_error"] = hook_error
            task["result"] = execution.result
            background_results[bg_id] = result

    with tasks.task_lock:
        cwd, cwd_error = file_tools.tool_cwd("agent")
        with background_lock:
            _bg_counter += 1
            bg_id = f"bg_{_bg_counter:04d}"
            background_tasks[bg_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
                "cwd": str(cwd) if cwd else None,
                "output_id": "background_" + uuid.uuid4().hex,
                "context": context,
            }
    def run_worker():
        with interaction.execution_context(context):
            interaction.publish("background_status", task_id=bg_id, status="running")
            worker()
            with background_lock:
                status = background_tasks.get(bg_id, {}).get("status", "finished")
            interaction.publish("background_status", task_id=bg_id, status=status)

    thread = threading.Thread(target=run_worker, daemon=True)
    try:
        thread.start()
    except Exception:
        with background_lock:
            background_tasks.pop(bg_id, None)
            background_results.pop(bg_id, None)
        raise
    if context is not None:
        with background_lock:
            _background_threads[:] = [item for item in _background_threads if item.is_alive()]
            _background_threads.append(thread)
    core.terminal_print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def wait_for_background_tasks():
    with background_lock:
        threads = list(_background_threads)
    for thread in threads:
        thread.join()
    with background_lock:
        _background_threads.clear()


def collect_background_results() -> list[str]:
    """收集已结束后台任务，先保存长输出，再一次性交付整批通知。

    返回：task_notification 文本列表，包含摘要和可选 full_output 路径。
        超过 200 字符的结果保存全文；整批构造成功后删除内存登记。
        文件保存失败会抛出异常，整批仍保持待交付，重试可复用已经保存的路径。
    """
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] in {"completed", "failed"}]
        notifications = []
        for bg_id in ready:
            task = background_tasks[bg_id]
            output = background_results.get(bg_id, "")
            full_output = ""
            if len(output) > BACKGROUND_SUMMARY_CHARS:
                saved_path = task.get("saved_output_path")
                if not saved_path or not Path(saved_path).is_file():
                    path = compaction.save_output(task["output_id"], output, subdirectory="background")
                    saved_path = task["saved_output_path"] = str(path)
                full_output = f"  <full_output>{saved_path}</full_output>\n"
            result = task.get("result")
            if result is not None and not task.get("result_published"):
                with interaction.execution_context(task.get("context")):
                    interaction.publish("tool_result", **result.model_result(task["tool_use_id"]), status=result.status)
                task["result_published"] = True
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{bg_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command_status>{task.get('command_status', '')}</command_status>\n"
                f"  <hook_error>{task.get('hook_error', '')}</hook_error>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{output[:BACKGROUND_SUMMARY_CHARS]}</summary>\n"
                f"{full_output}"
                f"</task_notification>")
        # A failed save leaves the entire batch ready for a later collection.
        for bg_id in ready:
            background_tasks.pop(bg_id)
            background_results.pop(bg_id, None)
        return notifications


def has_pending_background() -> bool:
    """判断是否有已结束但尚未送达的后台任务。

    Returns:
        有待交付的后台任务时返回 ``True``，否则返回 ``False``。
    """
    # 读取后台任务表时加锁，避免与后台线程同时修改状态。
    with background_lock:
        # 只要有一个任务已经完成或失败，就需要延迟目标评估。
        return any(task["status"] in {"completed", "failed"}
                   for task in background_tasks.values())


def has_background_tasks() -> bool:
    """Include running commands and undelivered results at session boundaries."""
    with background_lock:
        return bool(background_tasks)



# -- Cron 调度器 --

# cron 任务独立于对话历史存储；触发后转成定时提示词，注入同一个 agent 主循环。
def _durable_path() -> Path:
    return core.WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.RLock()
_last_fired: dict[str, str] = {}


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value)
                   for part in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """判断五字段 cron 表达式是否匹配指定本地时间。

    输入：cron_expr 为分、时、日、月、星期表达式；dt 为待判断时间。
    返回：是否匹配。星期日记为 0；日和星期同时受限时满足其一即可。
        字段数错误返回 False，字段内容应先通过 validate_cron 校验。
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)
    if not (m and h and month_ok):
        return False
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return dow_ok
    if dow == "*":
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        left, right = field.split("-", 1)
        if not left.isdigit() or not right.isdigit():
            return f"Invalid range: {field}"
        a, b = int(left), int(right)
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < lo or value > hi:
        return f"Value {value} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """校验项目支持的五字段 cron 表达式。

    输入：cron_expr 为分、时、日、月、星期表达式。
    返回：合法时为 None，否则为带字段名称的错误文本；星期范围为 0–6。
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    """将内存中的 durable cron 任务原子地写入持久化文件。

    返回：None。持有 cron 锁，写临时文件后替换正式文件；非持久化任务不写入。
        写入错误继续抛出，临时文件在 finally 中清理。
    """
    with cron_lock:
        durable = [asdict(job) for job in scheduled_jobs.values() if job.durable]
        durable_path = _durable_path()
        temporary = durable_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(durable, indent=2), encoding="utf-8")
            os.replace(temporary, durable_path)
        finally:
            temporary.unlink(missing_ok=True)


def load_durable_jobs():
    """启动时从文件恢复持久化 cron 及尚未确认交付的任务。

    返回：None。有效记录加入 scheduled_jobs；pending_delivery 记录同时加入队列。
        文件缺失时不操作，读取或解析异常忽略；已登记 ID 保留内存状态。
    """
    if not _durable_path().exists():
        return
    try:
        with cron_lock:
            for item in json.loads(_durable_path().read_text(encoding="utf-8")):
                job = CronJob(**item)
                if job.id not in scheduled_jobs and not validate_cron(job.cron):
                    scheduled_jobs[job.id] = job
                    if job.pending_delivery:
                        cron_queue.append(job)
    except Exception:
        pass


def schedule_job(cron: str, prompt: str,
                 recurring: bool = True, durable: bool = True) -> CronJob | str:
    """校验并登记一个周期或一次性定时提示词任务。

    输入：cron 为时间表达式；prompt 为到期注入的文本；recurring 为是否重复；
        durable 为是否跨进程启动保留。
    返回：已登记的 CronJob，或 cron 校验错误文本。
        持久化失败时恢复之前的登记并抛出异常。
    """
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable)
    with cron_lock:
        previous = scheduled_jobs.get(job.id)
        scheduled_jobs[job.id] = job
        if durable:
            try:
                save_durable_jobs()
            except Exception:
                if previous is None:
                    scheduled_jobs.pop(job.id, None)
                else:
                    scheduled_jobs[job.id] = previous
                raise
    return job


def cancel_job(job_id: str) -> str:
    """取消已登记的 cron，并移除队列中尚未消费的同 ID 投递。

    输入：job_id 为 cron 任务 ID。
    返回：取消确认文本；未找到时抛出 ValueError。
        持久化写入失败时恢复登记和队列，再抛出异常。
    """
    with cron_lock:
        previous_queue = list(cron_queue)
        job = scheduled_jobs.pop(job_id, None)
        cron_queue[:] = [queued for queued in cron_queue if queued.id != job_id]
        if job and job.durable:
            try:
                save_durable_jobs()
            except Exception:
                scheduled_jobs[job_id] = job
                cron_queue[:] = previous_queue
                raise
    if not job:
        raise ValueError(f"Job {job_id} not found")
    return f"Cancelled {job_id}"


def _enqueue_due_job(job: CronJob):
    """将到期任务入队，一次性任务先记录待交付状态。

    输入：job 为到期的 CronJob，调用方持有 cron 锁。
    返回：None。一次性 durable 任务先保存 pending_delivery，再加入 cron_queue；
        保存失败时撤销待交付标志并抛出异常，不发布队列项。
    """
    if not job.recurring:
        job.pending_delivery = True
        try:
            if job.durable:
                save_durable_jobs()
        except Exception:
            job.pending_delivery = False
            raise
    cron_queue.append(job)


def cron_scheduler_loop(stop_event):
    """按 core.CRON_POLL_SECONDS 检查到期 cron，在同一分钟内避免重复入队。

    返回：正常情况下持续循环。按本地时间匹配，跳过等待交付的一次性任务；
        单个任务出错时打印原因并继续处理其他任务，不在这里请求模型。
    """
    while not stop_event.wait(core.CRON_POLL_SECONDS):
        now = datetime.now()
        marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if job.pending_delivery:
                        continue
                    if cron_matches(job.cron, now) and _last_fired.get(job.id) != marker:
                        _enqueue_due_job(job)
                        _last_fired[job.id] = marker
                except Exception as e:
                    core.terminal_print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    """取出本批到期任务，交给主循环注入模型历史。

    返回：队列快照并清空原队列；不确认交付、不删除 scheduled_jobs 中的任务。
    """
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def acknowledge_cron_jobs(jobs: list[CronJob]):
    """模型成功响应后确认本批提示词已交付。

    输入：jobs 为已经消费并发送给模型的任务列表。
    返回：None。删除匹配的一次性 pending_delivery 任务，周期任务保留。
        持久化失败时恢复任务登记并重新入队，然后抛出异常。
    """
    durable_changed = False
    with cron_lock:
        previous_jobs = dict(scheduled_jobs)
        for job in jobs:
            current = scheduled_jobs.get(job.id)
            if current and not current.recurring and current.pending_delivery:
                scheduled_jobs.pop(job.id, None)
                durable_changed = durable_changed or current.durable
        if durable_changed:
            try:
                save_durable_jobs()
            except Exception:
                scheduled_jobs.clear()
                scheduled_jobs.update(previous_jobs)
                restore_cron_jobs(jobs)
                raise


def restore_cron_jobs(jobs: list[CronJob]):
    """将未确认交付且仍存在的 cron 任务放回队列。

    输入：jobs 为需要恢复的任务列表。
    返回：None。按 ID 去重，不重新加入已经取消或删除的任务。
    """
    with cron_lock:
        queued_ids = {job.id for job in cron_queue}
        for job in jobs:
            current = scheduled_jobs.get(job.id)
            if current and current.id not in queued_ids:
                cron_queue.append(current)
                queued_ids.add(current.id)


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> ToolResult:
    """处理模型创建 cron 的工具请求。

    输入：cron/prompt 为时间表达式和提示词；recurring/durable 为重复和持久化开关。
    返回：包含任务 ID 的确认或校验错误文本；实际登记由 schedule_job 执行。
    """
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return ToolResult(f"Error: {result}", "failed")
    return ToolResult(f"Scheduled {result.id}: '{cron}' -> {prompt}", "success")


def run_list_crons() -> ToolResult:
    """生成当前定时任务列表。

    返回：任务 ID、表达式、提示词预览及重复/持久化标志的文本；无任务时给出提示。
    """
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return ToolResult("No cron jobs.", "success")
    return ToolResult("\n".join(
        f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
        f"[{'recurring' if job.recurring else 'one-shot'}, "
        f"{'durable' if job.durable else 'session'}]"
        for job in jobs), "success")


def run_cancel_cron(job_id: str) -> ToolResult:
    """处理模型取消 cron 的工具请求。

    输入：job_id 为要取消的定时任务 ID。
    返回：ToolResult，包含取消结果正文和明确状态，存储异常继续抛出。
    """
    try:
        return ToolResult(cancel_job(job_id), "success")
    except ValueError as exc:
        return ToolResult(str(exc), "failed")


_runtime_services_started = False
_runtime_services_lock = threading.Lock()
_runtime_stop = threading.Event()
_runtime_thread = None


def start_runtime_services(*, load_jobs=True):
    """在当前进程内只初始化一次 cron 服务。

    返回：None。恢复持久化任务并启动调度守护线程，之后的调用直接返回。
    """
    global _runtime_services_started, _runtime_thread
    with _runtime_services_lock:
        if _runtime_services_started:
            return
        if load_jobs:
            load_durable_jobs()
        _runtime_stop.clear()
        parent = interaction.current_context()
        # Cron belongs to this runtime instance, not the session active at startup.
        context = (interaction.ExecutionContext(parent.channel, None, 0,
                                                source="cron", scope="runtime")
                   if parent else None)
        def run_cron():
            with interaction.execution_context(context):
                cron_scheduler_loop(_runtime_stop)
        _runtime_thread = threading.Thread(target=run_cron, daemon=True)
        _runtime_thread.start()
        _runtime_services_started = True


def stop_runtime_services():
    global _runtime_services_started, _runtime_thread
    with _runtime_services_lock:
        _runtime_stop.set()
        if _runtime_thread is not None:
            _runtime_thread.join()
        _runtime_thread = None
        _runtime_services_started = False


def clear_runtime_jobs():
    """Discard the old directory's in-memory schedule after stopping services."""
    with cron_lock:
        scheduled_jobs.clear()
        cron_queue.clear()
        _last_fired.clear()
