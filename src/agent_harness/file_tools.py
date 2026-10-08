"""基础文件与 shell 工具。

所有工具都通过 safe_path 相对 cwd 解析路径，因此绑定任务的 worktree 会改变
读写位置。run_agent_* 则用当前任务分配的 cwd 包装同一组工具。
"""

from __future__ import annotations

import ast
import atexit
import glob
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import core
from . import tasks

# -- 基础工具 --


def safe_path(path: str, cwd: Path | None = None) -> Path:
    """解析文件路径，并确保真实路径位于指定工作目录内。

    输入：path 为相对或绝对路径；cwd 为允许的根目录，默认使用启动目录。
    返回：解析符号链接后的绝对 Path；越界时抛出 ValueError，不创建文件。
    """
    base = (cwd or core.WORKDIR).resolve()
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved


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
                process.wait(timeout=0.05)
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
        time.sleep(0.05)


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
        stdout, stderr = process.communicate(timeout=120)
        out = (stdout + stderr).strip()
        return (out[:50000] if out else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)", None
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}", None
    finally:
        if process is not None:
            _stop_process_group(process)
            try:
                process.wait(timeout=0.2)
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
             run_in_background: bool = False) -> str:
    """同步执行 shell 命令并返回格式化结果。

    输入：command 为命令；cwd 为执行目录；run_in_background 由外层分发器处理，
        直接调用本函数时不改变同步执行行为。
    返回：输出或错误字符串；权限确认由调用方在执行前完成。
    """
    # run_in_background 由分发器处理；直接执行时忽略它。
    return _format_bash_result(*_run_bash_process(command, cwd))


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path | None = None) -> str:
    """读取工作目录内文件，或按绝对路径读取运行时归档。

    输入：path 为文件路径；limit 为最多读取行数；offset 为跳过行数；cwd 为任务目录。
    返回：文件文本或错误字符串；未展示完时附加剩余行数提示。
        归档目录内绝对路径按启动目录校验，其他路径按 cwd 校验，默认使用启动目录。
    """
    try:
        absolute = Path(path)
        if absolute.is_absolute() and any(
                absolute.resolve().is_relative_to(directory.resolve())
                for directory in (core.tool_results_dir(), core.transcript_dir())):
            # Runtime archive paths remain readable when the task cwd changes.
            cwd = core.WORKDIR
        file_path = safe_path(path, cwd)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path | None = None) -> str:
    """在允许的目录内创建或覆盖 UTF-8 文本文件。

    输入：path 为路径；content 为正文；cwd 为允许根目录，默认使用启动目录。
    返回：写入确认或错误字符串；成功时按需创建父目录并替换文件内容。
    """
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path | None = None) -> str:
    """替换文件中第一次出现的指定文本。

    输入：path 为路径；old_text 为原文；new_text 为替换文本；cwd 为允许根目录。
    返回：编辑确认或错误字符串；原文未找到时不写文件。
    """
    try:
        fp = safe_path(path, cwd)
        text = fp.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path | None = None) -> str:
    """搜索指定目录内匹配 glob 模式的路径。

    输入：pattern 为模式，支持 ** 递归；cwd 为搜索根目录，默认使用启动目录。
    返回：排序去重后的路径文本，最多展示 200 项；无匹配或异常时返回提示。
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
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _agent_cwd() -> tuple[Path | None, str | None]:
    """取得主 agent 当前任务的有效工具目录。

    返回：(目录, None)，无分配时使用启动目录；分配缺失或失效时返回 (None, 错误文本)。
    """
    try:
        return tasks.assignment_cwd("agent"), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Error: Invalid task assignment: {exc}"


def run_agent_bash(command: str, run_in_background: bool = False) -> str:
    """按主 agent 当前分配执行 shell 工具。

    输入：command 为命令；run_in_background 为交给外层调度器解释的后台标志。
    返回：分配错误或 run_bash 的结果字符串。
    """
    cwd, error = _agent_cwd()
    return error or run_bash(command, cwd, run_in_background)


def run_agent_read(path: str, limit: int | None = None,
                   offset: int = 0) -> str:
    """按主 agent 当前分配读取文件。

    输入：path 为路径；limit/offset 为读取行数上限和跳过行数。
    返回：分配错误或 run_read 的文本结果；归档绝对路径遵循共享读取规则。
    """
    cwd, error = _agent_cwd()
    return error or run_read(path, limit, offset, cwd)


def run_agent_write(path: str, content: str) -> str:
    """按主 agent 当前分配写入文件。

    输入：path 为文件路径；content 为正文。
    返回：分配错误或写入结果字符串，成功时修改对应任务目录中的文件。
    """
    cwd, error = _agent_cwd()
    return error or run_write(path, content, cwd)


def run_agent_edit(path: str, old_text: str, new_text: str) -> str:
    """按主 agent 当前分配替换文件文本。

    输入：path 为路径；old_text/new_text 为原文和替换内容。
    返回：分配错误或编辑结果字符串。
    """
    cwd, error = _agent_cwd()
    return error or run_edit(path, old_text, new_text, cwd)


def run_agent_glob(pattern: str) -> str:
    """按主 agent 当前分配搜索文件。

    输入：pattern 为 glob 模式。
    返回：分配错误、匹配路径列表或无匹配提示。
    """
    cwd, error = _agent_cwd()
    return error or run_glob(pattern, cwd)


def call_tool_handler(handler, args: dict, name: str) -> str:
    """统一执行同步工具，将参数字典展开并把结果转换为文本。

    输入：handler 为查表得到的函数或 None；args 为关键字参数字典；name 为工具名称。
    返回：字符串结果；无处理器时返回未知工具，执行异常时返回异常类型及说明。
        本函数不执行权限检查，调用方须先经过相应 hooks。
    """
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return str(handler(**(args or {})))
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, todo in enumerate(todos):
        if not isinstance(todo, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in todo or "status" not in todo:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{todo['status']}'"
    return todos, None

def run_todo_write(todos: list) -> str:
    """校验并替换当前会话的内存 TODO 列表。

    输入：todos 为包含 content 和 status 的条目列表，也支持规范化器接受的字符串。
    返回：更新数量的确认或错误文本；成功时替换 tasks.CURRENT_TODOS，不写任务 JSON。
    """
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error
    tasks.CURRENT_TODOS = todos
    print(f"  \033[33m[todo] updated {len(tasks.CURRENT_TODOS)} item(s)\033[0m")
    return f"Updated {len(tasks.CURRENT_TODOS)} todos"
