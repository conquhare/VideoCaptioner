"""Unified LLM client for the application."""

import os
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, List, Optional
from urllib.parse import urlparse, urlunparse

import openai
from openai import OpenAI
from tenacity import (
    Retrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from videocaptioner.core.utils.cache import get_llm_cache, memoize
from videocaptioner.core.utils.logger import setup_logger

from .request_logger import create_logging_http_client, log_llm_response

logger = setup_logger("llm_client")


class EmptyResponseError(Exception):
    """LLM 返回了空 choices/content（偶发：限流软化、内容过滤、服务抖动）。

    可安全重试——同一请求重发通常即可恢复。
    """

# ── 限流感知节流 ──────────────────────────────────────────────
# 任一线程遇到 RateLimitError 时触发全局冷却窗口，让所有并发线程
# 在发起下一次 LLM 调用前先等待，避免对低 RPM 模型形成请求风暴。
# 仅在发生限流时生效，正常情况下零开销。
_throttle_lock = threading.Lock()
_cooldown_until: float = 0.0          # monotonic 时间戳，冷却截止时刻
_COOLDOWN_SECONDS: float = 8.0        # 单次限流触发的默认冷却时长（秒）
_MAX_COOLDOWN_SECONDS: float = 120.0  # 冷却窗口上限，防止并发 429 无限堆叠


def _trigger_cooldown(seconds: float = _COOLDOWN_SECONDS) -> None:
    """触发全局冷却（限流时调用）。

    取 max 略延长冷却，但不超过 _MAX_COOLDOWN_SECONDS，
    避免并发 429 把等待时间无限累加。
    """
    global _cooldown_until
    with _throttle_lock:
        now = time.monotonic()
        proposed = min(now + seconds, now + _MAX_COOLDOWN_SECONDS)
        _cooldown_until = max(_cooldown_until, proposed)


def _wait_for_cooldown() -> None:
    """发起 LLM 调用前调用：若处于全局冷却期则等待到冷却结束。"""
    while True:
        with _throttle_lock:
            remaining = _cooldown_until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5.0))


def _parse_retry_after(exc: Exception) -> Optional[float]:
    """从 RateLimitError 中解析服务端返回的 Retry-After 头（秒）。

    兼容两种格式：
    - 秒数（整数/小数字符串），如 "30"
    - HTTP 日期（RFC 1123），如 "Wed, 21 Oct 2015 07:28:00 GMT"

    头缺失或无法解析时返回 None，由调用方回落到默认随机退避。
    """
    try:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None) if response is not None else None
        if not headers:
            return None
        value = headers.get("retry-after")
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        # 先按秒数解析
        try:
            seconds = float(value)
            return seconds if seconds >= 0 else None
        except ValueError:
            pass
        # 再按 HTTP 日期解析（相对当前时间换算为秒）
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if target is None:
            return None
        now = time.time()
        if target.tzinfo is None:
            # 无时区信息时按本地时间处理
            delta = target.timestamp() - now
        else:
            delta = target.timestamp() - now
        return delta if delta > 0 else 0.0
    except Exception:  # 解析头时任何异常都不应影响主重试流程
        return None


# ── 主动限速(令牌桶,按 RPM 预算匀速发车)──────────────────
# 与上面的 429 被动冷却叠加为双重保险:前者主动避免超限,
# 后者在仍被服务端限流时兜底。每次调用实时读取配置,支持运行时改值。
_rpm_lock = threading.Lock()
_rpm_tokens: float = -1.0        # <0 表示尚未初始化(首次调用按配置填充)
_rpm_last_refill: float = 0.0

# 显式 RPM 覆盖值：由 CLI/驱动进程在启动时调用 set_rpm_override() 注入，
# 优先级高于 UI 配置回退。headless 环境无法读取 UI 配置(cfg 导入常失败)，
# 若无此覆盖，令牌桶将始终按默认 20 RPM 放行，导致上游低 RPM 免费额度被
# 频繁 429 打回。None 表示未覆盖，行为与旧版完全一致。
_rpm_override: Optional[int] = None


def set_rpm_override(rpm: Optional[int]) -> None:
    """设置进程级 RPM 覆盖值（优先于 UI 配置）。

    供 CLI / 后台驱动脚本在启动时调用，传入一个较低 RPM（如 4）以匹配
    上游免费额度，无需导入 UI 配置。传 None 可清除覆盖、恢复默认读取行为。

    Args:
        rpm: 每 分钟 允许的请求数；>=1 生效，<=0 或 None 视为不覆盖。
    """
    global _rpm_override
    with _rpm_lock:
        _rpm_override = rpm if (rpm is not None and rpm >= 1) else None


