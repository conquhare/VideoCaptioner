"""翻译器基类"""

import atexit
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import (
    ProcessStage,
    SubtitleFailure,
    SubtitleProcessData,
    SubtitleStatus,
)
from videocaptioner.core.translate.types import TargetLanguage
from videocaptioner.core.utils.cache import generate_cache_key, get_translate_cache
from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("subtitle_translator")


class BaseTranslator(ABC):
    """翻译器基类"""

    def __init__(
        self,
        thread_num: int,
        batch_num: int,
        target_language: TargetLanguage,
        update_callback: Optional[Callable],
        failure_callback: Optional[Callable[[SubtitleFailure], None]] = None,
        checkpoint_path: Optional[str] = None,
        checkpoint_callback: Optional[Callable[[ASRData], None]] = None,
        retry_chunk_size: Optional[int] = None,
    ):
        self.thread_num = thread_num
        self.batch_num = batch_num
        self.target_language = target_language
        self.is_running = True
        self.update_callback = update_callback
        self.failure_callback = failure_callback
        self.checkpoint_path = checkpoint_path
        self.checkpoint_callback = checkpoint_callback
        self.retry_chunk_size = retry_chunk_size
        # 当前正在翻译的 segment 列表（1-based index → segments[i-1]），
        # 供降级点就地标注 process_status；由 translate_subtitle / retry_translate 设置
        self._current_segments: List[ASRDataSeg] = []
        # 两击失败计数：全局 1-based index → 累计失败次数（跨初始 pass 与统一重试持久）
        self._failure_counts: Dict[int, int] = {}
        # 批次编号映射：全局 1-based index → "batch_id_intra_seq"（如 "1_5"），仅用于日志/检查点追踪
        self._index_to_batch_key: Dict[int, str] = {}
        # 工作集：全局 1-based index → 当前已知最优译文（失败项无译文），用于重建检查点
        self._working_translated: Dict[int, str] = {}
        self.executor = None
        self._cache = get_translate_cache()

        self._init_thread_pool()

    def _init_thread_pool(self):
        """初始化线程池"""
        self.executor = ThreadPoolExecutor(max_workers=self.thread_num)
        atexit.register(self.stop)

    def translate_subtitle(self, subtitle_data: ASRData) -> ASRData:
        """翻译字幕文件"""
        try:
            asr_data = subtitle_data

            # 记录当前 segment 列表，供降级点标注失败状态
            self._current_segments = asr_data.segments

            # 重置两击计数与工作集（每次全新 translate pass 从零开始；统一重试不重置）
            self._failure_counts = {}
            self._working_translated = {}

            # 将ASRData转换为SubtitleProcessData列表
            translate_data_list = [
                SubtitleProcessData(index=i, original_text=seg.text)
                for i, seg in enumerate(asr_data.segments, 1)
            ]

            # 分批处理字幕
            chunks = self._split_chunks(translate_data_list)

            # 多线程翻译（大面积失败时 _parallel_translate 抛 RuntimeError，此处优雅捕获，
            # 让失败的 segments 进入下方统一重试而非直接中断）
            try:
                translated_list = self._parallel_translate(chunks)
            except RuntimeError:
                logger.warning("初始翻译大面积失败，尝试统一重试（更小批次）")
                # 抢救已成功的部分译文（_working_translated 在 _parallel_translate 折叠中已保留）
                translated_list = [
                    SubtitleProcessData(
                        index=i,
                        original_text=seg.text,
                        translated_text=self._working_translated.get(i, ""),
                    )
                    for i, seg in enumerate(asr_data.segments, 1)
                    if self._working_translated.get(i)
                ]

            # 设置Subtitle segment的翻译文本（含降级缺译标注）
            self._set_segments_translated_text(asr_data.segments, translated_list)

            # 统一重试（单次，不再递归）：对两击失败的 segments 用更小批次重试一次，
            # 就地更新 asr_data.segments 的译文；retry_translate 内部已优雅处理 RuntimeError
            failed_indices = self.get_failed_indices(min_strikes=1)
            if failed_indices:
                retry_chunk = self.retry_chunk_size or max(1, self.batch_num // 2)
                with self._override_batch_num(retry_chunk):
                    asr_data = self.retry_translate(asr_data, failed_indices)

            return ASRData(asr_data.segments)
        except Exception as e:
            logger.error(f"Translation failed: {str(e)}")
            raise RuntimeError(f"Translation failed: {str(e)}")

    def retry_translate(
        self,
        asr_data: ASRData,
        indices: List[int],
        thread_num: Optional[int] = None,
    ) -> ASRData:
        """只重试指定 index 的翻译；成功项原样保留（幂等）。

        幂等铁律：仅 indices 对应的字幕进入翻译请求，成功项译文不重算/不覆盖。
        失败项因原 chunk 未成功而 cache miss，天然不会误命中成功项缓存。
        成功 → 更新译文 + process_status[TRANSLATE]=SUCCESS；
        仍失败 → 保持 FAILED 并再次通过 failure_callback 上报。

        Args:
            asr_data: 完整字幕数据（就地更新并原样返回）
            indices: 需要重试的字幕 index（1-based）
            thread_num: 可选并发度覆盖（重试建议串行或小并发）

        Returns:
            更新后的 ASRData（同一对象）
        """
        target_indices = sorted({int(i) for i in indices})
        if not target_indices:
            return asr_data

        segments = asr_data.segments
        self._current_segments = segments

        with self._override_thread_num(thread_num):
            # 标记为重试中，用于区分「本次运行失败」与「历史失败」
            for i in target_indices:
                segments[i - 1].process_status[ProcessStage.TRANSLATE] = (
                    SubtitleStatus.RETRYING
                )

            # 构造仅含失败项的子集，成功项不参与翻译请求
            sub_list = [
                SubtitleProcessData(index=i, original_text=segments[i - 1].text)
                for i in target_indices
            ]
            chunks = self._split_chunks(sub_list)

            try:
                translated_list = self._parallel_translate(chunks)
            except RuntimeError:
                # 大面积/全失败：segment 已在降级点标记 FAILED 并上报，优雅返回
                translated_list = []

            trans_map = {d.index: d.translated_text for d in translated_list}

            # 回填：本次运行中仍为 FAILED 的保持失败（已上报），其余视为成功
            for i in target_indices:
                seg = segments[i - 1]
                if seg.process_status.get(ProcessStage.TRANSLATE) == SubtitleStatus.FAILED:
                    continue
                if i in trans_map:
                    seg.translated_text = trans_map[i]
                seg.process_status[ProcessStage.TRANSLATE] = SubtitleStatus.SUCCESS
                seg.error_message = ""

        return asr_data

    def _mark_translate_failed(self, indices: List[int], message: str) -> None:
        """就地标注翻译失败状态并上报（core 层只调回调，不依赖 UI）。

        同时累加两击失败计数，供统一重试与最终占位判定使用。
        """
        for idx in indices:
            # 累加失败次数（跨初始 pass 与统一重试持久，不在此重置）
            self._failure_counts[idx] = self._failure_counts.get(idx, 0) + 1
            if 1 <= idx <= len(self._current_segments):
                seg = self._current_segments[idx - 1]
                seg.process_status[ProcessStage.TRANSLATE] = SubtitleStatus.FAILED
                seg.error_message = message
        if self.failure_callback:
            self.failure_callback(
                SubtitleFailure(
                    indices=indices, stage=ProcessStage.TRANSLATE, message=message
                )
            )

    def get_failed_indices(self, min_strikes: int = 2) -> List[int]:
        """返回累计失败次数 ≥ min_strikes 的全局 1-based index（升序）。

        Args:
            min_strikes: 失败次数阈值。默认 2（初始 pass + 统一重试均失败 = 永久占位）；
                         传 1 可取「至少失败过一次」的集合（用于触发统一重试）。
        """
        return sorted(i for i, c in self._failure_counts.items() if c >= min_strikes)

    def _build_current_asr(self) -> ASRData:
        """根据 _current_segments + 当前工作集重建一份 in-progress ASRData（供检查点落盘）。

        已知译文填入 translated_text，未知（失败）项保留空译文；输出仍为常规顺序时间戳，
        batch_intra 仅作内部追踪，不体现在此 ASRData 的编号上。
        """
        segments = [
            ASRDataSeg(
                text=seg.text,
                start_time=seg.start_time,
                end_time=seg.end_time,
                translated_text=self._working_translated.get(i, seg.translated_text),
            )
            for i, seg in enumerate(self._current_segments, 1)
        ]
        return ASRData(segments)

    def _invoke_checkpoint(self) -> None:
        """尽力写检查点：IO 失败仅告警，绝不中断主流程。"""
        if not self.checkpoint_callback:
            return
        try:
            self.checkpoint_callback(self._build_current_asr())
        except Exception as e:  # 检查点 IO 失败不应影响主流程
            logger.warning(f"写入检查点失败（已忽略）: {str(e)}")

    @contextmanager
    def _override_thread_num(self, thread_num: Optional[int]) -> Iterator[None]:
        """临时覆盖并发度（重试专用），退出时恢复原并发度并重建线程池。"""
        if not thread_num or thread_num == self.thread_num:
            yield
            return

        original = self.thread_num
        self.thread_num = thread_num
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
        self.executor = ThreadPoolExecutor(max_workers=thread_num)
        try:
            yield
        finally:
            self.thread_num = original
            if self.executor:
                self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = ThreadPoolExecutor(max_workers=original)

    @contextmanager
    def _override_batch_num(self, batch_num: Optional[int]) -> Iterator[None]:
        """临时覆盖批次大小（统一重试专用，用更小批次提高成功率），退出时恢复。"""
        if not batch_num or batch_num == self.batch_num:
            yield
            return

        original = self.batch_num
        self.batch_num = batch_num
        try:
            yield
        finally:
            self.batch_num = original

    def _split_chunks(
        self, translate_data_list: List[SubtitleProcessData]
    ) -> List[List[SubtitleProcessData]]:
        """将字幕分割成块"""
        return [
            translate_data_list[i : i + self.batch_num]
            for i in range(0, len(translate_data_list), self.batch_num)
        ]

    def _parallel_translate(
        self, chunks: List[List[SubtitleProcessData]]
    ) -> List[SubtitleProcessData]:
        """并行翻译All块"""
        # 构建批次编号映射（1-based batch_id + 1-based intra_seq），仅用于日志/检查点追踪
        self._index_to_batch_key = {}
        for batch_id, chunk in enumerate(chunks, 1):
            for intra_seq, data in enumerate(chunk, 1):
                self._index_to_batch_key[data.index] = f"{batch_id}_{intra_seq}"

        future_to_chunk = {}
        translated_list = []
        failed_count = 0
        total_segments = sum(len(c) for c in chunks)

        for chunk in chunks:
            future = self.executor.submit(self._safe_translate_chunk, chunk)
            future_to_chunk[future] = chunk

        for future in as_completed(future_to_chunk):
            if not self.is_running:
                break
            chunk = future_to_chunk[future]
            try:
                result = future.result()
                translated_list.extend(result)
                # 折叠到工作集（仅记录有译文的项），随后尽力写检查点
                for d in result:
                    if d.translated_text:
                        self._working_translated[d.index] = d.translated_text
            except Exception as e:
                logger.error(f"Translation chunk failed: {e}")
                failed_count += len(chunk)
                # 降级：对该 chunk 所有 index 标注失败并上报，仍保留原文
                batch_keys = [
                    self._index_to_batch_key.get(d.index, str(d.index))
                    for d in chunk
                ]
                message = f"{e} (batch {batch_keys[0]}..{batch_keys[-1]})"
                logger.warning(
                    f"翻译批次失败 (batch {batch_keys[0]}..{batch_keys[-1]}): "
                    f"{[d.index for d in chunk]}"
                )
                self._mark_translate_failed([d.index for d in chunk], message)
                translated_list.extend(chunk)

            # 每个批次完成（无论成功或优雅失败）后尽力写检查点
            self._invoke_checkpoint()

        # Raise if all or most translations failed
        if failed_count > 0 and total_segments > 0:
            fail_rate = failed_count / total_segments
            if fail_rate >= 0.5:
                raise RuntimeError(
                    f"Translation failed: {failed_count}/{total_segments} segments failed "
                    f"({fail_rate:.0%}). Check your API key and network connection."
                )
            elif failed_count > 0:
                logger.warning(f"Translation partially failed: {failed_count}/{total_segments} segments")

        return translated_list

    def _get_cache_key(self, chunk: List[SubtitleProcessData]) -> str:
        """生成缓存键"""
        class_name = self.__class__.__name__
        chunk_key = generate_cache_key(chunk)
        lang = self.target_language.value
        return f"{class_name}:{chunk_key}:{lang}"

    def _safe_translate_chunk(
        self, chunk: List[SubtitleProcessData]
    ) -> List[SubtitleProcessData]:
        """安全的翻译块"""
        try:
            cache_key = self._get_cache_key(chunk)
            try:
                cached_result = self._cache.get(cache_key, default=None)
            except Exception:
                # Graceful degradation: corrupted cache (e.g. old pickle from app→videocaptioner rename)
                cached_result = None
                self._cache.delete(cache_key)
            if cached_result is not None:
                return cached_result

            result = self._translate_chunk(chunk)

            if self.update_callback:
                self.update_callback(result)

            self._cache.set(cache_key, result, expire=86400 * 7)
            return result

        except Exception as e:
            logger.exception(f"Translation failed: {str(e)}")
            raise

    def _set_segments_translated_text(
        self, original_segments: List[ASRDataSeg], translated_list: List[SubtitleProcessData]
    ) -> List[ASRDataSeg]:
        """设置Subtitle segment的翻译文本"""
        # 创建索引到翻译文本的映射
        translation_map = {data.index: data.translated_text for data in translated_list}

        for i, seg in enumerate(original_segments, 1):
            if i not in translation_map:
                logger.error(f"Subtitle segment {i} has no translation")
                # 降级：标注失败并上报（缺译文本，保留原文）
                seg.process_status[ProcessStage.TRANSLATE] = SubtitleStatus.FAILED
                seg.error_message = "no translation returned"
                if self.failure_callback:
                    self.failure_callback(
                        SubtitleFailure(
                            indices=[i],
                            stage=ProcessStage.TRANSLATE,
                            message="no translation returned",
                        )
                    )
                continue
            seg.translated_text = translation_map[i]

        return original_segments

    @abstractmethod
    def _translate_chunk(
        self, subtitle_chunk: List[SubtitleProcessData]
    ) -> List[SubtitleProcessData]:
        """翻译字幕块"""
        pass

    def stop(self):
        """停止翻译器"""
        if not self.is_running:
            return

        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"Error closing thread pool: {str(e)}")
            finally:
                self.executor = None
