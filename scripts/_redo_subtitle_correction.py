# -*- coding: utf-8 -*-
"""批量重做「字幕校正」：对给定【原始字幕】跑 优化+翻译，复现 GUI 的正确配置。

直接调用 CLI 的 run()（无需 Qt 事件循环），config 从 GUI settings.json 同步，
确保用上：赛博朋克边缘行者2 提示词 + sensenova LLM + 原文在上 + 反思翻译。

用法: uv run python scripts/_redo_subtitle_correction.py <原始字幕.srt> [<更多.srt>...]
"""
import sys
from argparse import Namespace
from pathlib import Path

# 让 CLI 的 run() 复用 GUI 配置
from videocaptioner.ui.common.config import cfg
from videocaptioner.cli.commands.subtitle import run as cli_run


def build_config(split: bool) -> dict:
    """从 GUI cfg 构造 CLI run() 所需的 config dict。

    split: 原始字幕是否需要重新断句。
      - 词级原始字幕(FasterWhisper 单词级输出, 如 2.Give Me Another Day)必须开 split,
        否则单字/词组直接进优化+翻译导致语义破碎、机翻直译感。
      - 已断句的原始字幕可关 split(重做校正不重新断句)。
    """
    llm_base, llm_key, llm_model = cfg._get_llm_triplet() if hasattr(cfg, "_get_llm_triplet") else (
        cfg.openai_api_base.value or "", cfg.openai_api_key.value or "", cfg.openai_model.value or ""
    )
    return {
        "llm": {
            "api_key": llm_key,
            "api_base": llm_base,
            "model": llm_model,
            # 关闭思考模式以提速(合法值 none/low/medium/high/max)
            "reasoning_effort": cfg.llm_reasoning_effort.value if hasattr(cfg, "llm_reasoning_effort") else "none",
            # 免费端点真实上限约 3-5 RPM；CLI set_rpm_override 读这里。设 4 让令牌桶主动
            # 节流到安全线以下，避免几乎每请求吃 429 + 5-60s 退避（旧档 rpm=20 曾致 7.5h）。
            "rpm_limit": 4,
        },
        "subtitle": {
            "optimize": True,
            "translate": True,
            "split": split,  # 词级原始字幕必须开；已断句的可关(见 build_config docstring)
            "max_word_count_cjk": cfg.max_word_count_cjk.value,
            "max_word_count_english": cfg.max_word_count_english.value,
            "thread_num": 2,   # 2 线程并发；配合 rpm=4 令牌桶，并发不致触发限流
            "batch_size": 20,  # 每批 20 条；失败重试时减半批。原文在上/反思翻译用此档验证通过
        },
        "translate": {
            "service": "llm",  # GUI 用 LLM 翻译
            "target_language": "zh-Hans",
            "reflect": cfg.need_reflect_translate.value,
        },
        "synthesize": {"layout": "source-above"},  # 原文在上
        "output": {"format": "srt"},
        "custom_prompt_text": cfg.custom_prompt_text.value or "",
    }


def main() -> int:
    # --split: 对词级原始字幕(FasterWhisper 单词级输出)重新断句合并成完整句。
    # 缺省关(已断句的原始字幕重做校正不重新断句)。
    argv = sys.argv[1:]
    split = "--split" in argv
    argv = [a for a in argv if a != "--split"]
    files = [Path(p) for p in argv]
    if not files:
        print(
            "usage: _redo_subtitle_correction.py [--split] <原始字幕.srt> [...]\n"
            "  --split  词级原始字幕需重新断句合并成完整句(如 FasterWhisper 单词级输出)。",
            file=sys.stderr,
        )
        return 2
    config = build_config(split)
    prompt = config.pop("custom_prompt_text", "")
    rc_all = 0
    for f in files:
        if not f.exists():
            print(f"[SKIP] not found: {f}", file=sys.stderr)
            continue
        print(f"\n===== 校正开始: {f.name} (split={split}) =====", flush=True)
        args = Namespace(
            input=str(f), output=None, format="srt",
            translator="llm", target_language="zh-Hans", reflect=config["translate"]["reflect"],
            no_optimize=False, no_translate=False, no_split=not split,
            max_cjk=config["subtitle"]["max_word_count_cjk"],
            max_english=config["subtitle"]["max_word_count_english"],
            prompt=prompt, prompt_file=None,
            thread_num=config["subtitle"]["thread_num"],
            batch_size=config["subtitle"]["batch_size"],
            layout="source-above", verbose=True, quiet=False,
        )
        rc = cli_run(args, config)
        print(f"===== 校正结束: {f.name}  rc={rc} =====", flush=True)
        if rc != 0:
            rc_all = rc
    return rc_all


if __name__ == "__main__":
    sys.exit(main())