def _get_rpm_limit() -> int:
    """惰性读取 RPM 配置(避免 core 在导入期依赖 UI 配置)。

    优先级：显式覆盖(set_rpm_override) > UI 配置(cfg.llm_rpm_limit) > 默认 20。
    """
    with _rpm_lock:
        override = _rpm_override
    if override is not None:
        return override
    try:
        from videocaptioner.ui.common.config import cfg

        val = int(getattr(cfg, "llm_rpm_limit", 20) or 20)
        return val if val > 0 else 0
    except Exception:
        return 20


# ── 网络层重试次数(单个请求失败/限流时的自动重试)──────────────────
# 与 RPM 同样的覆盖模式：CLI 可在启动时 set_max_retries() 注入，优先于 UI 配置。
# 默认 4(中等)，明显低于旧版硬编码的 10，使单条请求在持续失败时更快失败，
# 交由更高层的批级/UI 重试兜底。
_retry_lock = threading.Lock()
_max_retries_override: Optional[int] = None
_DEFAULT_MAX_RETRIES: int = 4


def set_max_retries(n: Optional[int]) -> None:
    """设置进程级网络重试次数覆盖值(优先于 UI 配置)。

    供 CLI / 后台驱动脚本在启动时调用。传 None 可清除覆盖、恢复默认读取行为。

    Args:
        n: 单个请求的最大尝试次数；>=1 生效，<=0 或 None 视为不覆盖。
    """
    global _max_retries_override
    with _retry_lock:
        _max_retries_override = n if (n is not None and n >= 1) else None


def _get_max_retries() -> int:
    """惰性读取网络重试次数配置(避免 core 在导入期依赖 UI 配置)。

    优先级：显式覆盖(set_max_retries) > UI 配置(cfg.llm_max_retries) > 默认 4。
    """
    with _retry_lock:
        override = _max_retries_override
    if override is not None:
        return override
    try:
        from videocaptioner.ui.common.config import cfg

        val = int(getattr(cfg, "llm_max_retries", _DEFAULT_MAX_RETRIES) or _DEFAULT_MAX_RETRIES)
        return max(1, val)
    except Exception:
        return _DEFAULT_MAX_RETRIES


def _build_retrying() -> "Retrying":
    """按当前配置构建 tenacity.Retrying(每次调用实时读取重试次数)。

    旧版把 stop=stop_after_attempt(10) 写死在 @retry 装饰器里，于导入期固化、
    无法随配置变化。这里改为每次调用重建 Retrying，使 UI/CLI 改的重试次数能
    在下一次请求即时生效。wait/retry/before_sleep 与旧版完全一致：
    - 仅对 RateLimitError / EmptyResponseError 重试
    - before_sleep_log 会触发全局 429 冷却(含 Retry-After 精确等待)
    """
    return Retrying(
        stop=stop_after_attempt(_get_max_retries()),
        wait=wait_random_exponential(multiplier=1, min=5, max=60),
        retry=retry_if_exception_type((openai.RateLimitError, EmptyResponseError)),
        before_sleep=before_sleep_log,
    )


def _acquire_token() -> None:
    """令牌桶:按 RPM 预算匀速放行,无可用令牌则等待补充。

    rpm=20 → 每 3 秒补充 1 个令牌;并发线程共享同一桶,
    整体请求速率被限制在 rpm/分钟 以内,从源头避免突发 429。
    """
    global _rpm_tokens, _rpm_last_refill
    rpm = _get_rpm_limit()
    if rpm <= 0:
        return  # 配置为 0 或不限制
    while True:
        with _rpm_lock:
            now = time.monotonic()
            if _rpm_tokens < 0:
                _rpm_tokens = float(rpm)
                _rpm_last_refill = now
            elapsed = now - _rpm_last_refill
            _rpm_tokens = min(float(rpm), _rpm_tokens + elapsed * (rpm / 60.0))
            _rpm_last_refill = now
            if _rpm_tokens >= 1.0:
                _rpm_tokens -= 1.0
                return
            wait = (1.0 - _rpm_tokens) / (rpm / 60.0)
        time.sleep(max(wait, 0.01))


