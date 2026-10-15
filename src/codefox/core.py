"""运行时共享配置。

其他模块从这里读取启动目录、API 客户端和模型 ID，因此状态位置与模型选择
由一个模块统一管理。导入顺序上，本模块必须先于功能模块加载。
"""
from __future__ import annotations   # 让类型注解按"字符串"延迟解析（3.7+），
                                     # 避免前向引用报错（如 list[str] 写法）

import os                            # 环境变量、路径操作
import re                             # 会话 ID 校验
import threading                     # 控制台锁、线程判断
from pathlib import Path             # 面向对象的路径操作
from .providers.llm_client import Anthropic    # 从同包 llm_client 导入 Anthropic 客户端类
from dotenv import load_dotenv       # 读取 .env 文件的库

# ---------- 平台差异适配 ----------
try:
    import fcntl                    # Unix 文件锁模块
except ImportError:                 # Windows 没有 fcntl 模块。
    fcntl = None                    # 置为 None，后面代码用它时需判空

try:
    import readline                 # 命令行行编辑（历史记录、补全、光标）
    readline.parse_and_bind('set bind-tty-special-chars off')  # 关掉 tty 特殊字符绑定，避免干扰
    READLINE_AVAILABLE = True
except ImportError:                 # 非交互环境（如管道/CI）没有 readline
    READLINE_AVAILABLE = False

# ---------- 环境变量加载 ----------
load_dotenv(override=True)          # 读取 .env 文件；override=True 表示已存在的变量也被覆盖
if os.getenv("ANTHROPIC_BASE_URL"):
    # 自定义 API 地址时不带 auth token，避免被外部地址误用凭据
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ---------- 路径常量 ----------
WORKDIR = Path.cwd()                # 启动目录（运行时状态放这，跟随"从哪里启动"）
PACKAGE_DIR = Path(__file__).resolve().parent   # 包源码目录（静态资源放这，跟随代码位置）

# ---------- API 客户端与模型 ----------
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))   # 全局唯一客户端
MODEL = os.environ["MODEL_ID"]      # 主模型（用 [] 取，缺了直接报错，强制必须配置）
PRIMARY_MODEL = MODEL               # 主模型别名
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")   # 备用模型（可选，getenv 缺了返回 None）

# 运行时状态跟随启动目录；静态资源随包分发。
SKILLS_DIR = PACKAGE_DIR / "skills" # 技能目录（静态资源，随包走）


def transcript_dir() -> Path:
    """当前会话有上下文时返回会话归档目录，否则返回启动目录回退目录。"""
    from .runtime import interaction
    context = interaction.current_context()
    session_id = context.session_id if context is not None else None
    if session_id and re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", session_id):
        return session_artifacts_dir(session_id) / "transcripts"
    return WORKDIR / ".transcripts"


def sessions_dir() -> Path:
    """会话持久化目录。"""
    from .runtime.session_store import sessions_dir as session_directory
    return session_directory()


def session_artifacts_dir(session_id: str) -> Path:
    """返回指定会话的独立产物目录。"""
    if not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", session_id):
        raise ValueError("Invalid session ID")
    return sessions_dir() / f"{session_id}.artifacts"


def tool_results_dir() -> Path:
    """当前会话有上下文时返回会话工具结果目录，否则返回启动目录目录。"""
    from .runtime import interaction
    context = interaction.current_context()
    session_id = context.session_id if context is not None else None
    if session_id and re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", session_id):
        return session_artifacts_dir(session_id) / "tool-results"
    return workspace_tool_results_dir()


def workspace_tool_results_dir() -> Path:
    """启动目录下不属于某个会话的工具与后台任务结果目录。"""
    return WORKDIR / ".task_outputs" / "tool-results"


