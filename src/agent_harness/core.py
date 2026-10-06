"""运行时共享配置。

其他模块从这里读取启动目录、API 客户端和模型 ID，因此状态位置与模型选择
由一个模块统一管理。导入顺序上，本模块必须先于功能模块加载。
"""
from __future__ import annotations   # 让类型注解按"字符串"延迟解析（3.7+），
                                     # 避免前向引用报错（如 list[str] 写法）

import os                            # 环境变量、路径操作
import threading                     # 控制台锁、线程判断
from pathlib import Path             # 面向对象的路径操作
from .llm_client import Anthropic    # 从同包 llm_client 导入 Anthropic 客户端类
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
    """转录记录目录：启动目录下的 .transcripts。"""
    return WORKDIR / ".transcripts"


def tool_results_dir() -> Path:
    """工具结果目录：启动目录下按任务隔离存放。"""
    return WORKDIR / ".task_outputs" / "tool-results"


# ---------- 运行参数常量 ----------
DEFAULT_MAX_TOKENS = 8000           # 默认最大生成 token 数
ESCALATED_MAX_TOKENS = 16000        # 升级/长任务时的上限
MAX_RETRIES = 3                     # API 调用失败最大重试次数
MAX_CONSECUTIVE_529 = 2             # 连续 529（限流）容忍次数
MAX_RECOVERY_RETRIES = 2            # 恢复后的最大重试次数
BASE_DELAY_MS = 500                 # 重试基础退避延迟（毫秒）
CONTEXT_LIMIT = 50000               # 上下文长度上限（防止超模型窗口）
KEEP_RECENT_TOOL_RESULTS = 3        # 历史中保留最近几条工具结果
PERSIST_THRESHOLD = 30000           # 超过该长度就持久化/截断上下文
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."  # 续写提示词

# ---------- 控制台提示符 ----------
PROMPT = "\033[36magent-harness >> \033[0m"   # 带 ANSI 颜色（青色）的普通提示符
# \001/\002 告诉 Readline：ANSI 转义序列不占显示宽度。
READLINE_PROMPT = "\001\033[36m\002agent-harness >> \001\033[0m\002"   # 给 readline 用的版本

CLI_ACTIVE = False                  # 是否处于 CLI 模式（run_cli 里会置 True）


class ConsoleBroker:
    """让普通输入和工作线程的权限确认依次使用同一个标准输入。"""
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
