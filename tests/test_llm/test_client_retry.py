"""验证 LLM 网络层重试次数可动态配置（call-time 生效，而非导入期固化）。"""

import httpx
import openai
import pytest
from tenacity import RetryError, wait_none

from videocaptioner.core.llm import client as client_mod


def _make_rate_limit_error() -> openai.RateLimitError:
    """构造一个带合法 response 的 RateLimitError（供 tenacity 捕获重试）。"""
    req = httpx.Request("POST", "https://example.com/v1/chat/completions")
    resp = httpx.Response(429, request=req)
    return openai.RateLimitError("rate limited", response=resp, body=None)


@pytest.fixture
def fast_retry_env(monkeypatch):
    """剥离所有慢路径：令牌桶/冷却/退避睡眠/429 冷却触发，使测试快速跑完。

    _build_retrying 在调用时读取模块全局 wait_random_exponential / before_sleep_log，
    因此 monkeypatch 模块属性即可让本次测试用 wait_none 且不触发冷却。
    """
    # 令牌桶与冷却：直接放行，不等待
    monkeypatch.setattr(client_mod, "_acquire_token", lambda: None)
    monkeypatch.setattr(client_mod, "_wait_for_cooldown", lambda: None)
    # before_sleep：不再触发 8s 全局冷却
    monkeypatch.setattr(client_mod, "before_sleep_log", lambda retry_state: None)
    # 退避等待：改为不等待，避免测试被 5-60s 退避拖慢
    monkeypatch.setattr(client_mod, "wait_random_exponential", lambda **kw: wait_none())

    # 每次用例结束后清除 override，避免污染其它测试
    yield
    client_mod.set_max_retries(None)


def _run_always_failing_call(counter: list) -> None:
    """跑一次 _call_llm_api，mock 底层 client 始终抛 RateLimitError，统计尝试次数。"""
    error = _make_rate_limit_error()

    class _FakeCompletions:
        def create(self, *args, **kwargs):
            counter.append(1)
            raise error

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    # 覆盖建连，返回始终失败的 mock client
    client_mod._build_llm_client = lambda base_url, api_key: _FakeClient()

    # 默认 reraise=False：重试耗尽后抛 RetryError（与旧版 @retry 装饰器行为一致）
    with pytest.raises(RetryError):
        client_mod._call_llm_api(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            base_url="https://example.com/v1",
            api_key="sk-test",
        )


def test_default_max_retries_is_4(fast_retry_env):
    """默认（无 override、无 UI 配置干预）应回落到 4。"""
    # headless 下读取 UI 配置可能失败，但 _get_max_retries 兜底也是 4；
    # 若读到 cfg 默认 4 则同样为 4。这里只断言 override 清除后的取值。
    client_mod.set_max_retries(None)
    val = client_mod._get_max_retries()
    assert val == 4, f"默认重试次数应为 4，实际 {val}"


def test_retry_count_is_dynamic(fast_retry_env):
    """改变设置后，下一次调用的尝试次数随之变化（证明 call-time 生效）。"""
    # 重试 2 次
    counter: list = []
    client_mod.set_max_retries(2)
    _run_always_failing_call(counter)
    assert len(counter) == 2, f"设置 2 时应尝试 2 次，实际 {len(counter)}"

    # 改为重试 5 次，无需重新导入/重建装饰器
    counter.clear()
    client_mod.set_max_retries(5)
    _run_always_failing_call(counter)
    assert len(counter) == 5, f"设置 5 时应尝试 5 次，实际 {len(counter)}"


def test_retry_count_floor_is_one(fast_retry_env):
    """override 传 <=0 视为不覆盖（回落配置/默认）；这里验证至少尝试 1 次。"""
    counter: list = []
    client_mod.set_max_retries(1)
    _run_always_failing_call(counter)
    assert len(counter) == 1, f"设置 1 时应仅尝试 1 次，实际 {len(counter)}"
