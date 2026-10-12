"""统一工具权限判断与本机用户的权限模式设置。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

from .. import core
from ..tools import tool_registry


MODE_LABELS = {"default": "默认", "no_approval": "免审批"}
_cwd = ContextVar("permission_cwd", default=None)


def settings_path():
    return Path.home() / ".agent-harness" / "settings.json"


class PermissionSettings:
    """仅在启动时读取；保存成功后才发布新模式，不轮询其他实例。"""

    def __init__(self):
        self.mode = "default"

    def load(self):
        path = settings_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.mode = "default"
            return
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read permission settings {path}: {exc}") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("permission_mode"), str)
                or data["permission_mode"] not in MODE_LABELS):
            raise ValueError(f"Invalid permission settings: {path}")
        self.mode = data["permission_mode"]

    def save(self, mode):
        if mode not in MODE_LABELS:
            raise ValueError(f"Unknown permission mode: {mode}")
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                    dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"permission_mode": mode}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.mode = mode


SETTINGS = PermissionSettings()


@contextmanager
def working_directory(cwd):
    token = _cwd.set(cwd)
    try:
        yield
    finally:
        _cwd.reset(token)


@dataclass(frozen=True)
class PermissionDecision:
    action: str
    reason: str = ""
    path: Path | None = None


def _invalid(reason):
    return PermissionDecision("invalid", f"Error: Invalid arguments: {reason}")


def _shell_words(command):
    # 保留引号，避免把打印的分隔符误当作命令分隔符。这里只识别有限语法。
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _unquote(word):
    values = shlex.split(word)
    return values[0] if len(values) == 1 else word


def _forbidden(argv):
    while argv and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", argv[0]):
        argv = argv[1:]
    if not argv:
        return False
    name = Path(argv[0]).name
    if name in {"sudo", "shutdown", "reboot"} or name == "mkfs" or name.startswith("mkfs."):
        return True
    if name == "dd" and any(arg.startswith("if=") for arg in argv[1:]):
        return True
    if name == "rm":
        flags = "".join(arg[1:] for arg in argv[1:] if arg.startswith("-") and not arg.startswith("--"))
        recursive = "r" in flags or "R" in flags or "--recursive" in argv
        force = "f" in flags or "--force" in argv
        return recursive and force and any(arg.startswith("/") and Path(arg).resolve() == Path("/")
                                           for arg in argv[1:] if not arg.startswith("-"))
    # 常用执行包装仍按被执行的命令检查，不把普通参数里的单词当作命令。
    if name in {"env", "command", "exec"}:
        rest = argv[1:]
        while rest and (rest[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", rest[0])):
            rest = rest[1:]
        return _forbidden(rest)
    if name in {"sh", "bash"} and "-c" in argv:
        index = argv.index("-c") + 1
        return index < len(argv) and _shell_denied(argv[index])
    return False


def _shell_denied(command):
    try:
        words = _shell_words(command)
        segment = []
        for word in words + [";"]:
            if re.fullmatch(r"[;&|()\n]+", word):
                if _forbidden(segment):
                    return True
                segment = []
            else:
                segment.append(_unquote(word))
        # 未知表达式仍交给审批；不是完整 Shell 静态分析器。
        return False
    except (ValueError, OSError):
        return False


def _simple_read_only(command):
    if any(char in command for char in "\n\r$`"):
        return False
    try:
        raw = _shell_words(command)
        if any(re.fullmatch(r"[;&|()<>]+", word) for word in raw):
            return False
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    name, *args = argv
    if name == "pwd":
        return all(arg in {"-L", "-P"} for arg in args)
    if name in {"ls", "cat"}:
        flags = "aAlhdFRtrS1" if name == "ls" else "nbsETvA"
        options = True
        for arg in args:
            if arg == "--":
                options = False
            elif options and arg.startswith("-") and arg != "-":
                if arg.startswith("--") or any(char not in flags for char in arg[1:]):
                    return False
        return True
    if name != "git" or not args:
        return False
    subcommand, *args = args
    if subcommand == "status":
        allowed = {"--short", "-s", "--branch", "-b", "--porcelain", "--porcelain=1",
                   "--porcelain=2", "--untracked-files", "--untracked-files=no",
                   "--untracked-files=normal", "--untracked-files=all"}
        return all(arg in allowed for arg in args)
    if subcommand == "diff":
        allowed = {"--stat", "--name-only", "--name-status", "--cached", "--staged",
                   "--no-ext-diff", "--no-textconv"}
        paths = False
        for arg in args:
            if arg == "--":
                paths = True
            elif not paths and arg.startswith("-") and arg not in allowed:
                return False
        return True
    return False


def evaluate(block, *, mode):
    """返回允许、询问、拒绝或参数错误；不弹审批，不执行工具。"""
    args = block.input
    if not isinstance(args, dict):
        return _invalid("tool input must be an object")
    name = block.name
    if name == "bash":
        if not isinstance(args.get("command"), str):
            return _invalid("shell command must be a string")
        for key in ("run_in_background", "background"):
            if key in args and not isinstance(args[key], bool):
                return _invalid(f"{key} must be a boolean")
        command = args["command"]
        if _shell_denied(command):
            return PermissionDecision("deny", "Permission denied: command is on the deny list")
        action = "allow" if _simple_read_only(command) else "ask"
        reason = "shell"
        path = None
    elif name in {"read_file", "write_file", "edit_file"}:
        required = {"read_file": ("path",), "write_file": ("path", "content"),
                    "edit_file": ("path", "old_text", "new_text")}[name]
        for key in required:
            if not isinstance(args.get(key), str):
                return _invalid(f"{key} must be a string")
        for key in ("limit", "offset"):
            if key in args and type(args[key]) is not int:
                return _invalid(f"{key} must be an integer")
        try:
            cwd = _cwd.get() or core.WORKDIR
            cwd = (cwd() if callable(cwd) else cwd).resolve()
            path = (cwd / args["path"]).resolve()
        except (ValueError, OSError, RuntimeError) as exc:
            return _invalid(str(exc))
        action = "allow" if path.is_relative_to(core.WORKDIR.resolve()) else "ask"
        reason = "file"
    elif name.startswith("mcp__"):
        policy = tool_registry.REGISTRY.policy_for(name)
        if policy == "deny":
            return PermissionDecision("deny", "Permission denied: MCP tool policy")
        action = "allow" if policy == "allow" else "ask"
        reason, path = "MCP", None
    else:
        return PermissionDecision("allow")
    if action == "ask" and mode == "no_approval":
        action = "allow"
    return PermissionDecision(action, reason, path)
