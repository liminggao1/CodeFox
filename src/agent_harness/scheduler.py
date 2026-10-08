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
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import core
from . import file_tools
from . import hooks
from . import tasks
from . import compaction

# -- 后台任务 --

# 慢工具先返回占位 tool_result，实际结果随后作为 task_notification 注入主循环。
_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    return (
        tool_name == "bash"
        and tool_input.get("run_in_background") is True
    )


def start_background_task(block, handlers: dict) -> str:
    global _bg_counter
    command = block.input.get("command", block.name)

    def worker():
        try:
            if block.name != "bash":
                raise ValueError("only bash can run in the background")
            if cwd_error:
                raise ValueError(cwd_error.removeprefix("Error: "))
            output, exit_code = file_tools._run_bash_process(
                str(block.input["command"]), cwd)
            result = file_tools._format_bash_result(output, exit_code)
            status = "completed" if exit_code == 0 else "failed"
        except Exception as exc:
            result = f"Error: {type(exc).__name__}: {exc}"
            status = "failed"
        try:
            hooks.trigger_hooks("PostToolUse", block, result)
        except Exception as exc:
            result = (f"Error: PostToolUse hook failed: "
                      f"{type(exc).__name__}: {exc}\n{result}")
            status = "failed"
        with background_lock:
            task = background_tasks.get(bg_id)
            if task is None:
                return
            task["status"] = status
            background_results[bg_id] = str(result)

    with tasks.task_lock:
        cwd, cwd_error = file_tools._agent_cwd()
        with background_lock:
            _bg_counter += 1
            bg_id = f"bg_{_bg_counter:04d}"
            background_tasks[bg_id] = {
                "tool_use_id": block.id,
                "command": command,
                "status": "running",
                "cwd": str(cwd) if cwd else None,
                "output_id": "background_" + uuid.uuid4().hex,
            }
    thread = threading.Thread(target=worker, daemon=True)
    try:
        thread.start()
    except Exception:
        with background_lock:
            background_tasks.pop(bg_id, None)
            background_results.pop(bg_id, None)
        raise
    print(f"  \033[33m[background] {bg_id}: {str(command)[:60]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """Save full long outputs and publish the ready batch atomically."""
    with background_lock:
        ready = [bg_id for bg_id, task in background_tasks.items()
                 if task["status"] in {"completed", "failed"}]
        notifications = []
        for bg_id in ready:
            task = background_tasks[bg_id]
            output = background_results.get(bg_id, "")
            full_output = ""
            if len(output) > 200:
                saved_path = task.get("saved_output_path")
                if not saved_path or not Path(saved_path).is_file():
                    path = compaction.save_output(task["output_id"], output, subdirectory="background")
                    saved_path = task["saved_output_path"] = str(path)
                full_output = f"  <full_output>{saved_path}</full_output>\n"
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{bg_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{output[:200]}</summary>\n"
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
    if not _durable_path().exists():
        return
    try:
        for item in json.loads(_durable_path().read_text(encoding="utf-8")):
            job = CronJob(**item)
            if not validate_cron(job.cron):
                scheduled_jobs[job.id] = job
                if job.pending_delivery:
                    cron_queue.append(job)
    except Exception:
        pass


def schedule_job(cron: str, prompt: str,
                 recurring: bool = True, durable: bool = True) -> CronJob | str:
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
        return f"Job {job_id} not found"
    return f"Cancelled {job_id}"


def _enqueue_due_job(job: CronJob):
    """先持久化一次性投递，再通过队列公开。"""
    if not job.recurring:
        job.pending_delivery = True
        try:
            if job.durable:
                save_durable_jobs()
        except Exception:
            job.pending_delivery = False
            raise
    cron_queue.append(job)


def cron_scheduler_loop():
    while True:
        time.sleep(1)
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
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def acknowledge_cron_jobs(jobs: list[CronJob]):
    """模型调用接收提示词后，删除对应的一次性任务。"""
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
    """模型调用失败后，将未确认的投递重新入队。"""
    with cron_lock:
        queued_ids = {job.id for job in cron_queue}
        for job in jobs:
            current = scheduled_jobs.get(job.id)
            if current and current.id not in queued_ids:
                cron_queue.append(current)
                queued_ids.add(current.id)


def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' -> {prompt}"


def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs."
    return "\n".join(
        f"  {job.id}: '{job.cron}' -> {job.prompt[:40]} "
        f"[{'recurring' if job.recurring else 'one-shot'}, "
        f"{'durable' if job.durable else 'session'}]"
        for job in jobs)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


_runtime_services_started = False
_runtime_services_lock = threading.Lock()


def start_runtime_services():
    """CLI host 启动时只初始化一次持久化调度。"""
    global _runtime_services_started
    with _runtime_services_lock:
        if _runtime_services_started:
            return
        load_durable_jobs()
        threading.Thread(target=cron_scheduler_loop, daemon=True).start()
        _runtime_services_started = True