# ---------- 运行参数常量 ----------
DEFAULT_MAX_TOKENS = 32000           # 默认最大生成 token 数
ESCALATED_MAX_TOKENS = 64000        # 升级/长任务时的上限
WORKFLOW_AGENT_MAX_TOKENS = 20000    # 工作流步骤输出上限
COMPACTION_MAX_TOKENS = 20000        # 历史压缩摘要输出上限
MEMORY_SELECTION_MAX_TOKENS = 2000  # 记忆筛选结果输出上限
MEMORY_EXTRACTION_MAX_TOKENS = 10000 # 新记忆抽取输出上限
MEMORY_CONSOLIDATION_MAX_TOKENS = 30000  # 记忆合并输出上限
# 长期记忆会在回合结束后额外调用模型；MVP 默认关闭，显式设为 1/true/on 才启用。
MEMORY_ENABLED = os.getenv("AGENT_MEMORY_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
MAX_RETRIES = 3                     # API 调用失败最大重试次数
MAX_CONSECUTIVE_529 = 2             # 连续 529（限流）容忍次数
MAX_RECOVERY_RETRIES = 2            # 恢复后的最大重试次数
BASE_DELAY_MS = 500                 # 重试基础退避延迟（毫秒）
MAX_RETRY_DELAY_MS = 32000          # 重试退避延迟上限（毫秒，不含抖动）
RETRY_JITTER_RATIO = 0.25           # 在退避延迟上追加的随机抖动比例
SUBAGENT_MAX_ROUNDS = 12            # 每个子代理工作单元的模型调用轮数上限
SUBAGENT_MAX_TOOL_CALLS = 40        # 每个子代理工作单元的工具调用上限
ASYNC_EVENT_POLL_SECONDS = 1        # 主循环后台事件检查间隔（秒）
CRON_POLL_SECONDS = 1               # 定时任务检查间隔（秒）

# ---------- 工具执行参数 ----------
BASH_TIMEOUT_SECONDS = 120          # 同步及后台 shell 命令超时（秒）
BASH_OUTPUT_CHAR_LIMIT = 500000      # shell 返回结果的字符上限
GIT_TIMEOUT_SECONDS = 30            # Git 命令超时（秒）
GIT_OUTPUT_CHAR_LIMIT = 50000        # Git 工具返回结果的字符上限
GLOB_RESULT_LIMIT = 200             # glob 最多展示的路径数
PROCESS_TERMINATION_GRACE_SECONDS = 0.05  # 终止信号到强制结束的等待时间
PROCESS_REAP_TIMEOUT_SECONDS = 0.2  # 结束后回收 shell 进程的等待时间
LARGE_OUTPUT_WARNING_CHARS = 100000 # 大工具输出的日志提醒阈值

# ---------- 上下文压缩参数 ----------
CONTEXT_LIMIT = int(os.getenv("MODEL_CONTEXT_TOKENS", "258000"))  # 模型与服务端共同支持的上下文窗口（token）
CONTEXT_SAFETY_RATIO = 0.1         # 为估算误差和请求开销预留的窗口比例
CONTEXT_SAFETY_MARGIN = int(CONTEXT_LIMIT * CONTEXT_SAFETY_RATIO)
CONTEXT_INPUT_BUDGET = CONTEXT_LIMIT - ESCALATED_MAX_TOKENS - CONTEXT_SAFETY_MARGIN
if CONTEXT_INPUT_BUDGET <= 0:
    raise ValueError(
        "MODEL_CONTEXT_TOKENS must exceed ESCALATED_MAX_TOKENS plus its safety margin"
    )
COMPACT_TRIGGER_RATIO = 0.9        # 可用输入预算达到此比例时开始压缩
COMPACT_TARGET_RATIO = 0.8         # 压缩后回落到可用输入预算的此比例
TOOL_RESULT_BUDGET_RATIO = 0.16    # 最新一批工具结果占可用输入预算的比例
MEMORY_RECALL_BUDGET_RATIO = 0.05  # 记忆召回占可用输入预算的比例
MEMORY_RECALL_MAX_TOKENS = 20000   # 记忆召回 token 硬上限
MEMORY_RECALL_MAX_CHARS = 20000    # 记忆正文字符硬上限
COMPACT_TRIGGER = int(CONTEXT_INPUT_BUDGET * COMPACT_TRIGGER_RATIO)
COMPACT_TARGET = int(CONTEXT_INPUT_BUDGET * COMPACT_TARGET_RATIO)
TOOL_RESULT_BUDGET = int(CONTEXT_INPUT_BUDGET * TOOL_RESULT_BUDGET_RATIO)
MEMORY_RECALL_TOKEN_BUDGET = min(
    int(CONTEXT_INPUT_BUDGET * MEMORY_RECALL_BUDGET_RATIO),
    MEMORY_RECALL_MAX_TOKENS,
)
TOOL_PREVIEW_CHARS = int(os.getenv("TOOL_PREVIEW_CHARS", "1000"))  # 落盘结果的字符预览长度
KEEP_RECENT_TOOL_RESULTS = 3        # 历史中保留最近几条工具结果
PERSIST_THRESHOLD = int(os.getenv("TOOL_PERSIST_THRESHOLD_CHARS", "30000"))  # 单条工具结果落盘阈值（字符）
COMPACTION_INPUT_TOKEN_LIMIT = COMPACT_TARGET  # 发送给摘要模型的历史 token 上限
COMPACTION_HEAD_MESSAGES = 3        # 归档中间历史时保留的开头消息数
COMPACTION_TAIL_MESSAGES = 5        # 被动压缩时保留的最近消息数
MIN_COMPACT_RESULT_CHARS = 120      # 不值得压缩落盘的短工具结果阈值
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."  # 续写提示词

# ---------- 控制台提示符 ----------
PROMPT = "\033[36mcodefox >> \033[0m"   # 带 ANSI 颜色（青色）的普通提示符
# \001/\002 告诉 Readline：ANSI 转义序列不占显示宽度。
READLINE_PROMPT = "\001\033[36m\002codefox >> \001\033[0m\002"   # 给 readline 用的版本

CLI_ACTIVE = False                  # 是否处于 CLI 模式（run_cli 里会置 True）


class ConsoleBroker:
    """The CLI adapter owns all normal and approval reads."""
    def __init__(self):
        self._lock = threading.Lock()       # 互斥锁：保证同时只有一个线程读输入
        self.reader = None                  # 可替换的输入函数（默认用内置 input）
        self.display_prompt = PROMPT        # 普通打印用的提示符
        self.readline_prompt = READLINE_PROMPT  # readline 用的提示符

    def set_prompt(self, display_prompt: str, readline_prompt: str):
        """外部可临时更换两种提示符。"""
        self.display_prompt = display_prompt
        self.readline_prompt = readline_prompt

    def ask(self, prompt: str | None = None) -> str:
        """读取一行用户输入；prompt 为 None 时用 readline 提示符。"""
        with self._lock:                    # 加锁，避免多线程抢输入
            # 没显式给 prompt 就用 readline 版提示符（否则用传入的）
            active_prompt = self.readline_prompt if prompt is None else prompt
            # 优先用自定义 reader（如 GUI 注入），否则用内置 input
            return (self.reader or input)(active_prompt)


CONSOLE = ConsoleBroker()           # 全局唯一的控制台入口


def terminal_print(text: str):
    """向终端打印文本；若在工作线程且 CLI 活跃，先清行再打印，避免把用户正在输入的内容冲掉。"""
    from .runtime import interaction
    if interaction.current_context() is not None:
        interaction.publish("log", text=str(text))
        return
    if threading.current_thread() is threading.main_thread() or not CLI_ACTIVE:
        # 主线程打印：直接输出（没有输入框冲突）
        print(text)
        return

    # 工作线程打印：需要保护用户正在输入的半行
    line = ""
    if READLINE_AVAILABLE:
        try:
            line = readline.get_line_buffer()   # 取出用户当前还没回车的那半行输入
        except Exception:
            line = ""                           # 取不到就当作空
    print(f"\r\033[K{text}")                    # \r 回行首 + \033[K 清空本行，再打印新文本
    # 重新打印提示符 + 用户那半行，把输入现场还原（flush 立即刷新）
    print(CONSOLE.display_prompt + line, end="", flush=True)
