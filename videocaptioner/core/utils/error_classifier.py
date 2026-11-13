from enum import Enum


class FatalErrorType(str, Enum):
    """致命错误类别，供 UI 决定文案与是否禁用手动重试。"""

    NONE = "none"
    AUTH_QUOTA = "auth_quota"  # 403 / 欠费 / 余额不足 —— 不可自动重试
    SERVICE_UNAVAILABLE = "service"  # 503 / 服务不可用 —— 条件恢复
    NETWORK = "network"  # 网络中断
    UNKNOWN = "unknown"


# 关键字分类表：按顺序匹配，先命中先归类（区分大小写关键字统一转小写后比对）
_KEYWORD_RULES: list[tuple[FatalErrorType, tuple[str, ...]]] = [
    (
        FatalErrorType.AUTH_QUOTA,
        (
            "403",
            "quota",
            "insufficient",
            "balance",
            "余额",
            "欠费",
            "预扣费",
        ),
    ),
    (
        FatalErrorType.SERVICE_UNAVAILABLE,
        (
            "503",
            "unavailable",
            "service unavailable",
            "model_not_found",
            "no available channel",
        ),
    ),
    (
        FatalErrorType.NETWORK,
        (
            "timeout",
            "connection",
            "network",
            "econnreset",
            "连接",
        ),
    ),
]


def classify_error(exc_or_msg: object) -> FatalErrorType:
    """从异常/错误文本识别致命错误类别。

    Args:
        exc_or_msg: Exception 实例或任意错误文本。传入异常对象时取 `str()`。

    Returns:
        命中的 `FatalErrorType`；无法归类返回 `UNKNOWN`；
        空字符串/None 返回 `NONE`。
    """
    if exc_or_msg is None:
        return FatalErrorType.NONE

    text = str(exc_or_msg).strip()
    if not text:
        return FatalErrorType.NONE

    lowered = text.lower()
    for error_type, keywords in _KEYWORD_RULES:
        if any(keyword in lowered for keyword in keywords):
            return error_type

    return FatalErrorType.UNKNOWN
