"""基础文件与 shell 工具。

所有工具按 cwd 解析路径，因此绑定任务的 worktree 会改变读写位置。
文件读写的路径授权由调用前的权限检查负责；glob 仍只搜索工作目录。
run_agent_* 则用当前任务分配的 cwd 包装同一组工具。
"""

from __future__ import annotations

import atexit
import glob
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .tool_result import ToolResult
from .. import core
from ..workspace import tasks
from ..runtime import interaction
from ..runtime.lead_context import current_lead_context

# -- 基础工具 --


def resolve_path(path: str, cwd: Path | None = None) -> Path:
    """按任务目录解析文件路径；权限检查已在工具分发前完成。

    输入：path 为相对或绝对路径；cwd 为解析基准目录，默认使用启动目录。
    返回：解析符号链接后的绝对 Path，不创建文件，也不重复询问或拒绝已授权的路径。
    """
    base = (cwd or core.WORKDIR).resolve()
    return (base / path).resolve()


_shell_processes: set[subprocess.Popen] = set()
_shell_process_lock = threading.RLock()


def _stop_process_group(process: subprocess.Popen):
    """跨平台终止 shell 进程及其子进程。

    POSIX 使用进程组信号（先 SIGTERM，后 SIGKILL）。Windows 没有
    ``os.killpg`` 和 ``signal.SIGKILL``，因此改用
    ``Popen.terminate()`` / ``Popen.kill()``。
    """
    if os.name == "nt":
        for stop in (process.terminate, process.kill):
            if process.poll() is not None:
                return
            try:
                stop()
            except OSError:
                return
            try:
                process.wait(timeout=core.PROCESS_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                continue
        return
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(core.PROCESS_TERMINATION_GRACE_SECONDS)


def _stop_all_shell_processes():
    with _shell_process_lock:
        processes = list(_shell_processes)
    for process in processes:
        _stop_process_group(process)


def _handle_termination_signal(signum, _frame):
    _stop_all_shell_processes()
    raise SystemExit(128 + signum)


atexit.register(_stop_all_shell_processes)
signal.signal(signal.SIGTERM, _handle_termination_signal)


def _run_bash_process(command: str, cwd: Path | None = None) -> tuple[str, int | None]:
    process = None
    try:
        # start_new_session（setsid）仅在 POSIX 上可用，Windows 不使用。
        popen_kwargs = {} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(
            command, shell=True, cwd=cwd or core.WORKDIR,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace",
            **popen_kwargs,
        )
        with _shell_process_lock:
            _shell_processes.add(process)
        stdout, stderr = process.communicate(timeout=core.BASH_TIMEOUT_SECONDS)
        out = (stdout + stderr).strip()
        return (out[:core.BASH_OUTPUT_CHAR_LIMIT] if out else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({core.BASH_TIMEOUT_SECONDS:g}s)", None
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}", None
    finally:
        if process is not None:
            _stop_process_group(process)
            try:
                process.wait(timeout=core.PROCESS_REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            with _shell_process_lock:
                _shell_processes.discard(process)


def _format_bash_result(output: str, exit_code: int | None) -> str:
    if exit_code == 0:
        return output
    if exit_code is None:
        return output
    return f"Error: command exited with status {exit_code}\n{output}"


def run_bash(command: str, cwd: Path | None = None,
             run_in_background: bool = False) -> ToolResult:
    """同步执行 shell 命令并返回格式化结果。

    输入：command 为命令；cwd 为执行目录；run_in_background 由外层分发器处理，
        直接调用本函数时不改变同步执行行为。
    返回：ToolResult，正文包含命令输出，状态按退出码给出；权限由调用方先检查。
    """
    # run_in_background 由分发器处理；直接执行时忽略它。
    output, exit_code = _run_bash_process(command, cwd)
    return ToolResult(_format_bash_result(output, exit_code), "success" if exit_code == 0 else "failed")


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path | None = None) -> ToolResult:
    """读取已授权的文件，或按绝对路径读取运行时归档。

    输入：path 为文件路径；limit 为最多读取行数；offset 为跳过行数；cwd 为任务目录。
    返回：ToolResult，正文包含文件文本或错误，状态明确成功或失败；未展示完时附加剩余行数提示。
        绝对路径直接解析，其他路径按 cwd 解析，默认使用启动目录。
        运行时归档路径是绝对路径，不受当前任务目录变化影响。
    """
    try:
        file_path = resolve_path(path, cwd)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return ToolResult("\n".join(lines), "success")
    except Exception as e:
        return ToolResult(f"Error: {e}", "failed")


def run_write(path: str, content: str, cwd: Path | None = None) -> ToolResult:
    """在已授权的路径创建或覆盖 UTF-8 文本文件。

    输入：path 为路径；content 为正文；cwd 为解析基准目录，默认使用启动目录。
    返回：ToolResult，包含写入确认或错误及明确状态；成功时按需创建父目录并替换文件内容。
    """
    try:
        fp = resolve_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return ToolResult(f"Wrote {len(content)} bytes to {path}", "success")
    except Exception as e:
        return ToolResult(f"Error: {e}", "failed")


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path | None = None) -> ToolResult:
    """替换文件中第一次出现的指定文本。

    输入：path 为路径；old_text 为原文；new_text 为替换文本；cwd 为解析基准目录。
    返回：ToolResult，包含编辑确认或错误及明确状态；原文未找到时不写文件。
    """
    try:
        fp = resolve_path(path, cwd)
        text = fp.read_text(encoding="utf-8")
        if old_text not in text:
            return ToolResult(f"Error: text not found in {path}", "failed")
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return ToolResult(f"Edited {path}", "success")
    except Exception as e:
        return ToolResult(f"Error: {e}", "failed")


