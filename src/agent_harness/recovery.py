"""模型调用的重试与错误恢复。

记录当前模型、重试额度和连续过载次数，让主循环在本轮中复用状态完成回退。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from . import core

# -- 错误恢复 --

class RecoveryState:
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = core.PRIMARY_MODEL


def retry_delay(attempt: int) -> float:
    base = min(core.BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def with_retry(fn, state: RecoveryState):
    """带重试与故障降级的包装器，执行目标函数，针对429限流、529服务过载做重试处理。
    Args:
        fn: 无参可调用对象，待执行的业务函数
        state: 恢复状态实例，保存连续529计数、当前使用模型等状态，用于触发模型降级
    Returns:
        fn执行成功后的返回结果
    Raises:
        RuntimeError: 达到最大重试次数仍然失败时抛出
        Exception: 非限流/非过载类异常，不重试，直接向上抛出
    Note:
        429限流：等待后直接重试，重置连续529计数器；
        529服务过载：累加连续失败计数，连续多次触发则自动切换到备用模型；
        其他类型异常不重试，直接抛出。
    """
    # 循环进行多次尝试，attempt为当前重试轮次
    for attempt in range(core.MAX_RETRIES):
        try:
            result = fn()
            # 执行成功，清空连续529过载计数
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__.lower()
            msg = str(e).lower()
            # 捕获429限流错误，延时后重试
            if "ratelimit" in name or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429] retry {attempt + 1}/{core.MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            # 捕获529服务过载错误，累计失败次数，达到阈值切换备用模型
            if "overloaded" in name or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= core.MAX_CONSECUTIVE_529 and core.FALLBACK_MODEL:
                    state.current_model = core.FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"  \033[31m[529] switching to {core.FALLBACK_MODEL}\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529] retry {attempt + 1}/{core.MAX_RETRIES} "
                      f"after {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue
            # 不属于限流/过载的异常，直接抛出，不重试
            raise
    # 所有重试次数耗尽，抛出异常
    raise RuntimeError(f"Max retries ({core.MAX_RETRIES}) exceeded")



def is_prompt_too_long_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)
