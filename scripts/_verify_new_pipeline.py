# -*- coding: utf-8 -*-
"""单集小样本验证新 pipeline（检查点 + 两击重试 + 提速参数）。

截取原始 SRT 前若干条 -> 用显式新参数 (rpm/thread/batch) 调真实 cli_run
-> 观察每批 checkpoint 落盘、限流退避、重试上限、最终输出。

用法:
  uv run python scripts/_verify_new_pipeline.py <原始.srt> [样本条数=75] [batch=25] [thread=2] [rpm=4]
"""
import sys
from argparse import Namespace
from pathlib import Path

from videocaptioner.ui.common.config import cfg
from videocaptioner.cli.commands.subtitle import run as cli_run


def take_first_segments(src: Path, dst: Path, n: int) -> int:
    """截取 SRT 前n个字幕块（块=序号行+时间行+文本行+空行）。返回实际条数。"""
    text = src.read_text(encoding="utf-8")
    blocks = text.replace("\r\n", "\n").split("\n\n")
    kept, count = [], 0
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip() != ""]
        if len(lines) >= 2:
            kept.append(b)
            count += 1
            if count >= n:
                break
    dst.write_text("\n\n".join(kept) + "\n", encoding="utf-8")
    return count


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _verify_new_pipeline.py <原始.srt> [样本条数] [batch] [thread] [rpm]", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 75
    batch = int(sys.argv[3]) if len(sys.argv) > 3 else 25
    thread = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    rpm = int(sys.argv[5]) if len(sys.argv) > 5 else 4

    work = Path("work-dir/_verify_new_pipeline")
    work.mkdir(parents=True, exist_ok=True)
    sample = work / "sample_input.srt"
    got = take_first_segments(src, sample, n)
    print(f"[样本] 截取 {got} 条 -> {sample}", flush=True)

    llm_base = cfg.openai_api_base.value or ""
    llm_key = cfg.openai_api_key.value or ""
    llm_model = cfg.openai_model.value or ""
    config = {
        "llm": {
            "api_key": llm_key,
            "api_base": llm_base,
            "model": llm_model,
            "reasoning_effort": "none",
            "rpm_limit": rpm,          # 新参数：CLI set_rpm_override 读这里
        },
        "subtitle": {
            "optimize": True,
            "translate": True,
            "split": True,   # 必须开：词级碎片 -> LLM 断句合并成完整句，否则单词条目直接进翻译导致语义破碎
            "max_word_count_cjk": cfg.max_word_count_cjk.value,
            "max_word_count_english": cfg.max_word_count_english.value,
            "thread_num": thread,       # 新参数
            "batch_size": batch,        # 新参数
        },
        "translate": {"service": "llm", "target_language": "zh-Hans", "reflect": cfg.need_reflect_translate.value},
        "synthesize": {"layout": "source-above"},
        "output": {"format": "srt"},
        "custom_prompt_text": cfg.custom_prompt_text.value or "",
    }
    prompt = config.pop("custom_prompt_text", "")

    args = Namespace(
        input=str(sample), output=str(work / "sample_output.srt"), format="srt",
        translator="llm", target_language="zh-Hans", reflect=config["translate"]["reflect"],
        no_optimize=False, no_translate=False, no_split=False,
        max_cjk=config["subtitle"]["max_word_count_cjk"],
        max_english=config["subtitle"]["max_word_count_english"],
        prompt=prompt, prompt_file=None,
        thread_num=thread, batch_size=batch,
        layout="source-above", verbose=True, quiet=False,
    )
    print(f"[参数] rpm={rpm} thread={thread} batch={batch} reflect={config['translate']['reflect']}", flush=True)
    print("[运行] 开始 cli_run ...", flush=True)
    rc = cli_run(args, config)
    print(f"[结束] rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
