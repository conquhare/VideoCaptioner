# -*- coding: utf-8 -*-
"""手动重试线程：按阶段幂等重试失败项（优化/翻译）。

镜像 `RetranslateThread` 范式，但只把「失败 index 子集」喂给 LLM，
成功项文本原样保留、不参与重发（幂等铁律，见 arch_failure_retry §4）。

thread→UI 全走信号；本类不直接操作 model/表格，也不把 PyQt 对象传入 core。
"""
import os
import random
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.entities import (
    ProcessStage,
    SubtitleConfig,
    SubtitleFailure,
    SubtitleProcessData,
    TranslatorServiceEnum,
)
from videocaptioner.core.llm.context import (
    clear_task_context,
    generate_task_id,
    set_task_context,
)
from videocaptioner.core.optimize.optimize import SubtitleOptimizer
from videocaptioner.core.utils.error_classifier import classify_error
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.thread.subtitle_thread import create_translator_from_config

logger = setup_logger("retry_thread")

# 「重试全部失败项」批间退避区间（秒），见 arch §4.5/Q2
_BACKOFF_RANGE = (0.3, 0.5)


class RetryThread(QThread):
    """按阶段分派幂等重试的工作线程。"""

    # (百分比, 状态描述)
    progress = pyqtSignal(int, str)
    # 回填结果：{str(index): to_json 行}，供 model.update_data 精确更新重试项
    finished = pyqtSignal(dict)
    # 仍失败的项（List[SubtitleFailure]），供 UI 重新标注失败标记
    degraded = pyqtSignal(object)
    # 致命失败 (error_text, FatalErrorType.value)；AUTH_QUOTA 时 UI 应禁用重试
    error = pyqtSignal(str, str)

    def __init__(
        self,
        asr_data: ASRData,
        failures: Dict[ProcessStage, List[int]],
        subtitle_config: SubtitleConfig,
        file_name: str = "",
        parent=None,
        custom_prompt: str = "",
        thread_num: Optional[int] = None,
    ):
        """
        Args:
            asr_data: 完整字幕数据（retry_* 就地更新并原样返回；成功项不重算）。
            failures: 各阶段待重试的 1-based index 集合，仅含 OPTIMIZE/TRANSLATE。
            subtitle_config: 当前任务配置（含 LLM/翻译服务参数）。
            file_name: 用于日志上下文的文件名。
            parent: Qt 父对象。
            custom_prompt: 优化重试使用的自定义提示词（与主流程一致）。
            thread_num: 并发度覆盖；None 时「重试全部失败项」强制串行(=1)，
                单条重试由调用方传 1 即时执行。
        """
        super().__init__(parent)
        self.asr_data = asr_data
        self.failures = {
            stage: sorted({int(i) for i in indices})
            for stage, indices in failures.items()
            if indices
        }
        self.subtitle_config = subtitle_config
        self.file_name = file_name
        self.custom_prompt = custom_prompt
        # 重试默认串行（thread_num=1），规避大面积失败时的并发风暴
        self.thread_num = thread_num if thread_num is not None else 1

        self.total = sum(len(v) for v in self.failures.values())
        self.done = 0

    # ---- 内部回调 ----
    def _progress_callback(self, result: List[SubtitleProcessData]) -> None:
        """update_callback：累计完成数并发射进度。"""
        self.done += len(result)
        pct = min(int(self.done / max(self.total, 1) * 100), 100)
        self.progress.emit(pct, self.tr("{0}% 重试中").format(pct))

    def _on_failure(self, failure: SubtitleFailure) -> None:
        """failure_callback：仍失败的项重新标注（实时逐条发射）。"""
        self.degraded.emit([failure])

    def _backoff(self) -> None:
        """批间退避 300-500ms（仅多阶段串行时用于降速，避免连续打 LLM）。"""
        time.sleep(random.uniform(*_BACKOFF_RANGE))

    # ---- 阶段分派 ----
    def _retry_optimize(self, indices: List[int]) -> None:
        config = self.subtitle_config
        if not config.llm_model:
            raise Exception("LLM 模型未配置")
        optimizer = SubtitleOptimizer(
            thread_num=self.thread_num,
            batch_num=config.batch_size,
            model=config.llm_model,
            custom_prompt=self.custom_prompt or "",
            update_callback=self._progress_callback,
            failure_callback=self._on_failure,
            base_url=config.base_url or "",
            api_key=config.api_key or "",
        )
        self.asr_data = optimizer.retry_optimize(
            self.asr_data, indices, thread_num=self.thread_num
        )

    def _retry_translate(self, indices: List[int]) -> None:
        translator = create_translator_from_config(
            self.subtitle_config, callback=self._progress_callback
        )
        translator.failure_callback = self._on_failure
        self.asr_data = translator.retry_translate(
            self.asr_data, indices, thread_num=self.thread_num
        )

    # ---- 主流程 ----
    def run(self):
        set_task_context(
            task_id=generate_task_id(),
            file_name=self.file_name,
            stage="retry",
        )
        # 记录参与重试的全部 index，用于结束时精确回填
        retried_indices: List[int] = []

        try:
            self._validate_llm_config()

            # 稳定顺序：先 OPTIMIZE 再 TRANSLATE（优化产出是翻译输入）
            ordered_stages = [
                stage
                for stage in (ProcessStage.OPTIMIZE, ProcessStage.TRANSLATE)
                if stage in self.failures
            ]

            for position, stage in enumerate(ordered_stages):
                indices = self.failures[stage]
                retried_indices.extend(indices)
                if position > 0:
                    self._backoff()
                if stage == ProcessStage.OPTIMIZE:
                    self._retry_optimize(indices)
                else:
                    self._retry_translate(indices)

            # 构造回填结果：{str(index): to_json 行}，仅含重试项
            result = self._build_result(retried_indices)
            self.progress.emit(100, self.tr("重试完成"))
            self.finished.emit(result)

        except Exception as e:
            logger.exception(f"重试失败: {e}")
            fatal_type = classify_error(e)
            self.error.emit(str(e), fatal_type.value)
        finally:
            clear_task_context()

    def _validate_llm_config(self) -> None:
        """校验 LLM 优化/翻译重试所需的凭据是否齐全（凭据经显式传参流向 call_llm）。"""
        config = self.subtitle_config
        needs_llm = (
            ProcessStage.OPTIMIZE in self.failures
            or (
                ProcessStage.TRANSLATE in self.failures
                and config.translator_service == TranslatorServiceEnum.OPENAI
            )
        )
        if not needs_llm:
            return
        if not (config.base_url and config.api_key and config.llm_model):
            missing = [
                name
                for name, value in (
                    ("API Base", config.base_url),
                    ("API Key", config.api_key),
                    ("Model", config.llm_model),
                )
                if not value
            ]
            raise Exception(
                self.tr("LLM 配置不完整，缺少：{0}，请到「设置 → LLM」补全").format(
                    "、".join(missing)
                )
            )

    def _build_result(self, indices: List[int]) -> dict:
        """抽取重试 index 对应的 to_json 行，供 model.update_data 回填。"""
        full_json = self.asr_data.to_json()
        return {str(i): full_json[str(i)] for i in indices if str(i) in full_json}