def normalize_base_url(base_url: str) -> str:
    """Normalize API base URL.

    处理用户常见填法错误：
    - 缺失协议头（如 apihub.agnes-ai.com/v1）时默认补 https://
    - 自动剥离 OpenAI 客户端会自动追加的 /chat/completions 后缀
    - 缺失 /v1 路径时默认补上
    """
    url = base_url.strip()
    parsed = urlparse(url)

    # 1. 缺失协议头时默认补 https://（否则 urlparse 会把整串当 path）
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    # 2. 剥离 OpenAI 客户端自动追加的 /chat/completions
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    path = path.rstrip("/")

    # 3. 缺失 /v1 时补上（兼容仅填域名的情形）
    if not path:
        path = "/v1"

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _build_llm_client(base_url: str, api_key: str) -> OpenAI:
    """按传入的 base_url/api_key 构建一个 OpenAI client（不缓存，随调随建）。

    显式传参取代旧的「进程级 env + 全局单例」方案，避免并发任务串号、
    以及换配置后复用旧 client 的问题。
    """
    if not base_url or not api_key:
        raise ValueError("base_url and api_key are required to build an LLM client")
    return OpenAI(
        base_url=normalize_base_url(base_url),
        api_key=api_key,
        http_client=create_logging_http_client(),
    )


def before_sleep_log(retry_state: RetryCallState) -> None:
    # 限流时触发全局冷却，让其它并发线程也暂停，避免请求风暴压垮低 RPM 模型
    _trigger_cooldown()
    logger.warning(
        "Rate Limit Error, sleeping and retrying... Please lower your thread concurrency or use better OpenAI API."
    )


def _call_llm_api(
    messages: List[dict],
    model: str,
    temperature: float = 1,
    **kwargs: Any,
) -> Any:
    """实际调用 LLM API（带重试）。base_url/api_key 经 **kwargs 透传，每次按调用方配置建连。

    重试次数在每次调用时实时读取配置（_build_retrying → _get_max_retries），
    因此 UI/CLI 修改的「网络重试次数」能即时生效，而非旧版导入期固化的 10。
    """
    return _build_retrying()(
        _call_llm_api_once, messages, model, temperature, **kwargs
    )


def _call_llm_api_once(
    messages: List[dict],
    model: str,
    temperature: float = 1,
    **kwargs: Any,
) -> Any:
    """单次 LLM 请求（无重试）。由 _call_llm_api 通过 tenacity 按当前配置驱动重试。"""
    _acquire_token()
    _wait_for_cooldown()
    base_url = kwargs.pop("base_url", "")
    api_key = kwargs.pop("api_key", "")
    reasoning_effort = kwargs.pop("reasoning_effort", "none")
    client = _build_llm_client(base_url, api_key)

    create_kwargs = dict(kwargs)
    # "none"=关闭思考，不传该参数(合法值仅 low/medium/high/max)
    if reasoning_effort and reasoning_effort != "none":
        create_kwargs["reasoning_effort"] = reasoning_effort

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,  # pyright: ignore[reportArgumentType]
            temperature=temperature,
            **create_kwargs,
        )
    except openai.RateLimitError as e:
        # 服务端明确告知限流：优先尊重 Retry-After 头，把全局冷却窗口设为
        # 服务端要求的秒数（仍受 _MAX_COOLDOWN_SECONDS 约束）。这样下一次
        # tenacity 重试在 _wait_for_cooldown() 会精确等到服务端允许的时刻，
        # 避免盲目随机退避造成的「欠等（立刻再吃 429）」或「过等（服务端只要
        # 5s 却硬等 60s）」。头缺失时回落到 before_sleep_log 的默认冷却 + 随机退避。
        retry_after = _parse_retry_after(e)
        if retry_after is not None:
            _trigger_cooldown(retry_after)
            logger.warning(
                f"Rate limited; honoring server Retry-After={retry_after:.1f}s"
            )
        raise  # 交回 tenacity 按策略重试

    # 记录响应内容
    log_llm_response(response)

    # 空响应校验下沉到本层：偶发空 choices/content 也走 tenacity 指数退避重试，
    # 避免一次服务抖动就判死整个断句/优化任务。
    if not (
        response
        and hasattr(response, "choices")
        and response.choices
        and len(response.choices) > 0
        and hasattr(response.choices[0], "message")
        and response.choices[0].message.content
    ):
        raise EmptyResponseError(
            "Invalid OpenAI API response: empty choices or content"
        )

    return response


@memoize(get_llm_cache(), expire=3600, typed=True)
def call_llm(
    messages: List[dict],
    model: str,
    temperature: float = 1,
    **kwargs: Any,
) -> Any:
    """Call LLM API with automatic caching."""
    # 重试已由 _call_llm_api 完成；此处仅作最终兜底（正常不会触发）
    return _call_llm_api(messages, model, temperature, **kwargs)