def run_glob(pattern: str, cwd: Path | None = None) -> ToolResult:
    """搜索指定目录内匹配 glob 模式的路径。

    输入：pattern 为模式，支持 ** 递归；cwd 为搜索根目录，默认使用启动目录。
    返回：ToolResult，正文为排序去重的路径文本，状态明确成功或失败。
        按 core.GLOB_RESULT_LIMIT 限制数量；无匹配时返回提示。
        解析后越界的匹配项不会返回，不修改文件。
    """
    import glob as g
    try:
        base = (cwd or core.WORKDIR).resolve()
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=base, recursive=True)
            if (base / match).resolve().is_relative_to(base)
        })
        shown = matches[:core.GLOB_RESULT_LIMIT]
        if len(matches) > core.GLOB_RESULT_LIMIT:
            shown.append("... (more matches omitted; narrow the pattern)")
        return ToolResult("\n".join(shown) if shown else "(no matches)", "success")
    except Exception as e:
        return ToolResult(f"Error: {e}", "failed")


def tool_cwd(owner: str) -> tuple[Path | None, str | None]:
    """Resolve the actor's current assignment on every invocation."""
    try:
        return tasks.assignment_cwd(owner), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Error: Invalid task assignment: {exc}"


def bind_file_tools(owner: str, *, allow_background: bool = False) -> dict:
    """Bind five handlers to an owner, without selecting tools or authorizing.

    Input: task owner; allow_background preserves the Lead's bash signature.
    Return: handlers that resolve current cwd at execution, not construction.
    """
    def invoke(handler, *args, **kwargs):
        cwd, error = tool_cwd(owner)
        return ToolResult(error, "failed") if error else handler(*args, cwd=cwd, **kwargs)

    if allow_background:
        def bash(command: str, run_in_background: bool = False) -> ToolResult:
            return invoke(run_bash, command, run_in_background=run_in_background)
    else:
        def bash(command: str) -> ToolResult:
            return invoke(run_bash, command)

    def read(path: str, limit: int | None = None, offset: int = 0) -> ToolResult:
        return invoke(run_read, path, limit=limit, offset=offset)

    def write(path: str, content: str) -> ToolResult:
        return invoke(run_write, path, content)

    def edit(path: str, old_text: str, new_text: str) -> ToolResult:
        return invoke(run_edit, path, old_text, new_text)

    def glob(pattern: str) -> ToolResult:
        return invoke(run_glob, pattern)

    return {"bash": bash, "read_file": read, "write_file": write,
            "edit_file": edit, "glob": glob}


_LEAD_HANDLERS = bind_file_tools("agent", allow_background=True)


def run_agent_bash(command: str, run_in_background: bool = False) -> ToolResult:
    """Execute bash with the Lead's current assignment."""
    return _LEAD_HANDLERS["bash"](command, run_in_background)


def run_agent_read(path: str, limit: int | None = None, offset: int = 0) -> ToolResult:
    """Read with the Lead's current assignment."""
    return _LEAD_HANDLERS["read_file"](path, limit, offset)


def run_agent_write(path: str, content: str) -> ToolResult:
    """Write with the Lead's current assignment."""
    return _LEAD_HANDLERS["write_file"](path, content)


def run_agent_edit(path: str, old_text: str, new_text: str) -> ToolResult:
    """Edit with the Lead's current assignment."""
    return _LEAD_HANDLERS["edit_file"](path, old_text, new_text)


def run_agent_glob(pattern: str) -> ToolResult:
    """Search with the Lead's current assignment."""
    return _LEAD_HANDLERS["glob"](pattern)


def run_todo_write(todos: list) -> ToolResult:
    """校验并替换当前会话的内存 TODO 列表。

    输入：todos 为协议层已解析的列表，每项包含 content 和 status。
    返回：ToolResult，包含清单统计或错误及明确状态。
        成功时由 LeadContext 替换清单，不写任务 JSON。
    """
    execution = interaction.current_context()
    if execution is not None and execution.source != "lead":
        return ToolResult("Error: child cannot update Lead Todo", "denied")
    try:
        todos = current_lead_context().replace_todos(todos)
    except ValueError as exc:
        return ToolResult(str(exc), "failed")
    # 整份列表通过检查后再更新；失败不会发布事件或覆盖原状态。
    interaction.publish("todo_updated", todos=todos)
    core.terminal_print(f"  \033[33m[todo] updated {len(todos)} item(s)\033[0m")
    # 统计本次接受的列表，不从其他工具结果推断任务完成情况。
    counts = {status: 0 for status in ("completed", "in_progress", "pending")}
    for todo in todos:
        counts[todo["status"]] += 1
    return ToolResult(f"已更新 {len(todos)} 项待办：完成 {counts['completed']} 项，"
            f"进行中 {counts['in_progress']} 项，待处理 {counts['pending']} 项。", "success")
