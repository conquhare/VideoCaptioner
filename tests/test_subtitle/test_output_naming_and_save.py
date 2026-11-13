"""Regression tests for subtitle output naming and save routing.

Covers two bugs from the "optimized+translated output is 0KB" incident:
1. Output name double-prefixing: reprocessing a ``【样式字幕】``/``【字幕】`` file
   used to leak the old prefix into the new name (e.g. ``【字幕】【样式字幕】...``).
2. Save routing: ``ASRData.save()`` must write content matching the file
   extension, and must not leave a truncated/0-byte file if serialization fails.
"""

from pathlib import Path

import pytest

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleLayoutEnum
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.task_factory import TaskFactory


@pytest.fixture
def tmp_srt(tmp_path: Path) -> Path:
    """A minimal valid source SRT file to feed into the task factory."""
    srt = tmp_path / "movie.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
        encoding="utf-8",
    )
    return srt


def _set_cfg_value(item, value) -> None:
    """Set a qfluentwidgets ConfigItem's in-memory value (no disk write)."""
    item.value = value


class TestOutputNamePrefixStripping:
    """create_subtitle_task must not produce double-prefixed output names."""

    def test_strips_original_subtitle_prefix(self, tmp_srt: Path, monkeypatch):
        src = tmp_srt.with_name("【原始字幕】movie.srt")
        src.write_text(tmp_srt.read_text(encoding="utf-8"), encoding="utf-8")
        _set_cfg_value(cfg.need_translate, False)

        task = TaskFactory.create_subtitle_task(file_path=str(src), need_next_task=False)

        assert task.output_path is not None
        name = Path(task.output_path).name
        assert name.startswith("【字幕】")
        # The base name must not retain any system prefix markers.
        assert "【原始字幕】" not in name
        assert name.count("【字幕】") == 1

    def test_strips_style_subtitle_prefix_no_double(self, tmp_srt: Path):
        """Reprocessing a 【样式字幕】 file must not yield 【字幕】【样式字幕】..."""
        src = tmp_srt.with_name("【样式字幕】movie.srt")
        src.write_text(tmp_srt.read_text(encoding="utf-8"), encoding="utf-8")
        _set_cfg_value(cfg.need_translate, False)

        task = TaskFactory.create_subtitle_task(file_path=str(src), need_next_task=False)

        name = Path(task.output_path).name
        assert name == "【字幕】movie.srt"

    def test_strips_nested_prefixes(self, tmp_srt: Path):
        """A file already carrying the buggy double prefix is cleaned up."""
        src = tmp_srt.with_name("【字幕】【样式字幕】movie.srt")
        src.write_text(tmp_srt.read_text(encoding="utf-8"), encoding="utf-8")
        _set_cfg_value(cfg.need_translate, False)

        task = TaskFactory.create_subtitle_task(file_path=str(src), need_next_task=False)

        name = Path(task.output_path).name
        assert name == "【字幕】movie.srt"

    def test_style_task_uses_ass_extension(self, tmp_srt: Path):
        """need_next_task=True routes to a single-prefixed .ass output."""
        src = tmp_srt.with_name("【样式字幕】movie.srt")
        src.write_text(tmp_srt.read_text(encoding="utf-8"), encoding="utf-8")
        _set_cfg_value(cfg.need_translate, False)

        task = TaskFactory.create_subtitle_task(file_path=str(src), need_next_task=True)

        name = Path(task.output_path).name
        assert name == "【样式字幕】movie.ass"


class TestSaveRouting:
    """ASRData.save() must match content to extension and write atomically."""

    @staticmethod
    def _bilingual_data() -> ASRData:
        return ASRData(
            [
                ASRDataSeg(
                    start_time=1000,
                    end_time=2000,
                    text="Hello",
                    translated_text="你好",
                )
            ]
        )

    def test_srt_path_gets_srt_content(self, tmp_path: Path):
        out = tmp_path / "out.srt"
        self._bilingual_data().save(str(out), layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP)

        text = out.read_text(encoding="utf-8")
        assert "00:00:01,000 --> 00:00:02,000" in text
        assert "Dialogue:" not in text  # must NOT be ASS content

    def test_ass_path_gets_ass_content(self, tmp_path: Path):
        out = tmp_path / "out.ass"
        self._bilingual_data().save(
            str(out),
            layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP,
            ass_style="",
        )

        text = out.read_text(encoding="utf-8")
        assert "[Script Info]" in text
        assert "Dialogue:" in text

    def test_failed_serialization_leaves_no_stub(self, tmp_path: Path):
        """If serialization raises, the target must not be truncated to 0 bytes."""
        out = tmp_path / "out.srt"
        out.write_text("PRE-EXISTING", encoding="utf-8")

        data = self._bilingual_data()
        # Force serialization to blow up mid-way.
        monkey_target = ASRData(
            [ASRDataSeg(start_time=0, end_time=0, text="x", translated_text="y")]
        )
        monkey_target.segments = None  # type: ignore[assignment]

        with pytest.raises(Exception):
            monkey_target.save(str(out), layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP)

        # The pre-existing file must be intact (atomic write), not emptied.
        assert out.read_text(encoding="utf-8") == "PRE-EXISTING"

    def test_no_zero_byte_output_on_success(self, tmp_path: Path):
        out = tmp_path / "out.srt"
        self._bilingual_data().save(str(out), layout=SubtitleLayoutEnum.ORIGINAL_ON_TOP)
        assert out.stat().st_size > 0
