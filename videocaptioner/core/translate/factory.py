"""翻译器工厂"""

from typing import Callable, Optional

from videocaptioner.core.translate.base import BaseTranslator
from videocaptioner.core.translate.bing_translator import BingTranslator
from videocaptioner.core.translate.deeplx_translator import DeepLXTranslator
from videocaptioner.core.translate.google_translator import GoogleTranslator
from videocaptioner.core.translate.llm_translator import LLMTranslator
from videocaptioner.core.translate.types import TargetLanguage, TranslatorType
from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("translator_factory")


class TranslatorFactory:
    """翻译器工厂类"""

    @staticmethod
    def create_translator(
        translator_type: TranslatorType,
        thread_num: int = 5,
        batch_num: int = 10,
        target_language: Optional[TargetLanguage] = None,
        model: str = "gpt-4o-mini",
        custom_prompt: str = "",
        is_reflect: bool = False,
        update_callback: Optional[Callable] = None,
        base_url: str = "",
        api_key: str = "",
        reasoning_effort: str = "none",
        checkpoint_callback: Optional[Callable] = None,
    ) -> BaseTranslator:
        """创建翻译器实例"""
        try:
            # 如果没有指定目标语言，使用默认值
            if target_language is None:
                target_language = TargetLanguage.SIMPLIFIED_CHINESE

            if translator_type == TranslatorType.OPENAI:
                translator: BaseTranslator = LLMTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    model=model,
                    custom_prompt=custom_prompt,
                    is_reflect=is_reflect,
                    update_callback=update_callback,
                    base_url=base_url,
                    api_key=api_key,
                    reasoning_effort=reasoning_effort,
                )
            elif translator_type == TranslatorType.GOOGLE:
                batch_num = 5
                translator = GoogleTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    timeout=20,
                    update_callback=update_callback,
                )
            elif translator_type == TranslatorType.BING:
                batch_num = 10
                translator = BingTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    update_callback=update_callback,
                )
            elif translator_type == TranslatorType.DEEPLX:
                batch_num = 5
                translator = DeepLXTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    timeout=20,
                    update_callback=update_callback,
                )
            else:
                translator = LLMTranslator(
                    thread_num=thread_num,
                    batch_num=batch_num,
                    target_language=target_language,
                    model=model,
                    custom_prompt=custom_prompt,
                    is_reflect=is_reflect,
                    update_callback=update_callback,
                    base_url=base_url,
                    api_key=api_key,
                    reasoning_effort=reasoning_effort,
                )

            # 统一注入检查点回调（各子类经 BaseTranslator 读取 self.checkpoint_callback）
            if checkpoint_callback is not None:
                translator.checkpoint_callback = checkpoint_callback

            return translator
        except Exception as e:
            logger.error(f"Failed to create translator: {str(e)}")
            raise
