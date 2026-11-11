"""字幕优化模块

使用LLM优化字幕内容，支持agent loop自动验证和修正。
"""

import atexit
import difflib
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

import json_repair

from ..asr.asr_data import ASRData, ASRDataSeg
from ..entities import (
    ProcessStage,
    SubtitleFailure,
    SubtitleProcessData,
    SubtitleStatus,
)
from ..llm import call_llm
from ..prompts import get_prompt
from ..split.alignment import SubtitleAligner
from ..utils.logger import setup_logger
from ..utils.text_utils import count_words

logger = setup_logger("subtitle_optimizer")

MAX_STEPS = 3

# 极短碎片词判定阈值：整批字幕词数都 ≤ 此值时跳过相似度校验（避免填充词删除触发无谓反馈循环）
_SHORT_FRAGMENT_MAX_WORDS = 3


class SubtitleOptimizer:
    """字幕优化器

    使用LLM优化字幕内容，支持:
    - Agent loop自动验证和修正
    - 并发批量处理
    - 自动对齐修复
    """

    def __init__(
        self,
        thread_num: int,
        batch_num: int,
        model: str,
        custom_prompt: str,
        update_callback: Optional[Callable] = None,
        failure_callback: Optional[Callable[[SubtitleFailure], None]] = None,
        base_url: str = "",
        api_key: str = "",
        reasoning_effort: str = "none",
        checkpoint_path: Optional[str] = None,
        checkpoint_callback: Optional[Callable[[ASRData], None]] = None,
        retry_chunk_size: Optional[int] = None,
    ):
        """初始化优化器

        Args:
            thread_num: 并发线程数
            batch_num: 每批处理的字幕数量
            model: LLM模型名称
            custom_prompt: 自定义优化提示词
            temperature: LLM温度参数
            update_callback: 进度更新回调函数
            failure_callback: 降级失败上报回调（与 update_callback 对称），为 None 时静默
            base_url: LLM API 基础 URL（显式传参，取代旧的进程级 env 方案）
            api_key: LLM API 密钥
            checkpoint_path: 检查点文件路径（可选，仅供记录，实际落盘由 checkpoint_callback 负责）
            checkpoint_callback: 每批完成后回调，接收当前 in-progress ASRData 并落盘（增量检查点）
            retry_chunk_size: 统一重试时的批次大小；None 时取 max(1, batch_num // 2)
        """
        self.thread_num = thread_num
        self.batch_num = batch_num
        self.model = model
        self.custom_prompt = custom_prompt
        self.base_url = base_url
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        self.update_callback = update_callback
        self.failure_callback = failure_callback
        self.checkpoint_path = checkpoint_path
        self.checkpoint_callback = checkpoint_callback
        self.retry_chunk_size = retry_chunk_size
        # 当前正在优化的 segment 列表（1-based index → segments[i-1]），
        # 供降级点就地标注 process_status；由 optimize_subtitle / retry_optimize 设置
        self._current_segments: List[ASRDataSeg] = []
        # 两击失败计数：全局 1-based index → 累计失败次数（跨初始 pass 与统一重试持久）
        self._failure_counts: Dict[int, int] = {}
        # 批次编号映射：全局 1-based index → "batch_id_intra_seq"（如 "1_5"），仅用于日志/检查点追踪
        self._index_to_batch_key: Dict[int, str] = {}
        # 工作集：全局 1-based index → 当前已知最优优化文本（失败项保留原文），用于重建检查点
        self._working_optimized: Dict[int, str] = {}

        self.is_running = True
        self.executor: Optional[ThreadPoolExecutor] = None
        self._init_thread_pool()

    def _init_thread_pool(self) -> None:
        """初始化线程池并注册清理函数"""
        self.executor = ThreadPoolExecutor(max_workers=self.thread_num)
        atexit.register(self.stop)

    def optimize_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """优化字幕

        Args:
            subtitle_data: 字幕文件路径或ASRData对象

        Returns:
            优化后的ASRData对象
        """
        try:
            # Reading字幕
            if isinstance(subtitle_data, str):
                asr_data = ASRData.from_subtitle_file(subtitle_data)
            else:
                asr_data = subtitle_data

            # 记录当前 segment 列表，供降级点标注失败状态
            self._current_segments = asr_data.segments

            # 重置两击计数与工作集（每次全新 optimize pass 从零开始；统一重试不重置）
            self._failure_counts = {}
            self._working_optimized = {}

            # 转换为字典格式
            subtitle_dict = {
                str(i): seg.text for i, seg in enumerate(asr_data.segments, 1)
            }

            # 分批处理
            chunks = self._split_chunks(subtitle_dict)

            # 并行优化
            optimized_dict = self._parallel_optimize(chunks)

            # 折叠到工作集，供检查点重建
            for k, v in optimized_dict.items():
                self._working_optimized[int(k)] = v

            # 用初始 pass 的结果构建新的 segment 列表，供统一重试就地更新
            asr_data = ASRData(self._create_segments(asr_data.segments, optimized_dict))

            # 统一重试（单次，不再递归）：对初始 pass 失败的 segments 用更小批次重试一次
            failed_indices = self.get_failed_indices(min_strikes=1)
            if failed_indices:
                retry_chunk = self.retry_chunk_size or max(1, self.batch_num // 2)
                with self._override_batch_num(retry_chunk):
                    asr_data = self.retry_optimize(asr_data, failed_indices)

            return asr_data

        except Exception as e:
            logger.error(f"Optimization failed: {str(e)}")
            raise RuntimeError(f"Optimization failed: {str(e)}")

    def retry_optimize(
        self,
        asr_data: ASRData,
        indices: List[int],
        thread_num: Optional[int] = None,
    ) -> ASRData:
        """只重试指定 index 的优化；成功项原样保留（幂等）。

        幂等铁律：仅 indices 对应的字幕进入 LLM 请求，成功项文本不重算/不覆盖。
        成功 → 更新文本 + process_status[OPTIMIZE]=SUCCESS；
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
                segments[i - 1].process_status[ProcessStage.OPTIMIZE] = (
                    SubtitleStatus.RETRYING
                )

            # 构造仅含失败项的子集（键为原 1-based index），成功项不参与
            sub_dict = {str(i): segments[i - 1].text for i in target_indices}
            chunks = self._split_chunks(sub_dict)
            optimized_dict = self._parallel_optimize(chunks)

            # 回填：本次运行中仍为 FAILED 的保持失败（已在降级点上报），其余视为成功
            for i in target_indices:
                seg = segments[i - 1]
                seg.text = optimized_dict.get(str(i), seg.text)
                if seg.process_status.get(ProcessStage.OPTIMIZE) == SubtitleStatus.FAILED:
                    continue
                seg.process_status[ProcessStage.OPTIMIZE] = SubtitleStatus.SUCCESS
                seg.error_message = ""

        return asr_data

    def _mark_optimize_failed(self, indices: List[int], message: str) -> None:
        """就地标注优化失败状态并上报（core 层只调回调，不依赖 UI）。

        同时累加两击失败计数，供统一重试与最终占位判定使用。
        """
        for idx in indices:
            # 累加失败次数（跨初始 pass 与统一重试持久，不在此重置）
            self._failure_counts[idx] = self._failure_counts.get(idx, 0) + 1
            if 1 <= idx <= len(self._current_segments):
                seg = self._current_segments[idx - 1]
                seg.process_status[ProcessStage.OPTIMIZE] = SubtitleStatus.FAILED
                seg.error_message = message
        if self.failure_callback:
            self.failure_callback(
                SubtitleFailure(
                    indices=indices, stage=ProcessStage.OPTIMIZE, message=message
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

        失败项保留原文（工作集里失败项已是原文），输出仍为常规顺序时间戳，
        batch_intra 仅作内部追踪，不体现在此 ASRData 的编号上。
        """
        segments = [
            ASRDataSeg(
                text=self._working_optimized.get(i, seg.text),
                start_time=seg.start_time,
                end_time=seg.end_time,
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
        """临时覆盖批次大小（统一重试专用，用更小批次提高 LLM 成功率），退出时恢复。"""
        if not batch_num or batch_num == self.batch_num:
            yield
            return

        original = self.batch_num
        self.batch_num = batch_num
        try:
            yield
        finally:
            self.batch_num = original

    def _split_chunks(self, subtitle_dict: Dict[str, str]) -> List[Dict[str, str]]:
        """将字幕字典分割成批次

        Args:
            subtitle_dict: 字幕字典 {index: text}

        Returns:
            批次列表
        """
        items = list(subtitle_dict.items())
        return [
            dict(items[i : i + self.batch_num])
            for i in range(0, len(items), self.batch_num)
        ]

    def _parallel_optimize(self, chunks: List[Dict[str, str]]) -> Dict[str, str]:
        """并行优化All批次

        Args:
            chunks: 字幕批次列表

        Returns:
            优化后的字幕字典
        """
        if not self.executor:
            raise ValueError("Thread pool not initialized")

        # 构建批次编号映射（1-based batch_id + 1-based intra_seq），仅用于日志/检查点追踪
        self._index_to_batch_key = {}
        for batch_id, chunk in enumerate(chunks, 1):
            for intra_seq, key in enumerate(chunk.keys(), 1):
                self._index_to_batch_key[int(key)] = f"{batch_id}_{intra_seq}"

        futures = []
        optimized_dict: Dict[str, str] = {}

        # 提交All任务
        for chunk in chunks:
            future = self.executor.submit(self._optimize_chunk, chunk)
            futures.append((future, chunk))

        # 收集结果
        for future, chunk in futures:
            if not self.is_running:
                break

            try:
                result = future.result()
                optimized_dict.update(result)
                # 折叠到工作集（成功项为优化文本，优雅失败项为原文），随后尽力写检查点
                for k, v in result.items():
                    self._working_optimized[int(k)] = v
            except Exception as e:
                logger.error(f"Optimization batch failed: {str(e)}")
                # 降级：对该批所有 index 标注失败并上报，仍保留原文继续
                failed_indices = [int(k) for k in chunk]
                batch_keys = [
                    self._index_to_batch_key.get(i, str(i)) for i in failed_indices
                ]
                message = f"{e} (batch {batch_keys[0]}..{batch_keys[-1]})"
                logger.warning(
                    f"优化批次失败 (batch {batch_keys[0]}..{batch_keys[-1]}): "
                    f"{failed_indices}"
                )
                self._mark_optimize_failed(failed_indices, message)
                optimized_dict.update(chunk)  # 失败时保留原文
                for k, v in chunk.items():
                    self._working_optimized[int(k)] = v

            # 每个批次完成（无论成功或优雅失败）后尽力写检查点
            self._invoke_checkpoint()

        return optimized_dict

    def _optimize_chunk(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """优化单个字幕批次

        Args:
            subtitle_chunk: 字幕批次字典

        Returns:
            优化后的字幕批次
        """
        start_idx = next(iter(subtitle_chunk))
        end_idx = next(reversed(subtitle_chunk))
        logger.debug(f"[+]Optimizing subtitles: {start_idx} - {end_idx}")

        try:
            result = self.agent_loop(subtitle_chunk)

            if self.update_callback:
                callback_data = [
                    SubtitleProcessData(
                        index=int(idx),
                        original_text=subtitle_chunk[idx],
                        optimized_text=result[idx],
                    )
                    for idx in sorted(result.keys(), key=int)
                ]
                self.update_callback(callback_data)

            return result

        except Exception as e:
            logger.error(f"Optimization failed: {str(e)}")
            # 降级：标注该批所有 index 失败并上报，返回原文继续
            failed_indices = [int(k) for k in subtitle_chunk]
            batch_keys = [
                self._index_to_batch_key.get(i, str(i)) for i in failed_indices
            ]
            message = f"{e} (batch {batch_keys[0]}..{batch_keys[-1]})"
            logger.warning(
                f"优化批次失败 (batch {batch_keys[0]}..{batch_keys[-1]}): {failed_indices}"
            )
            self._mark_optimize_failed(failed_indices, message)
            return subtitle_chunk

    def agent_loop(self, subtitle_chunk: Dict[str, str]) -> Dict[str, str]:
        """使用agent loop优化字幕

        LLM → 验证 → 反馈 → 重试 (最多MAX_STEPS次)

        Args:
            subtitle_chunk: 字幕批次字典

        Returns:
            优化后的字幕批次

        Raises:
            ValueError: LLM returned empty result
        """
        # 构建提示词
        user_prompt = (
            f"Correct the following subtitles. Keep the original language, do not translate:\n"
            f"<input_subtitle>{str(subtitle_chunk)}</input_subtitle>"
        )

        if self.custom_prompt:
            user_prompt += (
                f"\nReference content:\n<reference>{self.custom_prompt}</reference>"
            )

        messages = [
            {"role": "system", "content": get_prompt("optimize/subtitle")},
            {"role": "user", "content": user_prompt},
        ]

        last_result = None

        # Agent loop
        for step in range(MAX_STEPS):
            # 调用LLM
            response = call_llm(
                messages=messages,
                model=self.model,
                temperature=0.2,
                base_url=self.base_url,
                api_key=self.api_key,
                reasoning_effort=self.reasoning_effort,
            )

            result_text = response.choices[0].message.content
            if not result_text:
                raise ValueError("LLM returned empty result")

            # 解析结果
            parsed_result = json_repair.loads(result_text)
            if not isinstance(parsed_result, dict):
                raise ValueError(
                    f"LLM返回结果类型Error，期望dict，实际{type(parsed_result)}"
                )

            result_dict: Dict[str, str] = parsed_result
            last_result = result_dict

            # 验证结果
            is_valid, error_message = self._validate_optimization_result(
                original_chunk=subtitle_chunk, optimized_chunk=result_dict
            )

            if is_valid:
                return self._repair_subtitle(subtitle_chunk, result_dict)

            # 验证失败，添加反馈
            logger.warning(
                f"优化验证失败，开始反馈循环 (第{step + 1}次尝试): {error_message}"
            )
            messages.append({"role": "assistant", "content": result_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Validation failed: {error_message}\n"
                        f"Please fix the errors and output ONLY a valid JSON dictionary."
                    ),
                }
            )

        # 达到最大步数
        logger.warning(f"Max attempts reached({MAX_STEPS})，returning last result")
        return (
            self._repair_subtitle(subtitle_chunk, last_result)
            if last_result
            else subtitle_chunk
        )

    def _validate_optimization_result(
        self, original_chunk: Dict[str, str], optimized_chunk: Dict[str, str]
    ) -> Tuple[bool, str]:
        """验证优化结果

        检查:
        1. 键是否完全匹配
        2. 改动是否过大（相似度 < 0.7）

        Args:
            original_chunk: 原始字幕批次
            optimized_chunk: 优化后字幕批次

        Returns:
            (是否有效, Error反馈)
        """
        expected_keys = set(original_chunk.keys())
        actual_keys = set(optimized_chunk.keys())

        # 检查键匹配
        if expected_keys != actual_keys:
            missing = expected_keys - actual_keys
            extra = actual_keys - expected_keys

            error_parts = []
            if missing:
                error_parts.append(f"Missing keys: {sorted(missing)}")
            if extra:
                error_parts.append(f"Extra keys: {sorted(extra)}")

            error_msg = (
                "\n".join(error_parts) + f"\nRequired keys: {sorted(expected_keys)}\n"
                f"Please return the COMPLETE optimized dictionary with ALL {len(expected_keys)} keys."
            )
            return False, error_msg

        # 检查改动是否过大（逐条比较相似度）
        # 极短碎片词（≤3词）跳过相似度校验：它们的"优化"无非是删填充词/首字母大写，
        # 与系统提示词要求一致，difflib 相似度对空串/大写极敏感（'Hmm.'→'' 算 0%），
        # 重试只会逼模型把填充词加回去，浪费请求额度并加剧限流。直接采纳首次结果。
        if all(count_words(v) <= _SHORT_FRAGMENT_MAX_WORDS for v in original_chunk.values()):
            return True, ""

        excessive_changes = []
        for key in expected_keys:
            original_text = original_chunk[key]
            optimized_text = optimized_chunk[key]

            # 清理文本用于比较
            original_cleaned = re.sub(r"\s+", " ", original_text).strip()
            optimized_cleaned = re.sub(r"\s+", " ", optimized_text).strip()

            # 计算相似度
            matcher = difflib.SequenceMatcher(None, original_cleaned, optimized_cleaned)
            similarity = matcher.ratio()
            similarity_threshold = 0.3 if count_words(original_text) <= 10 else 0.7

            # 相似度过低
            if similarity < similarity_threshold:
                excessive_changes.append(
                    f"Key '{key}': similarity {similarity:.1%} < {similarity_threshold:.0%}. "
                    f"Original: '{original_text}' → Optimized: '{optimized_text}' "
                )

        if excessive_changes:
            error_msg = ";\n".join(excessive_changes)
            error_msg += (
                "\n\nYour optimizations changed the text too much. "
                "Keep high similarity (≥70% for normal text) by making MINIMAL changes: "
                "only fix recognition errors and improve clarity, "
                "but preserve the original wording, length and structure as much as possible."
            )
            return False, error_msg

        return True, ""

    @staticmethod
    def _repair_subtitle(
        original: Dict[str, str], optimized: Dict[str, str]
    ) -> Dict[str, str]:
        """修复字幕对齐

        使用SubtitleAligner对齐原文和优化后的文本，
        处理优化过程中可能产生的段落合并或拆分。

        Args:
            original: 原始字幕字典
            optimized: 优化后字幕字典

        Returns:
            对齐后的字幕字典
        """
        try:
            aligner = SubtitleAligner()
            original_list = list(original.values())
            optimized_list = list(optimized.values())

            aligned_source, aligned_target = aligner.align_texts(
                original_list, optimized_list
            )

            if len(aligned_source) != len(aligned_target):
                logger.warning("Alignment length mismatch，returning original")
                return optimized

            # 重建字典，保持原有索引
            start_id = next(iter(original.keys()))
            return {
                str(int(start_id) + i): text for i, text in enumerate(aligned_target)
            }

        except Exception as e:
            logger.error(f"Alignment failed: {str(e)}，returning original")
            return optimized

    @staticmethod
    def _create_segments(
        original_segments: List[ASRDataSeg],
        optimized_dict: Dict[str, str],
    ) -> List[ASRDataSeg]:
        """从优化字典创建新的ASRDataSeg列表

        Args:
            original_segments: 原始Subtitle segment列表
            optimized_dict: 优化后字幕字典

        Returns:
            新的Subtitle segment列表
        """
        return [
            ASRDataSeg(
                text=optimized_dict.get(str(i), seg.text),
                start_time=seg.start_time,
                end_time=seg.end_time,
            )
            for i, seg in enumerate(original_segments, 1)
        ]

    def stop(self) -> None:
        """停止优化器并清理资源"""
        if not self.is_running:
            return

        self.is_running = False

        if self.executor:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            finally:
                self.executor = None
