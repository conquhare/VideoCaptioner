# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, QTime, pyqtSignal
from PyQt5.QtGui import QBrush, QCloseEvent, QColor, QDragEnterEvent, QDropEvent, QKeyEvent
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CommandBar,
    InfoBar,
    InfoBarPosition,
    MessageBoxBase,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    TableView,
    TextEdit,
    TransparentDropDownPushButton,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.config import SUBTITLE_STYLE_PATH
from videocaptioner.core.asr.asr_data import ASRData
from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_INFO,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.entities import (
    OutputSubtitleFormatEnum,
    ProcessStage,
    SubtitleFailure,
    SubtitleLayoutEnum,
    SubtitleTask,
    SupportedSubtitleFormats,
)
from videocaptioner.core.subtitle import get_subtitle_style
from videocaptioner.core.translate.types import TargetLanguage
from videocaptioner.core.utils.error_classifier import FatalErrorType
from videocaptioner.core.utils.platform_utils import open_folder, reveal_in_explorer
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.common.signal_bus import signalBus
from videocaptioner.ui.components.SubtitleSettingDialog import SubtitleSettingDialog
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.retry_thread import RetryThread
from videocaptioner.ui.thread.subtitle_thread import RetranslateThread, SubtitleThread

# 失败标注配色（浅红底 + 红字），P0 用行着色 + 文字标签，P1 再加精致角标 delegate
_FAILURE_BG = QColor(255, 235, 238)  # 浅红底
_FAILURE_FG = QColor(211, 47, 47)  # 红字
_FAILURE_LABEL = "［失败］"  # 失败行在「字幕内容」列的文字标签前缀


class SubtitleTableModel(QAbstractTableModel):
    def __init__(self, data: Union[str, Dict[str, Any]] = ""):
        super().__init__()
        self._data: Dict[str, Any] = {}
        if isinstance(data, str):
            self.load_data(data)
        else:
            self._data = data

    def load_data(self, data: str):
        """加载字幕数据"""
        try:
            self._data = json.loads(data)
            self.layoutChanged.emit()
        except json.JSONDecodeError:
            pass

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:  # type: ignore
        if not index.isValid() or not self._data:
            return None

        row = index.row()
        col = index.column()
        segment = self._data.get(str(row + 1))

        if not segment:
            return None

        if role == Qt.DisplayRole or role == Qt.EditRole:  # type: ignore
            if col == 0:
                return (
                    QTime(0, 0)
                    .addMSecs(segment["start_time"])
                    .toString("hh:mm:ss.zzz")[:-2]
                )
            elif col == 1:
                return (
                    QTime(0, 0)
                    .addMSecs(segment["end_time"])
                    .toString("hh:mm:ss.zzz")[:-2]
                )
            elif col == 2:
                text = segment["original_subtitle"]
                if self._is_failed_segment(segment):
                    text = _FAILURE_LABEL + text  # 失败行文字标签（P0）
                return text
            elif col == 3:
                return segment["translated_subtitle"]
        elif role == Qt.TextAlignmentRole:  # type: ignore
            if col in [0, 1]:
                return Qt.AlignCenter  # type: ignore
        # 失败标注：失败行浅红底 + 失败列红字 + tooltip 显示错误信息
        elif role == Qt.BackgroundRole:  # type: ignore
            if self._is_failed_segment(segment):
                return QBrush(_FAILURE_BG)
        elif role == Qt.ForegroundRole:  # type: ignore
            if self._is_failed_segment(segment):
                return QBrush(_FAILURE_FG)
        elif role == Qt.ToolTipRole:  # type: ignore
            if self._is_failed_segment(segment):
                return self._failure_tooltip(segment)
        return None

    @staticmethod
    def _segment_failed_stages(segment: Dict[str, Any]) -> List[str]:
        """返回该行失败的阶段值列表（如 ['optimize', 'translate']）"""
        stages = segment.get("failed_stages")
        return list(stages) if stages else []

    @classmethod
    def _is_failed_segment(cls, segment: Dict[str, Any]) -> bool:
        """该行是否有任一失败阶段"""
        return bool(cls._segment_failed_stages(segment))

    @staticmethod
    def _failure_tooltip(segment: Dict[str, Any]) -> str:
        """构造失败行 tooltip：错误信息 + 失败阶段"""
        stages = segment.get("failed_stages") or []
        message = segment.get("error_message") or ""
        parts = []
        if stages:
            parts.append("失败阶段: " + ", ".join(stages))
        if message:
            parts.append(message)
        return "\n".join(parts)

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.EditRole) -> bool:  # type: ignore
        if not index.isValid() or not self._data:
            return False

        if role == Qt.EditRole:  # type: ignore
            row = index.row()
            col = index.column()
            segment = self._data.get(str(row + 1))

            if not segment:
                return False

            if col == 2:
                segment["original_subtitle"] = value
            elif col == 3:
                segment["translated_subtitle"] = value
            else:
                return False

            self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])  # type: ignore
            return True
        return False

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.DisplayRole,  # type: ignore
    ) -> Any:  # type: ignore
        if role == Qt.DisplayRole:  # type: ignore
            if orientation == Qt.Horizontal:  # type: ignore
                return [
                    self.tr("开始时间"),
                    self.tr("结束时间"),
                    self.tr("字幕内容"),
                    (
                        self.tr("翻译字幕")
                        if cfg.need_translate.value
                        else self.tr("优化字幕")
                    ),
                ][section]
            elif orientation == Qt.Vertical:  # type: ignore
                return str(section + 1)  # 显示行号
        elif role == Qt.TextAlignmentRole:  # type: ignore
            return Qt.AlignCenter  # type: ignore  # 居中对齐
        return None

    def rowCount(self, parent: Optional[QModelIndex] = None) -> int:
        return len(self._data)

    def columnCount(self, parent: Optional[QModelIndex] = None) -> int:
        return 4

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags  # type: ignore
        if index.column() in [2, 3]:
            return Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable  # type: ignore
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable  # type: ignore

    def update_data(self, new_data: Dict[str, str]) -> None:
        """更新字幕数据"""
        updated_rows = set()

        # 更新内部数据
        for key, value in new_data.items():
            if key in self._data:
                self._data[key]["translated_subtitle"] = value
                row = list(self._data.keys()).index(key)
                updated_rows.add(row)

        # 如果有更新，发出dataChanged信号
        if updated_rows:
            min_row = min(updated_rows)
            max_row = max(updated_rows)
            top_left = self.index(min_row, 2)
            bottom_right = self.index(max_row, 3)
            self.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole, Qt.EditRole])  # type: ignore

    def update_all(self, data: Dict[str, Any]) -> None:
        """更新所有数据"""
        self._data = data
        self.layoutChanged.emit()

    def update_rows_full(self, new_data: Dict[str, Any]) -> None:
        """用完整 to_json 行覆盖对应 `_data[key]`（含 failed_stages/error_message）。

        与 `update_data`（仅合并 translated_subtitle）不同，本方法整行覆盖，
        因此重试成功项的 `failed_stages` 会被清空、恢复常色；仍失败项保留失败标注。

        Args:
            new_data: {str(index): 完整 to_json 行}，键为 1-based 行号字符串。
        """
        affected_rows = set()

        for key, value in new_data.items():
            if key not in self._data:
                continue
            # 整行覆盖：保留 model 端键顺序，仅替换该行内容
            self._data[key].update(value)
            row = list(self._data.keys()).index(key)
            affected_rows.add(row)

        if affected_rows:
            top_left = self.index(min(affected_rows), 0)
            bottom_right = self.index(max(affected_rows), self.columnCount() - 1)
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [
                    Qt.DisplayRole,
                    Qt.EditRole,
                    Qt.BackgroundRole,
                    Qt.ForegroundRole,
                    Qt.ToolTipRole,
                ],  # type: ignore
            )

    def set_failure_markers(
        self,
        indices: List[int],
        stage: ProcessStage,
        message: str = "",
        clear: bool = False,
    ) -> None:
        """批量把失败标注写入 `_data[key]["failed_stages"]` 并局部刷新对应行。

        Args:
            indices: 受影响的字幕 index（1-based）。
            stage: 失败阶段（optimize/translate/split）。
            message: 错误信息，写入 error_message 供 tooltip 显示。
            clear: 为 True 时清除（而非写入）该阶段失败标记（用于重试成功后）。
        """
        stage_value = stage.value if isinstance(stage, ProcessStage) else str(stage)
        affected_rows = set()

        for idx in indices:
            key = str(idx)  # model._data 用字符串 key
            segment = self._data.get(key)
            if not segment:
                continue
            stages = set(segment.get("failed_stages") or [])
            if clear:
                stages.discard(stage_value)
                if not stages:
                    segment["error_message"] = ""
            else:
                stages.add(stage_value)
                if message:
                    segment["error_message"] = message
            segment["failed_stages"] = sorted(stages)

            row = list(self._data.keys()).index(key)
            affected_rows.add(row)

        if affected_rows:
            top_left = self.index(min(affected_rows), 0)
            bottom_right = self.index(max(affected_rows), self.columnCount() - 1)
            self.dataChanged.emit(
                top_left,
                bottom_right,
                [Qt.DisplayRole, Qt.BackgroundRole, Qt.ForegroundRole, Qt.ToolTipRole],  # type: ignore
            )

    def collect_failures(self) -> Dict[ProcessStage, List[int]]:
        """从 `_data` 按 failed_stages 归类出各阶段失败 index（1-based，升序）。

        供「重试全部失败项」收集待重发字幕。
        """
        failures: Dict[ProcessStage, List[int]] = {}
        for key, segment in self._data.items():
            for stage_value in segment.get("failed_stages") or []:
                try:
                    stage = ProcessStage(stage_value)
                except ValueError:
                    continue
                indices = failures.setdefault(stage, [])
                index = int(key)
                if index not in indices:
                    indices.append(index)
        for indices in failures.values():
            indices.sort()
        return failures

    def count_failures(self) -> int:
        """当前失败字幕总数（按行去重，一行可能失败于多个阶段）。"""
        return sum(
            1
            for segment in self._data.values()
            if segment.get("failed_stages")
        )


