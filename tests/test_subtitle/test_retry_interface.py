"""T05 端到端集成联调测试：手动重试接线（Interface ↔ RetryThread ↔ Model）。

离屏范式（参考 test_subtitle_thread.py）：设置 QT_QPA_PLATFORM=offscreen，
用 FakeRetryThread 替身捕获 spawn 参数、避免真实 LLM/网络调用，
并直接驱动 Interface 的槽方法验证回填/标注/禁用逻辑。
"""

import os
import sys

# 必须在导入 PyQt 之前设置离屏平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication

from videocaptioner.core.entities import ProcessStage, SubtitleFailure
from videocaptioner.ui.view import subtitle_interface as si_module
from videocaptioner.ui.view.subtitle_interface import SubtitleInterface


@pytest.fixture(scope="module")
def qapp():
    """模块级 QApplication（离屏）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def _use_qapp(qapp):
    return qapp


def _seg(start=0, end=1000, orig="a", trans="x",
         failed_stages=None, error_message=""):
    """构造一条 model._data 行（完整 to_json 结构）。"""
    return {
        "start_time": start,
        "end_time": end,
        "original_subtitle": orig,
        "translated_subtitle": trans,
        "failed_stages": list(failed_stages or []),
        "error_message": error_message,
    }


class FakeRetryThread(QThread):
    """RetryThread 替身：捕获构造参数、不真正执行 LLM。

    用 Python 方法遮蔽 C++ isRunning，使 `_is_processing` 可被测试驱动。
    """

    progress = pyqtSignal(int, str)
    finished = pyqtSignal(dict)
    degraded = pyqtSignal(object)
    error = pyqtSignal(str, str)

    last_instance = None
    started_flag = False

    def __init__(self, asr_data, failures, subtitle_config,
                 file_name="", parent=None, custom_prompt="", thread_num=None):
        super().__init__(parent)
        self.asr_data = asr_data
        self.failures = failures
        self.subtitle_config = subtitle_config
        self.file_name = file_name
        self.custom_prompt = custom_prompt
        self.thread_num = thread_num
        self._fake_running = False
        FakeRetryThread.last_instance = self
        FakeRetryThread.started_flag = False

    def isRunning(self):  # 遮蔽 C++ 实现，供测试可控
        return self._fake_running

    def start(self, *args, **kwargs):
        # 记录启动标记，但不真正运行（避免真实 LLM 调用）
        self._fake_running = True
        FakeRetryThread.started_flag = True

    def run(self):
        pass


@pytest.fixture
def iface(qapp):
    """构造离屏 SubtitleInterface 实例。"""
    return SubtitleInterface()


# ---- 场景 1：单条重试 spawn ----
def test_single_retry_spawn(iface, monkeypatch):
    """单条重试：failures={stage:[i]}，thread_num=1（即时串行）。"""
    monkeypatch.setattr(si_module, "RetryThread", FakeRetryThread)
    iface.subtitle_path = "/tmp/demo.srt"
    iface.model._data = {
        "1": _seg(failed_stages=["translate"], error_message="boom"),
        "2": _seg(orig="b", trans="y"),
    }
    iface._on_retry_requested([1], ProcessStage.TRANSLATE)

    fake = FakeRetryThread.last_instance
    assert fake is not None, "RetryThread 未被构造"
    assert fake.failures == {ProcessStage.TRANSLATE: [1]}
    assert fake.thread_num == 1
    assert FakeRetryThread.started_flag, "RetryThread.start() 未被调用"
    # 重试期间 start_button 置灰
    assert not iface.start_button.isEnabled()


# ---- 场景 2：批量「重试全部失败项」spawn ----
def test_batch_retry_all_spawn(iface, monkeypatch):
    """批量重试：按 collect_failures 分类，thread_num=None（内部串行）。"""
    monkeypatch.setattr(si_module, "RetryThread", FakeRetryThread)
    iface.subtitle_path = "/tmp/demo.srt"
    iface.model._data = {
        "1": _seg(failed_stages=["optimize"], error_message="o"),
        "2": _seg(orig="b", trans="y", failed_stages=["translate"], error_message="t"),
        "3": _seg(orig="c", trans="z"),
    }
    iface._on_retry_requested([], None)

    fake = FakeRetryThread.last_instance
    assert fake is not None
    assert fake.failures == {
        ProcessStage.OPTIMIZE: [1],
        ProcessStage.TRANSLATE: [2],
    }
    assert fake.thread_num is None  # 批量交由内部串行


# ---- 场景 3：无失败项时不 spawn ----
def test_no_failures_no_spawn(iface, monkeypatch):
    """没有失败项时提示并不构造线程。"""
    monkeypatch.setattr(si_module, "RetryThread", FakeRetryThread)
    iface.subtitle_path = "/tmp/demo.srt"
    iface.model._data = {"1": _seg(), "2": _seg(orig="b", trans="y")}
    FakeRetryThread.last_instance = None
    FakeRetryThread.started_flag = False

    iface._on_retry_requested([], None)
    assert FakeRetryThread.last_instance is None
    assert not FakeRetryThread.started_flag


# ---- 场景 4：retry_all_failed 经信号触发 spawn ----
def test_retry_all_failed_signal_path(iface, monkeypatch):
    """retry_all_failed() emit 信号 → _on_retry_requested 同步执行并 spawn。"""
    monkeypatch.setattr(si_module, "RetryThread", FakeRetryThread)
    iface.subtitle_path = "/tmp/demo.srt"
    iface.model._data = {"1": _seg(failed_stages=["optimize"], error_message="o")}
    FakeRetryThread.last_instance = None

    iface.retry_all_failed()
    assert FakeRetryThread.last_instance is not None
    assert FakeRetryThread.last_instance.failures == {ProcessStage.OPTIMIZE: [1]}


# ---- 场景 5：重试成功项清除失败标注 ----
def test_retry_finished_clears_marker(iface):
    """整行回填后，成功项 failed_stages 清空、恢复常色。"""
    iface.model._data = {"1": _seg(failed_stages=["translate"], error_message="boom")}
    assert iface.model._is_failed_segment(iface.model._data["1"])

    result = {"1": _seg(trans="ok")}  # 成功：无 failed_stages
    iface._on_retry_finished(result)

    assert iface.model._data["1"]["failed_stages"] == []
    assert not iface.model._is_failed_segment(iface.model._data["1"])
    assert iface.start_button.isEnabled()
    assert iface.progress_bar.value() == 100


# ---- 场景 6：重试仍失败项保留失败标注 ----
def test_retry_finished_keeps_marker(iface):
    """整行回填后，仍失败项保留［失败］标注（不丢状态）。"""
    iface.model._data = {"1": _seg(failed_stages=["translate"], error_message="boom")}

    result = {"1": _seg(trans="ok", failed_stages=["translate"],
                        error_message="still bad")}
    iface._on_retry_finished(result)

    seg = iface.model._data["1"]
    assert iface.model._is_failed_segment(seg)
    assert "translate" in seg["failed_stages"]
    assert seg["error_message"] == "still bad"
    assert iface.start_button.isEnabled()


# ---- 场景 7：主流程降级实时标注 ----
def test_processing_degraded_sets_marker(iface):
    """degraded 信号 → set_failure_markers 写入失败标注。"""
    iface.model._data = {"1": _seg()}
    failure = SubtitleFailure(indices=[1], stage=ProcessStage.OPTIMIZE,
                              message="fail msg")
    iface._on_processing_degraded([failure])

    seg = iface.model._data["1"]
    assert "optimize" in seg["failed_stages"]
    assert seg["error_message"] == "fail msg"
    assert iface.model._is_failed_segment(seg)


# ---- 场景 8：AUTH_QUOTA 禁用重试 ----
def test_fatal_auth_quota_disables_retry(iface):
    """鉴权/欠费致命错误：置禁用标志、隐藏重试按钮、拒绝重试请求。"""
    iface.subtitle_path = "/tmp/demo.srt"
    iface.model._data = {"1": _seg(failed_stages=["translate"], error_message="e")}
    iface.retry_all_button.show()

    iface._on_processing_fatal("403 quota exceeded", "auth_quota")

    assert iface._retry_disabled_by_auth is True
    assert iface.retry_all_button.isHidden()

    # 禁用后再请求重试应被拒绝（不 spawn、不报错）
    from videocaptioner.ui.view import subtitle_interface as si
    orig_retry = si.RetryThread
    try:
        called = {"n": 0}

        def _guard(*a, **k):
            called["n"] += 1
            raise AssertionError("auth_quota 禁用后不应构造 RetryThread")

        si.RetryThread = _guard
        # 应在守卫处 return，不会调用到 RetryThread
        iface._on_retry_requested([1], ProcessStage.TRANSLATE)
        assert called["n"] == 0
    finally:
        si.RetryThread = orig_retry


# ---- 场景 9：_is_processing 覆盖 retry 线程 ----
def test_is_processing_covers_retry_thread(iface):
    """重试线程运行中时 _is_processing 返回 True（防止重试期间再次触发）。"""
    assert iface._is_processing() is False

    iface._retry_thread = FakeRetryThread(None, {}, None)
    iface._retry_thread._fake_running = True
    assert iface._is_processing() is True

    iface._retry_thread._fake_running = False
    assert iface._is_processing() is False
