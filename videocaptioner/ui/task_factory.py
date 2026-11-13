import datetime
import re
from pathlib import Path
from typing import Optional

from videocaptioner.config import MODEL_PATH
from videocaptioner.core.llm.client import normalize_base_url
from videocaptioner.core.entities import (
    LANGUAGES,
    FullProcessTask,
    LLMServiceEnum,
    SubtitleConfig,
    SubtitleTask,
    SynthesisConfig,
    SynthesisTask,
    TranscribeConfig,
    TranscribeTask,
    TranscriptAndSubtitleTask,
)
from videocaptioner.ui.common.config import cfg


class TaskFactory:
    """任务工厂：把 UI 配置(cfg) 转换成 core 层任务对象。"""

    @staticmethod
    def get_current_llm_triplet() -> tuple:
        """按「当前实时 cfg.llm_service」取对应 LLM 三元组 (base_url, api_key, llm_model)。

        供两处共用，确保「测试连接」与「实际执行」读到同一份实时配置，
        消除 worker 依赖「建 task 时冻结快照」导致的配置过期/误报未配置问题。
        """
        current_service = cfg.llm_service.value
        llm_cfg = {
            LLMServiceEnum.OPENAI: (cfg.openai_api_base, cfg.openai_api_key, cfg.openai_model),
            LLMServiceEnum.SILICON_CLOUD: (cfg.silicon_cloud_api_base, cfg.silicon_cloud_api_key, cfg.silicon_cloud_model),
            LLMServiceEnum.DEEPSEEK: (cfg.deepseek_api_base, cfg.deepseek_api_key, cfg.deepseek_model),
            LLMServiceEnum.OLLAMA: (cfg.ollama_api_base, cfg.ollama_api_key, cfg.ollama_model),
            LLMServiceEnum.LM_STUDIO: (cfg.lm_studio_api_base, cfg.lm_studio_api_key, cfg.lm_studio_model),
            LLMServiceEnum.GEMINI: (cfg.gemini_api_base, cfg.gemini_api_key, cfg.gemini_model),
            LLMServiceEnum.CHATGLM: (cfg.chatglm_api_base, cfg.chatglm_api_key, cfg.chatglm_model),
        }
        base_cfg, key_cfg, model_cfg = llm_cfg.get(current_service, (None, None, None))
        base_url = base_cfg.value if base_cfg else ""
        if base_url:
            base_url = normalize_base_url(base_url)
        api_key = key_cfg.value if key_cfg else ""
        llm_model = model_cfg.value if model_cfg else ""
        return base_url, api_key, llm_model

    """任务工厂类，用于创建各种类型的任务"""

    @staticmethod
    def get_ass_style(style_name: str) -> str:
        """获取 ASS 字幕样式内容 (via style_manager, JSON-first with .txt fallback)"""
        from videocaptioner.core.subtitle.style_manager import load_style

        style = load_style(style_name)
        if style is not None:
            return style.to_ass_string()
        return ""

    @staticmethod
    def get_rounded_style() -> dict:
        """获取圆角背景样式配置 (from UI cfg overrides)"""
        return {
            "font_name": cfg.rounded_bg_font_name.value,
            "font_size": cfg.rounded_bg_font_size.value,
            "bg_color": cfg.rounded_bg_color.value,
            "text_color": cfg.rounded_bg_text_color.value,
            "corner_radius": cfg.rounded_bg_corner_radius.value,
            "padding_h": cfg.rounded_bg_padding_h.value,
            "padding_v": cfg.rounded_bg_padding_v.value,
            "margin_bottom": cfg.rounded_bg_margin_bottom.value,
            "line_spacing": cfg.rounded_bg_line_spacing.value,
            "letter_spacing": cfg.rounded_bg_letter_spacing.value,
        }

    @staticmethod
    def create_transcribe_task(
        file_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> TranscribeTask:
        """创建转录任务"""
        # 获取文件名
        file_name = Path(file_path).stem

        # 构建输出路径
        if need_next_task:
            need_word_time_stamp = cfg.need_split.value
            output_path = str(
                Path(cfg.work_dir.value)
                / file_name
                / "subtitle"
                / f"【原始字幕】{file_name}-{cfg.transcribe_model.value.value}-{cfg.transcribe_language.value.value}.srt"
            )
        else:
            need_word_time_stamp = False
            output_path = str(Path(file_path).parent / f"{file_name}.srt")

        config = TranscribeConfig(
            transcribe_model=cfg.transcribe_model.value,
            transcribe_language=LANGUAGES[cfg.transcribe_language.value.value],
            need_word_time_stamp=need_word_time_stamp,
            output_format=cfg.transcribe_output_format.value,
            # Whisper Cpp 配置
            whisper_model=cfg.whisper_model.value,
            # Whisper API 配置
            whisper_api_key=cfg.whisper_api_key.value,
            whisper_api_base=cfg.whisper_api_base.value,
            whisper_api_model=cfg.whisper_api_model.value,
            whisper_api_prompt=cfg.whisper_api_prompt.value,
            # Faster Whisper 配置
            faster_whisper_program=cfg.faster_whisper_program.value,
            faster_whisper_model=cfg.faster_whisper_model.value,
            faster_whisper_model_dir=str(MODEL_PATH),
            faster_whisper_device=cfg.faster_whisper_device.value,
            faster_whisper_vad_filter=cfg.faster_whisper_vad_filter.value,
            faster_whisper_vad_threshold=cfg.faster_whisper_vad_threshold.value,
            faster_whisper_vad_method=cfg.faster_whisper_vad_method.value,
            faster_whisper_ff_mdx_kim2=cfg.faster_whisper_ff_mdx_kim2.value,
            faster_whisper_one_word=cfg.faster_whisper_one_word.value,
            faster_whisper_prompt=cfg.faster_whisper_prompt.value,
        )

        task = TranscribeTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
            transcribe_config=config,
            need_next_task=need_next_task,
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_subtitle_task(
        file_path: str,
        video_path: Optional[str] = None,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> SubtitleTask:
        """创建字幕任务"""
        # 统一命名基准：全流程任务(video_path 存在)时，4 布局成品 srt 以视频文件名为基准
        # (见 subtitle_thread.py 第4步 `video_path.stem-{布局}.srt`)。样式字幕/普通字幕输出也
        # 应对齐到同一核心名，避免同一集出现「视频名-{布局}.srt」与「字幕名-{翻译服务}.ass」
        # 两种命名基准，导致成品与样式字幕路径看起来不统一。
        # 无 video_path(如单集界面直接处理字幕文件)时，回退到字幕文件名并剥离系统前缀。
        if video_path:
            output_name = Path(video_path).stem
        else:
            # 剥离输入文件名中残留的所有系统前缀标记（【原始字幕】/【下载字幕】/【样式字幕】/【字幕】），
            # 避免对已处理过的字幕文件再次优化/翻译时，输出名出现重复前缀（如「【字幕】【样式字幕】...」）。
            # 使用「锚定到文件名开头 + 全局匹配」的正则，可按任意顺序、以任意层数剥离这些标记。
            _PREFIXES = "原始字幕|下载字幕|样式字幕|字幕"
            output_name = re.sub(rf"^(?:【(?:{_PREFIXES})】)+", "", Path(file_path).stem)
        # 只在需要翻译时添加翻译服务后缀
        suffix = (
            f"-{cfg.translator_service.value.value}" if cfg.need_translate.value else ""
        )

        if need_next_task:
            output_path = str(
                Path(file_path).parent / f"【样式字幕】{output_name}{suffix}.ass"
            )
        else:
            output_path = str(
                Path(file_path).parent / f"【字幕】{output_name}{suffix}.srt"
            )

        # 根据当前选择的LLM服务获取对应的配置（复用公共方法，与 worker 实时刷新同源）
        base_url, api_key, llm_model = TaskFactory.get_current_llm_triplet()

        config = SubtitleConfig(
            # 翻译配置
            base_url=base_url,
            api_key=api_key,
            llm_model=llm_model,
            deeplx_endpoint=cfg.deeplx_endpoint.value,
            # 翻译服务
            translator_service=cfg.translator_service.value,
            # 字幕处理
            need_reflect=cfg.need_reflect_translate.value,
            need_translate=cfg.need_translate.value,
            need_optimize=cfg.need_optimize.value,
            thread_num=cfg.thread_num.value,
            batch_size=cfg.batch_size.value,
            # 字幕布局、样式
            subtitle_layout=cfg.subtitle_layout.value,  # Now returns SubtitleLayoutEnum
            subtitle_style=TaskFactory.get_ass_style(cfg.subtitle_style_name.value),
            # 字幕分割
            max_word_count_cjk=cfg.max_word_count_cjk.value,
            max_word_count_english=cfg.max_word_count_english.value,
            need_split=cfg.need_split.value,
            # 字幕翻译
            target_language=cfg.target_language.value,
            # 字幕提示
            custom_prompt_text=cfg.custom_prompt_text.value,
            # 思考力度
            reasoning_effort=cfg.llm_reasoning_effort.value,
        )

        task = SubtitleTask(
            queued_at=datetime.datetime.now(),
            subtitle_path=file_path,
            video_path=video_path,
            output_path=output_path,
            subtitle_config=config,
            need_next_task=need_next_task,
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_synthesis_task(
        video_path: str,
        subtitle_path: str,
        need_next_task: bool = False,
        task_id: Optional[str] = None,
    ) -> SynthesisTask:
        """创建视频合成任务"""
        output_path = str(
            Path(video_path).parent / f"【卡卡】{Path(video_path).stem}.mp4"
        )

        # 只有启用样式时才传入样式配置
        use_style = cfg.use_subtitle_style.value
        config = SynthesisConfig(
            need_video=cfg.need_video.value,
            soft_subtitle=cfg.soft_subtitle.value,
            render_mode=cfg.subtitle_render_mode.value,
            video_quality=cfg.video_quality.value,
            subtitle_layout=cfg.subtitle_layout.value,
            ass_style=TaskFactory.get_ass_style(cfg.subtitle_style_name.value) if use_style else "",
            rounded_style=TaskFactory.get_rounded_style() if use_style else None,
        )

        task = SynthesisTask(
            queued_at=datetime.datetime.now(),
            video_path=video_path,
            subtitle_path=subtitle_path,
            output_path=output_path,
            synthesis_config=config,
            need_next_task=need_next_task,
        )
        if task_id:
            task.task_id = task_id
        return task

    @staticmethod
    def create_transcript_and_subtitle_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
    ) -> TranscriptAndSubtitleTask:
        """创建转录和字幕任务"""
        if output_path is None:
            output_path = str(
                Path(file_path).parent / f"{Path(file_path).stem}_processed.srt"
            )

        return TranscriptAndSubtitleTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
        )

    @staticmethod
    def create_full_process_task(
        file_path: str,
        output_path: Optional[str] = None,
        transcribe_config: Optional[TranscribeConfig] = None,
        subtitle_config: Optional[SubtitleConfig] = None,
        synthesis_config: Optional[SynthesisConfig] = None,
    ) -> FullProcessTask:
        """创建完整处理任务（转录+字幕+合成）"""
        if output_path is None:
            output_path = str(
                Path(file_path).parent
                / f"{Path(file_path).stem}_final{Path(file_path).suffix}"
            )

        return FullProcessTask(
            queued_at=datetime.datetime.now(),
            file_path=file_path,
            output_path=output_path,
        )