class SubtitleInterface(QWidget):
    finished = pyqtSignal(str, str)
    # 请求重试信号：批量（indices 为空表示全部失败项）或单条（带 stage）。
    # 本次 T04 只做 UI 入口 + 信号，实际重试由 T03/T05 的 RetryThread 承接并接线本信号。
    retry_requested = pyqtSignal(object, object)  # (List[int], Optional[ProcessStage])

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.task: Optional[SubtitleTask] = None
        self.subtitle_path: Optional[str] = None
        self.custom_prompt_text: str = cfg.custom_prompt_text.value
        # 鉴权/欠费致命错误后禁用重试（重试无意义），恢复需重新加载/改配置
        self._retry_disabled_by_auth = False
        self.setAttribute(Qt.WA_DeleteOnClose)  # type: ignore
        self._init_ui()
        self._setup_signals()
        self._update_prompt_button_style()
        self.set_values()
        # 接线手动重试信号 → spawn RetryThread（T05 核心）
        self.retry_requested.connect(self._on_retry_requested)

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setObjectName("main_layout")
        self.main_layout.setSpacing(20)

        self._setup_top_layout()
        self._setup_subtitle_table()
        self._setup_bottom_layout()

    def set_values(self):
        self.layout_button.setText(
            cfg.subtitle_layout.value.value
        )  # Get enum's string value
        self.translate_button.setChecked(cfg.need_translate.value)
        self.optimize_button.setChecked(cfg.need_optimize.value)
        self.target_language_button.setText(cfg.target_language.value.value)
        self.target_language_button.setEnabled(cfg.need_translate.value)

    def _setup_top_layout(self):
        # 创建水平布局
        top_layout = QHBoxLayout()

        # 创建命令栏
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(
            Qt.ToolButtonTextBesideIcon  # type: ignore
        )  # 设置图标和文字并排显示
        top_layout.addWidget(self.command_bar, 1)  # 设置stretch为1，使其尽可能占用空间

        # 创建保存按钮的下拉菜单
        save_menu = RoundMenu(parent=self)
        save_menu.view.setMaxVisibleItems(8)  # 设置菜单最大高度
        for format in OutputSubtitleFormatEnum:
            action = Action(text=format.value)
            action.triggered.connect(
                lambda checked, f=format.value: self.on_save_format_clicked(f)
            )
            save_menu.addAction(action)

        # 添加保存按钮(带下拉菜单)
        save_button = TransparentDropDownPushButton(self.tr("保存"), self, FIF.SAVE)
        save_button.setMenu(save_menu)
        save_button.setFixedHeight(34)
        self.command_bar.addWidget(save_button)

        # 添加字幕排布下拉按钮
        self.layout_button = TransparentDropDownPushButton(
            self.tr("字幕排布"), self, FIF.LAYOUT
        )
        self.layout_button.setFixedHeight(34)
        self.layout_button.setMinimumWidth(125)
        self.layout_menu = RoundMenu(parent=self)
        for layout in ["译文在上", "原文在上", "仅译文", "仅原文"]:
            action = Action(text=layout)
            action.triggered.connect(
                lambda checked, layout_value=layout: signalBus.subtitle_layout_changed.emit(
                    layout_value
                )
            )
            self.layout_menu.addAction(action)
        self.layout_button.setMenu(self.layout_menu)
        self.command_bar.addWidget(self.layout_button)

        self.command_bar.addSeparator()

        # 添加字幕优化按钮
        self.optimize_button = Action(
            FIF.EDIT,
            self.tr("字幕校正"),
            triggered=self.on_subtitle_optimization_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.optimize_button)

        # 添加字幕翻译按钮
        self.translate_button = Action(
            FIF.LANGUAGE,
            self.tr("字幕翻译"),
            triggered=self.on_subtitle_translation_changed,
            checkable=True,
        )
        self.command_bar.addAction(self.translate_button)

        # 添加翻译语言选择
        self.target_language_button = TransparentDropDownPushButton(
            self.tr("翻译语言"), self, FIF.LANGUAGE
        )
        self.target_language_button.setFixedHeight(34)
        self.target_language_button.setMinimumWidth(125)
        self.target_language_menu = RoundMenu(parent=self)
        self.target_language_menu.setMaxVisibleItems(10)
        for lang in TargetLanguage:
            action = Action(text=lang.value)
            action.triggered.connect(
                lambda checked, lang_value=lang.value: signalBus.target_language_changed.emit(
                    lang_value
                )
            )
            self.target_language_menu.addAction(action)
        self.target_language_button.setMenu(self.target_language_menu)

        self.command_bar.addWidget(self.target_language_button)

        self.command_bar.addSeparator()

        # 添加文稿提示按钮
        self.prompt_button = Action(
            FIF.DOCUMENT, self.tr("Prompt"), triggered=self.show_prompt_dialog
        )
        self.command_bar.addAction(self.prompt_button)

        # 添加设置按钮
        self.command_bar.addAction(
            Action(FIF.SETTING, "", triggered=self.show_subtitle_settings)
        )

        # 添加视频播放按钮
        # self.command_bar.addAction(Action(FIF.VIDEO, "", triggered=self.show_video_player))

        # 添加打开文件夹按钮
        self.command_bar.addAction(
            Action(FIF.FOLDER, "", triggered=self.on_open_folder_clicked)
        )

        self.command_bar.addSeparator()

        # 添加文件选择按钮
        self.command_bar.addAction(
            Action(FIF.FOLDER_ADD, "", triggered=self.on_file_select)
        )

        # 添加开始按钮到水平布局
        self.start_button = PrimaryPushButton(self.tr("开始"), self, icon=FIF.PLAY)
        self.start_button.clicked.connect(
            lambda: self.start_subtitle_optimization(need_create_task=True)
        )
        self.start_button.setFixedHeight(34)
        # 悬浮提示：说明断点续跑与失败重试行为
        self.start_button.setToolTip(
            self.tr(
                "开始字幕校正/翻译。\n"
                "· 每批完成即写入检查点(<输出名>.checkpoint.srt)，中断后可从检查点续跑；\n"
                "· 失败条目会先整体跑一遍，末尾再对失败项减半批统一重试，仍失败则保留占位，可稍后在此界面手动重试。"
            )
        )
        top_layout.addWidget(self.start_button)

        # 「重试全部失败项(N)」按钮：无失败时隐藏，N 动态 = 当前失败总数
        self.retry_all_button = PushButton(self.tr("重试全部失败项"), self, icon=FIF.SYNC)
        self.retry_all_button.setFixedHeight(34)
        self.retry_all_button.hide()
        self.retry_all_button.clicked.connect(self.retry_all_failed)
        top_layout.addWidget(self.retry_all_button)

        self.main_layout.addLayout(top_layout)

    def _setup_subtitle_table(self):
        self.subtitle_table = TableView(self)
        self.model = SubtitleTableModel("")
        self.subtitle_table.setModel(self.model)
        self.subtitle_table.setBorderVisible(True)
        self.subtitle_table.setBorderRadius(8)
        self.subtitle_table.setWordWrap(True)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.subtitle_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Fixed
        )
        self.subtitle_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Fixed
        )
        self.subtitle_table.setColumnWidth(0, 120)
        self.subtitle_table.setColumnWidth(1, 120)

        # 配置垂直表头
        self.subtitle_table.verticalHeader().setVisible(True)  # 显示垂直表头
        self.subtitle_table.verticalHeader().setDefaultAlignment(
            Qt.AlignCenter  # type: ignore
        )  # 居中对齐
        self.subtitle_table.verticalHeader().setDefaultSectionSize(50)  # 行高
        self.subtitle_table.verticalHeader().setMinimumWidth(20)  # 设置最小宽度

        self.subtitle_table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed  # type: ignore
        )
        self.subtitle_table.clicked.connect(self.on_subtitle_clicked)
        # 添加右键菜单支持
        self.subtitle_table.setContextMenuPolicy(Qt.CustomContextMenu)  # type: ignore
        self.subtitle_table.customContextMenuRequested.connect(self.show_context_menu)
        self.main_layout.addWidget(self.subtitle_table)

    def _setup_bottom_layout(self):
        self.bottom_layout = QHBoxLayout()
        self.progress_bar = ProgressBar(self)
        self.status_label = BodyLabel(self.tr("请拖入字幕文件"), self)
        self.status_label.setMinimumWidth(100)
        self.status_label.setAlignment(Qt.AlignCenter)  # type: ignore

        # 添加取消按钮
        self.cancel_button = PushButton(self.tr("取消"), self, icon=FIF.CANCEL)
        self.cancel_button.hide()  # 初始隐藏
        self.cancel_button.clicked.connect(self.cancel_optimization)

        self.bottom_layout.addWidget(self.progress_bar, 1)
        self.bottom_layout.addWidget(self.status_label)
        self.bottom_layout.addWidget(self.cancel_button)
        self.main_layout.addLayout(self.bottom_layout)

    def _setup_signals(self) -> None:
        signalBus.subtitle_layout_changed.connect(self.on_subtitle_layout_changed)
        signalBus.target_language_changed.connect(self.on_target_language_changed)
        signalBus.subtitle_optimization_changed.connect(
            self.on_subtitle_optimization_changed
        )
        signalBus.subtitle_translation_changed.connect(
            self.on_subtitle_translation_changed
        )
        # self.subtitle_setting_button.clicked.connect(self.show_subtitle_settings)
        # self.video_player_button.clicked.connect(self.show_video_player)

    def show_prompt_dialog(self) -> None:
        dialog = PromptDialog(self)
        if dialog.exec_():
            self.custom_prompt_text = cfg.custom_prompt_text.value
            self._update_prompt_button_style()

    def _update_prompt_button_style(self) -> None:
        if self.custom_prompt_text.strip():
            green_icon = FIF.DOCUMENT.colored(
                QColor(76, 255, 165), QColor(76, 255, 165)
            )
            self.prompt_button.setIcon(green_icon)
        else:
            self.prompt_button.setIcon(FIF.DOCUMENT)

    def set_task(self, task: SubtitleTask) -> None:
        """设置任务并更新UI"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()  # type: ignore
        self.start_button.setEnabled(True)
        self.task = task
        self.subtitle_path = task.subtitle_path
        self.update_info(task)

    def update_info(self, task: SubtitleTask) -> None:
        """更新页面信息"""
        if not self.task:
            return
        original_subtitle_save_path = Path(str(self.task.subtitle_path))
        asr_data = ASRData.from_subtitle_file(str(original_subtitle_save_path))
        self.model._data = asr_data.to_json()
        self.model.layoutChanged.emit()
        self.status_label.setText(self.tr("已加载文件"))
        self._retry_disabled_by_auth = False  # 新任务恢复重试能力
        self._update_retry_all_button()

    def start_subtitle_optimization(self, need_create_task: bool = True) -> None:
        if self._is_processing():
            return
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("请先加载字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return
        self.start_button.setEnabled(False)
        self.retry_all_button.setEnabled(False)  # 重试期间置灰，避免并发数据竞争
        self.progress_bar.resume()
        self.progress_bar.reset()
        self.cancel_button.show()

        if need_create_task:
            # 将当前表格状态写回原文件，保留用户的合并/删除/编辑
            if self.model._data:
                ASRData.from_json(self.model._data).to_srt(save_path=self.subtitle_path)
            self.task = TaskFactory.create_subtitle_task(file_path=self.subtitle_path)
        if not self.task:
            self.start_button.setEnabled(True)
            self.cancel_button.hide()
            return
        self.subtitle_optimization_thread = SubtitleThread(self.task)
        self.subtitle_optimization_thread.finished.connect(
            self.on_subtitle_optimization_finished
        )
        self.subtitle_optimization_thread.progress.connect(
            self.on_subtitle_optimization_progress
        )
        self.subtitle_optimization_thread.update.connect(self.update_data)
        self.subtitle_optimization_thread.update_all.connect(self.update_all)
        self.subtitle_optimization_thread.error.connect(
            self.on_subtitle_optimization_error
        )
        # 主流程实时失败反馈：逐条降级标注 + 致命错误按类型定制
        self.subtitle_optimization_thread.degraded.connect(
            self._on_processing_degraded
        )
        self.subtitle_optimization_thread.fatal.connect(
            self._on_processing_fatal
        )
        self.subtitle_optimization_thread.set_custom_prompt_text(
            self.custom_prompt_text
        )
        self.subtitle_optimization_thread.start()
        InfoBar.info(
            self.tr("开始优化"),
            self.tr("开始优化字幕"),
            duration=INFOBAR_DURATION_INFO,
            parent=self,
        )

    def process(self) -> None:
        """主处理函数"""
        # 检查是否有任务
        self.start_subtitle_optimization(need_create_task=False)

    def on_subtitle_optimization_finished(
        self, video_path: str, output_path: str
    ) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.hide()
        self.progress_bar.setValue(100)
        self._update_retry_all_button()  # 优化结束后按失败数恢复/显示重试按钮
        if self.task and self.task.need_next_task:
            self.finished.emit(video_path, output_path)
        InfoBar.success(
            self.tr("优化完成"),
            self.tr("优化完成字幕..."),
            duration=INFOBAR_DURATION_SUCCESS,
            position=InfoBarPosition.BOTTOM,
            parent=self.parent(),
        )

    def on_subtitle_optimization_error(self, error: str) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.hide()  # 隐藏取消按钮
        self.progress_bar.error()
        self._update_retry_all_button()
        InfoBar.error(
            self.tr("优化失败"),
            self.tr(error),
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    def on_subtitle_optimization_progress(self, value: int, status: str) -> None:
        self.progress_bar.setValue(value)
        self.status_label.setText(status)

    def _on_processing_degraded(self, failures) -> None:
        """主流程/重试线程实时降级反馈：把失败标注写入表格（变红 + ［失败］）。

        Args:
            failures: List[SubtitleFailure]，逐条（或汇总）失败记录。
        """
        for f in failures:
            self.model.set_failure_markers(f.indices, f.stage, message=f.message)
        self._update_retry_all_button()

    def _on_processing_fatal(self, error_text: str, fatal_type: str) -> None:
        """主流程致命错误：按 FatalErrorType 定制文案；鉴权/欠费时禁用重试。

        Args:
            error_text: 错误详情文本。
            fatal_type: FatalErrorType.value（如 "auth_quota"）。
        """
        InfoBar.error(
            self.tr("处理失败"),
            self.tr(error_text),
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )
        if fatal_type == FatalErrorType.AUTH_QUOTA.value:
            # 403/欠费/余额不足：重试无意义，禁用全部重试入口
            self._retry_disabled_by_auth = True
            self.retry_all_button.hide()
            InfoBar.warning(
                self.tr("无法重试"),
                self.tr("检测到鉴权/欠费错误，请检查 API 配置后重新加载字幕"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )

    def update_data(self, data):
        self.model.update_data(data)
        self._update_retry_all_button()

    def update_all(self, data):
        self.model.update_all(data)
        self._update_retry_all_button()

    def _update_retry_all_button(self) -> None:
        """根据当前失败总数刷新「重试全部失败项(N)」按钮文案与可见性。

        鉴权/欠费致命错误后（`_retry_disabled_by_auth`）强制隐藏重试入口。
        """
        if self._retry_disabled_by_auth:
            self.retry_all_button.hide()
            return
        count = self.model.count_failures()
        if count > 0:
            self.retry_all_button.setText(self.tr("重试全部失败项(%d)" % count))
            self.retry_all_button.setToolTip(self._build_retry_tooltip())
            self.retry_all_button.show()
            self.retry_all_button.setEnabled(not self._is_processing())
        else:
            self.retry_all_button.hide()

    def _build_retry_tooltip(self) -> str:
        """构造「重试全部失败项」按钮的悬浮提示：按阶段列出失败条数。

        阶段顺序固定为 断句→优化→翻译；某阶段无失败则不列。
        """
        failures = self.model.collect_failures()
        stage_labels = {
            ProcessStage.SPLIT: self.tr("断句"),
            ProcessStage.OPTIMIZE: self.tr("优化"),
            ProcessStage.TRANSLATE: self.tr("翻译"),
        }
        parts = []
        for stage in (ProcessStage.SPLIT, ProcessStage.OPTIMIZE, ProcessStage.TRANSLATE):
            indices = failures.get(stage)
            if indices:
                preview = ", ".join(str(i) for i in indices[:8])
                more = self.tr(" …等共 %d 条") % len(indices) if len(indices) > 8 else ""
                parts.append(
                    self.tr("· %s失败 %d 条（第 %s%s）")
                    % (stage_labels[stage], len(indices), preview, more)
                )
        tip = self.tr("重试全部失败项：重新请求所有失败条目。\n") + "\n".join(parts)
        tip += "\n" + self.tr("（也可右键单个失败行「重试此条」，仅重发该条指定阶段）")
        return tip

    def retry_all_failed(self) -> None:
        """「重试全部失败项」入口：收集各阶段失败 index 并发出 retry_requested 信号。

        本次只做 UI 入口 + 信号，实际重试由 T03/T05 的 RetryThread 承接并接线
        `retry_requested` 信号。批量重试建议强制串行或小并发 + 批间退避（见架构 §4.5）。
        """
        if self._is_processing():
            return
        failures = self.model.collect_failures()
        if not failures:
            InfoBar.info(
                self.tr("无需重试"),
                self.tr("当前没有失败项"),
                duration=INFOBAR_DURATION_INFO,
                parent=self,
            )
            return
        # 置灰，避免并发数据竞争；重试线程完成回填后由 T05 恢复
        self.retry_all_button.setEnabled(False)
        total = sum(len(indices) for indices in failures.values())
        InfoBar.info(
            self.tr("开始重试"),
            self.tr("正在重试 %d 项失败字幕..." % total),
            duration=INFOBAR_DURATION_INFO,
            parent=self,
        )
        # indices 为空列表表示「全部失败项」，stage 传 None 表示按 collect_failures 分类处理
        self.retry_requested.emit([], None)

    def retry_single(self, index: int, stage: ProcessStage) -> None:
        """右键「重试此条」入口：重发单条字幕的指定失败阶段。

        本次只做 UI 入口 + 信号，实际重试由 T03/T05 的 RetryThread 承接并接线
        `retry_requested` 信号。
        """
        if self._is_processing():
            return
        self.retry_all_button.setEnabled(False)
        stage_name = stage.value if isinstance(stage, ProcessStage) else str(stage)
        InfoBar.info(
            self.tr("开始重试"),
            self.tr("正在重试第 %d 条字幕（%s）..." % (index, stage_name)),
            duration=INFOBAR_DURATION_INFO,
            parent=self,
        )
        self.retry_requested.emit([index], stage)

    def remove_widget(self) -> None:
        """隐藏顶部开始按钮和底部进度条"""
        self.start_button.hide()
        for i in range(self.bottom_layout.count()):
            item = self.bottom_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget:
                    widget.hide()

    def on_file_select(self) -> None:
        # 构建文件过滤器
        subtitle_formats = " ".join(
            f"*.{fmt.value}" for fmt in SupportedSubtitleFormats
        )
        filter_str = f"{self.tr('字幕文件')} ({subtitle_formats})"

        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择字幕文件"), "", filter_str
        )
        if file_path:
            self.subtitle_path = file_path
            self.load_subtitle_file(file_path)

    def on_save_format_clicked(self, format: str) -> None:
        """处理保存格式的选择"""
        if not self.subtitle_path:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("请先加载字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return

        # 获取保存路径
        default_name = Path(self.subtitle_path).stem
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("保存字幕文件"),
            default_name,  # 使用原文件名作为默认名
            f"{self.tr('字幕文件')} (*.{format})",
        )
        if not file_path:
            return

        try:
            # 转换并保存字幕
            asr_data = ASRData.from_json(self.model._data)
            layout = cfg.subtitle_layout.value

            if file_path.endswith(".ass"):
                style_str = get_subtitle_style(cfg.subtitle_style_name.value)
                asr_data.to_ass(style_str, layout, file_path)
            else:
                asr_data.save(file_path, layout=layout)
            InfoBar.success(
                self.tr("保存成功"),
                self.tr("字幕已保存至:") + file_path,
                duration=INFOBAR_DURATION_SUCCESS,
                parent=self,
            )
            reveal_in_explorer(file_path)
        except Exception as e:
            InfoBar.error(
                self.tr("保存失败"),
                self.tr("保存字幕文件失败: ") + str(e),
                duration=INFOBAR_DURATION_ERROR,
                parent=self,
            )

    def on_open_folder_clicked(self) -> None:
        """打开文件夹按钮点击事件"""
        if not self.task:
            InfoBar.warning(
                self.tr("警告"),
                self.tr("请先加载字幕文件"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return
        if not self.task:
            return
        if self.task.output_path:
            output_path = Path(self.task.output_path)
            target_dir = str(
                output_path.parent
                if output_path.exists()
                else Path(self.task.subtitle_path).parent
            )
        else:
            target_dir = str(Path(self.task.subtitle_path).parent)
        open_folder(target_dir)

    def load_subtitle_file(self, file_path: str) -> None:
        self.subtitle_path = file_path
        asr_data = ASRData.from_subtitle_file(file_path)
        self.model._data = asr_data.to_json()
        self.model.layoutChanged.emit()
        self.status_label.setText(self.tr("已加载文件"))
        self._retry_disabled_by_auth = False  # 新文件恢复重试能力
        self._update_retry_all_button()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        event.accept() if event.mimeData().hasUrls() else event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        for file_path in files:
            if not os.path.isfile(file_path):
                continue

            file_ext = os.path.splitext(file_path)[1][1:].lower()

            # 检查文件格式是否支持
            supported_formats = {fmt.value for fmt in SupportedSubtitleFormats}
            is_supported = file_ext in supported_formats

            if is_supported:
                self.load_subtitle_file(file_path)
                InfoBar.success(
                    self.tr("导入成功"),
                    self.tr("成功导入") + os.path.basename(file_path),
                    duration=INFOBAR_DURATION_SUCCESS,
                    position=InfoBarPosition.BOTTOM,
                    parent=self,
                )
                break
            else:
                InfoBar.error(
                    self.tr("格式错误") + file_ext,
                    self.tr("支持的字幕格式:") + str(supported_formats),
                    duration=INFOBAR_DURATION_ERROR,
                    parent=self,
                )
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()  # type: ignore
        super().closeEvent(event)

    def show_subtitle_settings(self) -> None:
        """显示字幕设置对话框"""
        dialog = SubtitleSettingDialog(self.window())
        dialog.exec_()

    def show_video_player(self) -> None:
        """显示视频播放器窗口"""
        # 创建视频播放器窗口（延迟导入，因为vlc是可选依赖）
        from videocaptioner.ui.components.MyVideoWidget import MyVideoWidget

        self.video_player = MyVideoWidget()
        self.video_player.resize(800, 600)

        def signal_update() -> None:
            if not self.model._data:
                return
            ass_style_name = cfg.subtitle_style_name.value
            ass_style_path = SUBTITLE_STYLE_PATH / f"{ass_style_name}.txt"
            if ass_style_path.exists():
                subtitle_style_srt = ass_style_path.read_text(encoding="utf-8")
            else:
                subtitle_style_srt = None
            temp_srt_path = os.path.join(tempfile.gettempdir(), "temp_subtitle.ass")
            asr_data = ASRData.from_json(self.model._data)
            asr_data.save(
                temp_srt_path,
                layout=cfg.subtitle_layout.value,
                ass_style=subtitle_style_srt or "",
            )
            signalBus.add_subtitle(temp_srt_path)

        # 如果有字幕文件,则添加字幕
        signal_update()

        signalBus.subtitle_layout_changed.connect(signal_update)
        self.model.dataChanged.connect(signal_update)
        self.model.layoutChanged.connect(signal_update)

        # 如果有关联的视频文件,则自动加载
        # Note: SubtitleTask doesn't have file_path attribute
        # if self.task and hasattr(self.task, "file_path") and self.task.file_path:
        #     self.video_player.setVideo(QUrl.fromLocalFile(self.task.file_path))

        self.video_player.show()
        self.video_player.play()

    def on_subtitle_clicked(self, index: QModelIndex) -> None:
        row = index.row()
        item = list(self.model._data.values())[row]
        start_time = item["start_time"]  # 毫秒
        end_time = (
            item["end_time"] - 50
            if item["end_time"] - 50 > start_time
            else item["end_time"]
        )
        signalBus.play_video_segment(start_time, end_time)

    def show_context_menu(self, pos) -> None:
        """显示右键菜单"""
        menu = RoundMenu(parent=self)

        # 获取选中的行
        indexes = self.subtitle_table.selectedIndexes()
        if not indexes:
            return

        # 获取唯一的行号
        rows = sorted(set(index.row() for index in indexes))
        if not rows:
            return

        # 添加菜单项
        merge_action = Action(FIF.LINK, self.tr("合并"))
        delete_action = Action(FIF.DELETE, self.tr("删除"))
        retranslate_action = Action(FIF.SYNC, self.tr("重新翻译"))
        menu.addAction(merge_action)
        menu.addAction(delete_action)
        menu.addAction(retranslate_action)
        merge_action.setShortcut("Ctrl+M")
        delete_action.setShortcut("Delete")
        retranslate_action.setShortcut("Ctrl+T")

        merge_action.setEnabled(len(rows) > 1)
        retranslate_action.setEnabled(cfg.need_translate.value and not self._is_processing())

        merge_action.triggered.connect(lambda: self.merge_selected_rows(rows))
        delete_action.triggered.connect(lambda: self.delete_selected_rows(rows))
        retranslate_action.triggered.connect(lambda: self.retranslate_selected_rows(rows))

        # 失败行的「重试此条」入口：按该行 failed_stages 决定显示哪些阶段重试
        if len(rows) == 1:
            self._add_retry_actions_for_row(menu, rows[0])

        # 显示菜单
        menu.exec(self.subtitle_table.viewport().mapToGlobal(pos))

    def _add_retry_actions_for_row(self, menu: RoundMenu, row: int) -> None:
        """对失败行增加「重试此条(优化)」/「重试此条(翻译)」菜单项。

        按该行 failed_stages 决定显示；点击走 retry_single 骨架（待 T05 接 RetryThread）。
        """
        keys = list(self.model._data.keys())
        if row >= len(keys):
            return
        segment = self.model._data.get(keys[row])
        if not segment:
            return

        failed_stages = set(segment.get("failed_stages") or [])
        if not failed_stages or self._is_processing():
            return

        menu.addSeparator()
        stage_map = {
            ProcessStage.OPTIMIZE.value: (self.tr("重试此条(优化)"), ProcessStage.OPTIMIZE),
            ProcessStage.TRANSLATE.value: (self.tr("重试此条(翻译)"), ProcessStage.TRANSLATE),
            ProcessStage.SPLIT.value: (self.tr("重试此条(断句)"), ProcessStage.SPLIT),
        }
        for stage_value, (label, stage) in stage_map.items():
            if stage_value in failed_stages:
                retry_action = Action(FIF.SYNC, label)
                # 用默认参数捕获当前 stage，避免闭包陷阱
                retry_action.triggered.connect(
                    lambda checked=False, idx=keys[row], st=stage: self.retry_single(int(idx), st)
                )
                menu.addAction(retry_action)

    def merge_selected_rows(self, rows: List[int]) -> None:
        """合并选中的字幕行"""
        if not rows or len(rows) < 2:
            return

        # 获取选中行的数据
        data = self.model._data
        data_list = list(data.values())

        # 获取第一行和最后一行的时间戳
        first_row = data_list[rows[0]]
        last_row = data_list[rows[-1]]
        start_time = first_row["start_time"]
        end_time = last_row["end_time"]

        # 合并字幕内容
        original_subtitles = []
        translated_subtitles = []
        for row in rows:
            item = data_list[row]
            original_subtitles.append(item["original_subtitle"])
            translated_subtitles.append(item["translated_subtitle"])

        merged_original = " ".join(original_subtitles)
        merged_translated = " ".join(translated_subtitles)

        # 创建新的合并后的字幕项
        merged_item = {
            "start_time": start_time,
            "end_time": end_time,
            "original_subtitle": merged_original,
            "translated_subtitle": merged_translated,
        }

        # 获取所有需要保留的键
        keys = list(data.keys())
        preserved_keys = keys[: rows[0]] + keys[rows[-1] + 1 :]

        # 创建新的数据字典
        new_data = {}
        for i, key in enumerate(preserved_keys):
            if i == rows[0]:
                new_key = f"{len(new_data) + 1}"
                new_data[new_key] = merged_item
            new_key = f"{len(new_data) + 1}"
            new_data[new_key] = data[key]

        # 如果合并的是最后几行，需要确保合并项被添加
        if rows[0] >= len(preserved_keys):
            new_key = f"{len(new_data) + 1}"
            new_data[new_key] = merged_item

        # 更新模型数据
        self.subtitle_table.clearSelection()
        self.model.update_all(new_data)

        # 显示成功提示
        InfoBar.success(
            self.tr("合并成功"),
            self.tr("已成功合并选中的字幕行"),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def delete_selected_rows(self, rows: List[int]) -> None:
        """删除选中的字幕行"""
        if not rows:
            return

        data = self.model._data
        keys = list(data.keys())
        rows_set = set(rows)

        new_data = {}
        for i, key in enumerate(keys):
            if i not in rows_set:
                new_key = f"{len(new_data) + 1}"
                new_data[new_key] = data[key]

        self.subtitle_table.clearSelection()
        self.model.update_all(new_data)

    def _is_processing(self) -> bool:
        """是否有任何处理任务正在运行"""
        if hasattr(self, "subtitle_optimization_thread") and self.subtitle_optimization_thread.isRunning():  # type: ignore
            return True
        if hasattr(self, "_retranslate_thread") and self._retranslate_thread.isRunning():
            return True
        # 重试线程也要算作处理中，否则重试期间还能再次触发重试/开始
        if hasattr(self, "_retry_thread") and self._retry_thread.isRunning():
            return True
        return False

    def retranslate_selected_rows(self, rows: List[int]) -> None:
        """重新翻译选中的字幕行"""
        if not rows or not self.model._data:
            return
        if self._is_processing():
            return

        # 提取选中行数据，保留原始键名（行号字符串）
        all_keys = list(self.model._data.keys())
        selected_data = {all_keys[row]: self.model._data[all_keys[row]] for row in rows}

        # 获取当前翻译配置
        subtitle_task = TaskFactory.create_subtitle_task(
            file_path=self.subtitle_path or ""
        )
        config = subtitle_task.subtitle_config
        if not config:
            return

        self.start_button.setEnabled(False)
        self.status_label.setText(self.tr("正在重新翻译..."))
        self.progress_bar.resume()
        self.progress_bar.reset()

        file_name = Path(self.subtitle_path).name if self.subtitle_path else ""
        self._retranslate_thread = RetranslateThread(selected_data, config, file_name)
        self._retranslate_thread.finished.connect(self._on_retranslate_finished)
        self._retranslate_thread.progress.connect(self.on_subtitle_optimization_progress)
        self._retranslate_thread.error.connect(self._on_retranslate_error)
        self._retranslate_thread.start()

    def _on_retranslate_finished(self, result: dict) -> None:
        self.start_button.setEnabled(True)
        self.model.update_data(result)
        self.progress_bar.setValue(100)
        self.status_label.setText(self.tr("重新翻译完成"))
        self._update_retry_all_button()
        InfoBar.success(
            self.tr("翻译完成"),
            self.tr("已更新选中行的翻译"),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def _on_retranslate_error(self, error: str) -> None:
        self.start_button.setEnabled(True)
        self.progress_bar.error()
        self.status_label.setText(self.tr("重新翻译失败"))
        self._update_retry_all_button()
        InfoBar.error(
            self.tr("翻译失败"),
            error,
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )

    # ---- 手动重试（T05 接线）：retry_requested → RetryThread → 回填 ----

    def _on_retry_requested(
        self, indices: List[int], stage: Optional[ProcessStage]
    ) -> None:
        """接收 retry_requested 信号，组装失败集合并 spawn RetryThread。

        Args:
            indices: 待重试的 1-based index 列表；空列表 + stage=None 表示「全部失败项」。
            stage: 指定阶段（单条重试）；None 表示按 collect_failures 分类批量处理。
        """
        if self._is_processing() or not self.model._data:
            return
        # 鉴权/欠费禁用后不再发起重试
        if self._retry_disabled_by_auth:
            InfoBar.warning(
                self.tr("无法重试"),
                self.tr("检测到鉴权/欠费错误，请检查 API 配置后重新加载字幕"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
            return

        # 组装 failures：单条重试（stage 不为 None）用 {stage: indices}；
        # 批量重试全部失败项（indices 为空 + stage None）用 collect_failures 结果。
        if stage is not None:
            failures: Dict[ProcessStage, List[int]] = {stage: list(indices)}
        else:
            failures = self.model.collect_failures()

        if not failures:
            InfoBar.info(
                self.tr("无需重试"),
                self.tr("没有可重试的失败项"),
                duration=INFOBAR_DURATION_INFO,
                parent=self,
            )
            self._update_retry_all_button()
            return

        # 重建完整 asr_data（retry_* 就地更新成功/失败状态后原样返回）
        asr_data = ASRData.from_json(self.model._data)

        # 取配置：与 retranslate 一致，从 TaskFactory 重建当前任务配置
        subtitle_task = TaskFactory.create_subtitle_task(
            file_path=self.subtitle_path or ""
        )
        config = subtitle_task.subtitle_config
        if not config:
            return

        # 置灰所有触发入口，避免并发数据竞争
        self.start_button.setEnabled(False)
        self.retry_all_button.setEnabled(False)
        self.progress_bar.resume()
        self.progress_bar.reset()
        self.status_label.setText(self.tr("正在重试..."))

        file_name = Path(self.subtitle_path).name if self.subtitle_path else ""
        # 单条重试（stage 明确且单 index）即时串行；批量交由 RetryThread 内部串行
        is_single = stage is not None and len(indices) == 1
        self._retry_thread = RetryThread(
            asr_data,
            failures,
            config,
            file_name=file_name,
            custom_prompt=self.custom_prompt_text,
            thread_num=1 if is_single else None,
        )
        self._retry_thread.progress.connect(self.on_subtitle_optimization_progress)
        self._retry_thread.finished.connect(self._on_retry_finished)
        self._retry_thread.degraded.connect(self._on_processing_degraded)
        self._retry_thread.error.connect(self._on_retry_error)
        self._retry_thread.start()

    def _on_retry_finished(self, result: dict) -> None:
        """重试线程完成：整行回填（含失败状态清除/保留）并恢复 UI。"""
        # 用完整 to_json 行覆盖，成功项 failed_stages 清空、仍失败项保留标注
        self.model.update_rows_full(result)
        self.start_button.setEnabled(True)
        self.cancel_button.hide()
        self.progress_bar.setValue(100)
        self.status_label.setText(self.tr("重试完成"))
        self._update_retry_all_button()
        InfoBar.success(
            self.tr("重试完成"),
            self.tr("重试完成，已更新 %d 项" % len(result)),
            duration=INFOBAR_DURATION_SUCCESS,
            parent=self,
        )

    def _on_retry_error(self, error_text: str, fatal_type: str) -> None:
        """重试线程致命失败：弹错、恢复入口；鉴权/欠费时禁用重试。"""
        self.start_button.setEnabled(True)
        self.progress_bar.error()
        self.status_label.setText(self.tr("重试失败"))
        InfoBar.error(
            self.tr("重试失败"),
            self.tr(error_text),
            duration=INFOBAR_DURATION_ERROR,
            parent=self,
        )
        if fatal_type == FatalErrorType.AUTH_QUOTA.value:
            self._retry_disabled_by_auth = True
            self.retry_all_button.hide()
            InfoBar.warning(
                self.tr("无法重试"),
                self.tr("检测到鉴权/欠费错误，请检查 API 配置后重新加载字幕"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )
        else:
            # 非鉴权错误：按剩余失败数恢复重试入口（可能还会再试）
            self._update_retry_all_button()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """处理键盘事件"""
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_M:  # type: ignore
            indexes = self.subtitle_table.selectedIndexes()
            if indexes:
                rows = sorted(set(index.row() for index in indexes))
                if len(rows) > 1:
                    self.merge_selected_rows(rows)
            event.accept()
        elif event.key() == Qt.Key_Delete:  # type: ignore
            indexes = self.subtitle_table.selectedIndexes()
            if indexes:
                rows = sorted(set(index.row() for index in indexes))
                self.delete_selected_rows(rows)
            event.accept()
        elif event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_T:  # type: ignore
            if cfg.need_translate.value and not self._is_processing():
                indexes = self.subtitle_table.selectedIndexes()
                if indexes:
                    rows = sorted(set(index.row() for index in indexes))
                    self.retranslate_selected_rows(rows)
            event.accept()
        else:
            super().keyPressEvent(event)

    def cancel_optimization(self) -> None:
        """取消字幕校正"""
        if hasattr(self, "subtitle_optimization_thread"):
            self.subtitle_optimization_thread.stop()  # type: ignore
            self.start_button.setEnabled(True)
            self.cancel_button.hide()
            self.progress_bar.resume()  # 恢复正常状态
            self.progress_bar.setValue(0)
            self.status_label.setText(self.tr("已取消校正"))
            InfoBar.warning(
                self.tr("已取消"),
                self.tr("字幕校正已取消"),
                duration=INFOBAR_DURATION_WARNING,
                parent=self,
            )

    def on_target_language_changed(self, language: str) -> None:
        """处理翻译语言变更"""
        for lang in TargetLanguage:
            if lang.value == language:
                self.target_language_button.setText(lang.value)
                cfg.set(cfg.target_language, lang)
                break

    def on_subtitle_optimization_changed(self, checked: bool) -> None:
        """处理字幕优化开关变更"""
        cfg.set(cfg.need_optimize, checked)
        self.optimize_button.setChecked(checked)

    def on_subtitle_translation_changed(self, checked: bool) -> None:
        """处理字幕翻译开关变更"""
        cfg.set(cfg.need_translate, checked)
        self.translate_button.setChecked(checked)
        # 控制翻译语言选择按钮的启用状态
        self.target_language_button.setEnabled(checked)

    def on_subtitle_layout_changed(self, layout: str) -> None:
        """处理字幕排布变更"""
        layout_enum = SubtitleLayoutEnum(layout)  # Convert string to enum
        cfg.set(cfg.subtitle_layout, layout_enum)
        self.layout_button.setText(layout)


class PromptDialog(MessageBoxBase):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setup_ui()
        self.setWindowTitle(self.tr("文稿提示"))
        # 连接按钮点击事件
        self.yesButton.clicked.connect(self.save_prompt)

    def setup_ui(self) -> None:
        self.titleLabel = BodyLabel(self.tr("文稿提示"), self)

        # 添加文本编辑框
        self.text_edit = TextEdit(self)
        self.text_edit.setPlaceholderText(
            self.tr(
                "请输入文稿提示（辅助校正字幕和翻译）\n\n"
                "支持以下内容:\n"
                "1. 术语表 - 专业术语、人名、特定词语的修正对照表\n"
                "示例:\n机器学习->Machine Learning\n马斯克->Elon Musk\n打call->应援\n\n"
                "2. 原字幕文稿 - 视频的原有文稿或相关内容\n"
                "示例: 完整的演讲稿、课程讲义等\n\n"
                "3. 修正要求 - 内容相关的具体修正要求\n"
                "示例: 统一人称代词、规范专业术语等\n\n"
                "注意: 使用小型LLM模型时建议控制文稿在1千字内。对于不同字幕文件,请使用与该字幕相关的文稿提示。"
            )
        )
        self.text_edit.setText(cfg.custom_prompt_text.value)

        self.text_edit.setMinimumWidth(420)
        self.text_edit.setMinimumHeight(380)

        # 添加到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.text_edit)
        self.viewLayout.setSpacing(10)

        # 设置按钮文本
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))

    def get_prompt(self) -> str:
        return self.text_edit.toPlainText()

    def save_prompt(self) -> None:
        # 在点击确定按钮时保存提示文本到配置
        prompt_text = self.text_edit.toPlainText()
        cfg.set(cfg.custom_prompt_text, prompt_text)


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough  # type: ignore
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)  # type: ignore
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)  # type: ignore

    app = QApplication(sys.argv)
    window = SubtitleInterface()
    window.show()
    sys.exit(app.exec_())
