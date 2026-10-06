"""Shared runtime configuration for the harness package.

Every other module reads the launch directory, the API client, and the model
ids from here, so a single module controls where state lands and which model
answers. Import order: this module must load before any feature module.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .llm_client import Anthropic
from dotenv import load_dotenv

try:
    import fcntl
except ImportError:  # Windows has no fcntl module.
    fcntl = None

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
PACKAGE_DIR = Path(__file__).resolve().parent
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# Runtime state follows the launch directory; assets ship inside the package.
SKILLS_DIR = PACKAGE_DIR / "skills"


def transcript_dir() -> Path:
    return WORKDIR / ".transcripts"


def tool_results_dir() -> Path:
    return WORKDIR / ".task_outputs" / "tool-results"

DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36magent-harness >> \033[0m"
# \001/\002 tell Readline the ANSI escapes have zero display width.
READLINE_PROMPT = "\001\033[36m\002agent-harness >> \001\033[0m\002"
CLI_ACTIVE = False


class ConsoleBroker:
    """Serialize normal prompts and worker permission questions on one stdin."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reader = None
        self.display_prompt = PROMPT
        self.readline_prompt = READLINE_PROMPT

    def set_prompt(self, display_prompt: str, readline_prompt: str):
        self.display_prompt = display_prompt
        self.readline_prompt = readline_prompt

    def ask(self, prompt: str | None = None) -> str:
        with self._lock:
            active_prompt = self.readline_prompt if prompt is None else prompt
            return (self.reader or input)(active_prompt)


CONSOLE = ConsoleBroker()


def terminal_print(text: str):
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        print(text)
        return
    line = ""
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()
        except Exception:
            line = ""
    print(f"\r\033[K{text}")
    print(CONSOLE.display_prompt + line, end="", flush=True)
