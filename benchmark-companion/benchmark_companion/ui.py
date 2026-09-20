from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .cases import list_prompt_sheets, load_cases
from .config import AppConfig
from .excel_sync import ExcelResultsSync
from .hotkeys import GlobalHotkeys
from .models import (
    ModelMode,
    RunRecord,
    RunStatus,
    SessionInfo,
    TaskItem,
    TokenStats,
    TokenStatus,
    expand_cases,
)
from .stats import query_token_stats
from .storage import BenchmarkStore, new_run_record, now_iso
from .workers import FunctionWorker


LOGGER = logging.getLogger(__name__)


def format_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remaining = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:05.2f}"
    return f"{minutes:02d}:{remaining:05.2f}"


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Benchmark Companion 设置")
        self.setModal(True)
        self.resize(620, 260)

        self.workbook_edit = QLineEdit(config.workbook_path)
        self.script_edit = QLineEdit(config.stats_script_path)
        self.token_edit = QLineEdit(config.token_name)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 30.0)
        self.delay_spin.setDecimals(1)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setSuffix(" 秒")
        self.delay_spin.setValue(config.stats_settle_seconds)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Benchmark 工作簿", self._path_row(self.workbook_edit, "workbook"))
        form.addRow("NewAPI 统计脚本", self._path_row(self.script_edit, "script"))
        form.addRow("TokenName", self.token_edit)
        form.addRow("统计等待时间", self.delay_spin)

        hint = QLabel("设置变更只影响之后创建的批次；已保存记录仍使用创建时的路径和 TokenName。")
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")

        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        save_button = QPushButton("保存")
        save_button.setObjectName("primaryButton")
        save_button.clicked.connect(self._validate_and_accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addStretch(1)
        layout.addLayout(buttons)

    def _path_row(self, edit: QLineEdit, kind: str) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        browse = QPushButton("浏览…")
        browse.clicked.connect(lambda: self._browse(edit, kind))
        layout.addWidget(edit, 1)
        layout.addWidget(browse)
        return widget

    def _browse(self, edit: QLineEdit, kind: str) -> None:
        if kind == "workbook":
            path, _ = QFileDialog.getOpenFileName(
                self, "选择 Benchmark 工作簿", edit.text(), "Excel 工作簿 (*.xlsx *.xlsm)"
            )
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择 NewAPI 统计脚本", edit.text(), "PowerShell 脚本 (*.ps1)"
            )
        if path:
            edit.setText(path)

    def _validate_and_accept(self) -> None:
        workbook = Path(self.workbook_edit.text().strip())
        script = Path(self.script_edit.text().strip())
        token_name = self.token_edit.text().strip()
        if not workbook.exists():
            QMessageBox.warning(self, "路径无效", f"找不到工作簿：\n{workbook}")
            return
        if not script.exists():
            QMessageBox.warning(self, "路径无效", f"找不到统计脚本：\n{script}")
            return
        if not token_name:
            QMessageBox.warning(self, "设置无效", "TokenName 不能为空")
            return
        self.accept()

    def values(self) -> dict[str, object]:
        return {
            "workbook_path": self.workbook_edit.text().strip(),
            "stats_script_path": self.script_edit.text().strip(),
            "token_name": self.token_edit.text().strip(),
            "stats_settle_seconds": float(self.delay_spin.value()),
        }


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, store: BenchmarkStore):
        super().__init__()
        self.config = config
        self.store = store
        self.session: SessionInfo | None = None
        self.current_item: TaskItem | None = None
        self.case_items: list[TaskItem] = []
        self.running = False
        self.timer_origin = 0.0
        self.start_time: str | None = None
        self.latest_record_id: str | None = None
        self._workers: set[FunctionWorker] = set()
        self._token_workers: dict[str, FunctionWorker] = {}
        self._sync_worker: FunctionWorker | None = None
        self._sync_again = False
        self._allow_close = False
        self._close_when_idle = False
        self._is_closing = False

        self.setWindowTitle("Benchmark Companion")
        self.setMinimumSize(520, 875)
        self.resize(560, 895)
        self._build_ui()
        self._apply_style()

        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(40)
        self.elapsed_timer.timeout.connect(self._update_elapsed)

        self.hotkeys = GlobalHotkeys(QApplication.instance(), self)
        self.hotkeys.start_requested.connect(self.start_run)
        self.hotkeys.success_requested.connect(lambda: self.finish_run(RunStatus.SUCCESS))
        self.hotkeys.failed_requested.connect(lambda: self.finish_run(RunStatus.FAILED))
        all_hotkeys = self.hotkeys.register()
        if all_hotkeys:
            self.hotkey_label.setText("全局快捷键已就绪：F8 开始 · F9 完成 · F10 失败")
        else:
            self.hotkey_label.setText("快捷键提示：" + "；".join(self.hotkeys.errors))

        self.always_on_top_checkbox.setChecked(config.always_on_top)
        self._apply_always_on_top(config.always_on_top, persist=False)
        self._restore_or_prepare()
        self.initial_sync_timer = QTimer(self)
        self.initial_sync_timer.setSingleShot(True)
        self.initial_sync_timer.timeout.connect(self.request_sync)
        self.initial_sync_timer.start(350)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        title_row = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("Benchmark Companion")
        title.setObjectName("title")
        subtitle = QLabel("WorkBuddy 手动测试辅助器")
        subtitle.setObjectName("mutedLabel")
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        title_row.addLayout(title_block)
        title_row.addStretch(1)
        self.always_on_top_checkbox = QCheckBox("置顶")
        self.always_on_top_checkbox.toggled.connect(self._on_always_on_top_changed)
        settings_button = QPushButton("设置")
        settings_button.clicked.connect(self.open_settings)
        title_row.addWidget(self.always_on_top_checkbox)
        title_row.addWidget(settings_button)
        outer.addLayout(title_row)

        session_card = self._card()
        session_layout = QVBoxLayout(session_card)
        session_layout.setContentsMargins(14, 12, 14, 12)
        top_controls = QHBoxLayout()
        mode_label = QLabel("新批次模型")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("NewAPI · deepseek-flash", ModelMode.NEW_API.value)
        self.mode_combo.addItem("WorkBuddy 默认模型", ModelMode.WORKBUDDY_DEFAULT.value)
        self.new_batch_button = QPushButton("开始新批次")
        self.new_batch_button.setObjectName("primaryButton")
        self.new_batch_button.clicked.connect(self.start_new_batch)
        top_controls.addWidget(mode_label)
        top_controls.addWidget(self.mode_combo, 1)
        top_controls.addWidget(self.new_batch_button)
        session_layout.addLayout(top_controls)

        sheet_controls = QHBoxLayout()
        sheet_label = QLabel("新批次 Sheet")
        self.prompt_sheet_combo = QComboBox()
        self.prompt_sheet_combo.setToolTip(
            "只显示第一行前四列为 CaseName、Prompt、Runs、Enabled 的 Sheet"
        )
        self.prompt_sheet_combo.currentIndexChanged.connect(
            self._on_prompt_sheet_changed
        )
        self.refresh_sheets_button = QPushButton("刷新")
        self.refresh_sheets_button.clicked.connect(
            lambda: self._reload_prompt_sheets(show_status=True)
        )
        sheet_controls.addWidget(sheet_label)
        sheet_controls.addWidget(self.prompt_sheet_combo, 1)
        sheet_controls.addWidget(self.refresh_sheets_button)
        session_layout.addLayout(sheet_controls)

        self.source_label = QLabel("正在读取提示词 Sheet…")
        self.source_label.setObjectName("mutedLabel")
        self.source_label.setWordWrap(True)
        session_layout.addWidget(self.source_label)
        outer.addWidget(session_card)

        task_card = self._card()
        task_card.setMinimumHeight(225)
        task_layout = QVBoxLayout(task_card)
        task_layout.setContentsMargins(16, 14, 16, 14)
        task_head = QHBoxLayout()
        self.case_label = QLabel("尚未开始")
        self.case_label.setObjectName("caseTitle")
        self.progress_label = QLabel("0 / 0")
        self.progress_label.setObjectName("mutedLabel")
        task_head.addWidget(self.case_label)
        task_head.addStretch(1)
        task_head.addWidget(self.progress_label)
        task_layout.addLayout(task_head)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(7)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        task_layout.addWidget(self.progress_bar)

        prompt_label = QLabel("提示词")
        prompt_label.setObjectName("sectionLabel")
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setReadOnly(True)
        self.prompt_edit.setPlaceholderText("开始批次后显示提示词")
        self.prompt_edit.setMinimumHeight(95)
        self.prompt_edit.setMaximumHeight(160)
        copy_button = QPushButton("复制提示词")
        copy_button.setObjectName("copyButton")
        copy_button.setFixedSize(156, 42)
        copy_button.clicked.connect(self.copy_prompt)
        self.copy_button = copy_button
        prompt_actions = QHBoxLayout()
        prompt_actions.addWidget(prompt_label)
        prompt_actions.addStretch(1)
        prompt_actions.addWidget(copy_button)
        task_layout.addLayout(prompt_actions)
        task_layout.addWidget(self.prompt_edit)
        outer.addWidget(task_card)

        timer_card = self._card()
        timer_layout = QVBoxLayout(timer_card)
        timer_layout.setContentsMargins(16, 12, 16, 14)
        self.timer_label = QLabel("00:00.00")
        self.timer_label.setObjectName("timer")
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timer_layout.addWidget(self.timer_label)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("可选备注（失败原因、异常现象等）")
        timer_layout.addWidget(self.note_edit)
        controls = QHBoxLayout()
        self.start_button = QPushButton("开始  F8")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self.start_run)
        self.success_button = QPushButton("完成  F9")
        self.success_button.setObjectName("successButton")
        self.success_button.clicked.connect(lambda: self.finish_run(RunStatus.SUCCESS))
        self.failed_button = QPushButton("失败  F10")
        self.failed_button.setObjectName("failedButton")
        self.failed_button.clicked.connect(lambda: self.finish_run(RunStatus.FAILED))
        self.skip_button = QPushButton("跳过")
        self.skip_button.clicked.connect(self.skip_run)
        controls.addWidget(self.start_button)
        controls.addWidget(self.success_button)
        controls.addWidget(self.failed_button)
        controls.addWidget(self.skip_button)
        timer_layout.addLayout(controls)
        outer.addWidget(timer_card)

        token_card = self._card()
        token_layout = QVBoxLayout(token_card)
        token_layout.setContentsMargins(16, 12, 16, 12)
        token_head = QHBoxLayout()
        token_title = QLabel("最近一条 Token 统计")
        token_title.setObjectName("sectionLabel")
        self.token_state_label = QLabel("尚无记录")
        self.token_state_label.setObjectName("mutedLabel")
        token_head.addWidget(token_title)
        token_head.addStretch(1)
        token_head.addWidget(self.token_state_label)
        token_layout.addLayout(token_head)
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(22)
        metrics.setVerticalSpacing(4)
        self.api_calls_value = self._metric(metrics, 0, 0, "API Calls")
        self.error_calls_value = self._metric(metrics, 0, 1, "Error Calls")
        self.input_tokens_value = self._metric(metrics, 0, 2, "Input Tokens")
        self.output_tokens_value = self._metric(metrics, 1, 0, "Output Tokens")
        self.total_tokens_value = self._metric(metrics, 1, 1, "Total Tokens")
        self.api_time_value = self._metric(metrics, 1, 2, "API Use Time")
        token_layout.addLayout(metrics)
        outer.addWidget(token_card)

        footer_actions = QHBoxLayout()
        self.retry_token_button = QPushButton("重新查询 Token")
        self.retry_token_button.clicked.connect(self.retry_token)
        self.sync_button = QPushButton("同步待处理结果")
        self.sync_button.clicked.connect(self.request_sync)
        footer_actions.addWidget(self.retry_token_button)
        footer_actions.addWidget(self.sync_button)
        footer_actions.addStretch(1)
        outer.addLayout(footer_actions)

        self.status_label = QLabel("准备中…")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        self.hotkey_label = QLabel("")
        self.hotkey_label.setObjectName("mutedLabel")
        outer.addWidget(self.hotkey_label)

    @staticmethod
    def _card() -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setFrameShape(QFrame.Shape.NoFrame)
        return card

    @staticmethod
    def _metric(layout: QGridLayout, row: int, column: int, title: str) -> QLabel:
        box = QVBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("metricTitle")
        value_label = QLabel("—")
        value_label.setObjectName("metricValue")
        box.addWidget(title_label)
        box.addWidget(value_label)
        layout.addLayout(box, row, column)
        return value_label

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget#root { background: #F4F6FA; color: #182230; }
            QWidget { font-size: 13px; }
            QLabel#title { font-size: 22px; font-weight: 700; color: #111827; }
            QLabel#caseTitle { font-size: 17px; font-weight: 700; color: #111827; }
            QLabel#sectionLabel { font-weight: 650; color: #334155; }
            QLabel#mutedLabel { color: #64748B; font-size: 12px; }
            QFrame#card { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 10px; }
            QLineEdit, QTextEdit, QComboBox, QDoubleSpinBox {
                background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 7px;
                padding: 7px; selection-background-color: #2563EB;
            }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border: 1px solid #2563EB; }
            QPushButton {
                background: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 7px;
                padding: 8px 12px; color: #253247; font-weight: 600;
            }
            QPushButton:hover { background: #F8FAFC; border-color: #94A3B8; }
            QPushButton:disabled { color: #94A3B8; background: #F8FAFC; border-color: #E2E8F0; }
            QPushButton#primaryButton, QPushButton#startButton {
                background: #2563EB; color: white; border-color: #2563EB;
            }
            QPushButton#primaryButton:hover, QPushButton#startButton:hover { background: #1D4ED8; }
            QPushButton#successButton { background: #15803D; color: white; border-color: #15803D; }
            QPushButton#successButton:hover { background: #166534; }
            QPushButton#failedButton { background: #B91C1C; color: white; border-color: #B91C1C; }
            QPushButton#failedButton:hover { background: #991B1B; }
            QLabel#timer { font-family: "Cascadia Mono", Consolas; font-size: 37px; font-weight: 700; color: #0F172A; }
            QLabel#metricTitle { color: #64748B; font-size: 11px; }
            QLabel#metricValue { color: #0F172A; font-size: 16px; font-weight: 700; }
            QProgressBar { background: #E2E8F0; border: 0; border-radius: 3px; }
            QProgressBar::chunk { background: #2563EB; border-radius: 3px; }
            QLabel#statusLabel { padding: 9px 11px; border-radius: 7px; background: #E8F0FE; color: #1E40AF; }
            QLabel#statusLabel[level="success"] { background: #DCFCE7; color: #166534; }
            QLabel#statusLabel[level="warning"] { background: #FEF3C7; color: #92400E; }
            QLabel#statusLabel[level="error"] { background: #FEE2E2; color: #991B1B; }
            """
        )

    def _restore_or_prepare(self) -> None:
        try:
            active = self.store.get_active_session()
        except Exception as exc:
            LOGGER.exception("Failed to load active session")
            self._set_status(f"读取本地状态失败：{exc}", "error")
            self._set_controls()
            return
        preferred_sheet = (
            active.prompt_sheet if active is not None else self.config.prompt_sheet
        )
        self._reload_prompt_sheets(preferred=preferred_sheet, show_status=False)
        if active is not None:
            self.session = active
            index = self.mode_combo.findData(active.model_mode.value)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
            self.source_label.setText(
                f"已恢复批次 · Sheet: {active.prompt_sheet} · {active.workbook_path}"
            )
            self._refresh_current_item(restoring=True)
        else:
            self.refresh_cases_preview()

    def _reload_prompt_sheets(
        self, *, preferred: str | None = None, show_status: bool = False
    ) -> None:
        selected_before = (
            preferred
            or self.prompt_sheet_combo.currentData()
            or self.config.prompt_sheet
        )
        try:
            sheet_names = list_prompt_sheets(self.config.workbook)
        except Exception as exc:
            LOGGER.exception("Failed to discover prompt sheets")
            blocker = QSignalBlocker(self.prompt_sheet_combo)
            self.prompt_sheet_combo.clear()
            del blocker
            self.source_label.setText(f"提示词 Sheet 读取失败：{exc}")
            self._set_status(str(exc), "error")
            self._set_controls()
            return

        selected = selected_before if selected_before in sheet_names else (
            "Cases" if "Cases" in sheet_names else sheet_names[0]
        )
        blocker = QSignalBlocker(self.prompt_sheet_combo)
        self.prompt_sheet_combo.clear()
        for sheet_name in sheet_names:
            self.prompt_sheet_combo.addItem(sheet_name, sheet_name)
        index = self.prompt_sheet_combo.findData(selected)
        self.prompt_sheet_combo.setCurrentIndex(max(0, index))
        del blocker

        if self.config.prompt_sheet != selected:
            self.config.prompt_sheet = selected
            try:
                self.config.save()
            except OSError:
                LOGGER.exception("Failed to persist prompt sheet selection")

        if show_status:
            if self.session is not None and self.session.status == "Active":
                self._set_status(
                    f"已刷新 Sheet；下一个新批次将使用「{selected}」。"
                )
            else:
                self.refresh_cases_preview()
                return
        self._set_controls()

    def refresh_cases_preview(self) -> None:
        sheet_name = self.prompt_sheet_combo.currentData()
        if not sheet_name:
            self._reload_prompt_sheets(show_status=False)
            sheet_name = self.prompt_sheet_combo.currentData()
        if not sheet_name:
            return
        try:
            result = load_cases(self.config.workbook, sheet_name=sheet_name)
            self.case_items = expand_cases(result.cases)
        except Exception as exc:
            LOGGER.exception("Failed to read prompt sheet")
            self.case_items = []
            self.source_label.setText(f"Sheet「{sheet_name}」读取失败：{exc}")
            self._set_status(str(exc), "error")
        else:
            self.source_label.setText(
                f"Sheet: {sheet_name} · {result.enabled_count} 个启用 Case · "
                f"{result.total_runs} 次运行 · {self.config.workbook}"
            )
            self._set_status(f"Sheet「{sheet_name}」已就绪，请开始新批次。")
        self._set_controls()

    def start_new_batch(self) -> None:
        sheet_name = self.prompt_sheet_combo.currentData()
        if not sheet_name:
            self._set_status("请先选择提示词 Sheet。", "warning")
            return
        try:
            result = load_cases(self.config.workbook, sheet_name=sheet_name)
            items = expand_cases(result.cases)
            mode = ModelMode(self.mode_combo.currentData())
        except Exception as exc:
            LOGGER.exception("Failed to prepare session")
            self._set_status(f"无法读取新批次：{exc}", "error")
            return

        if self.session is not None and self.session.status == "Active":
            detail = "当前计时正在运行。" if self.running else "当前批次尚未完成。"
            answer = QMessageBox.question(
                self,
                "开始新批次",
                detail
                + f"\n将放弃当前批次，并使用「{sheet_name}」/「{mode.model_name}」开始，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.elapsed_timer.stop()
            self.running = False
            self.store.abandon_session(self.session.session_id)

        try:
            self.session = self.store.create_session(
                model_mode=mode,
                items=items,
                workbook_path=str(self.config.workbook),
                prompt_sheet=sheet_name,
                stats_script_path=str(self.config.stats_script),
                token_name=self.config.token_name,
            )
        except Exception as exc:
            LOGGER.exception("Failed to start session")
            self._set_status(f"无法创建批次：{exc}", "error")
            return

        self.case_items = items
        self.config.prompt_sheet = sheet_name
        try:
            self.config.save()
        except OSError:
            LOGGER.exception("Failed to persist prompt sheet selection")
        self.timer_label.setText("00:00.00")
        self._set_status(
            f"已从「{sheet_name}」创建 {mode.model_name} 批次，共 {len(items)} 次运行。"
        )
        self._refresh_current_item()

    def _refresh_current_item(self, restoring: bool = False) -> None:
        if self.session is None:
            self.current_item = None
            self._set_controls()
            return
        self.session = self.store.get_session(self.session.session_id)
        self.current_item = self.store.get_current_item(self.session.session_id)
        self.source_label.setText(
            f"当前批次 · Sheet: {self.session.prompt_sheet} · {self.session.workbook_path}"
        )
        self.progress_bar.setRange(0, max(1, self.session.total_items))
        self.progress_bar.setValue(self.session.current_position)
        self.progress_label.setText(
            f"{min(self.session.current_position + 1, self.session.total_items)} / {self.session.total_items}"
            if self.current_item
            else f"{self.session.total_items} / {self.session.total_items}"
        )

        if self.current_item is None:
            self.running = False
            self.elapsed_timer.stop()
            self.case_label.setText("批次完成")
            self.prompt_edit.clear()
            self.timer_label.setText("00:00.00")
            self.mode_combo.setEnabled(True)
            self._set_status("当前批次已完成，所有结果均已先保存到本地。", "success")
            self._set_controls()
            return

        runs_for_case = [
            item.run_no
            for item in self.store.get_session_items(self.session.session_id)
            if item.case_name == self.current_item.case_name
        ]
        max_run = max(runs_for_case, default=self.current_item.run_no)
        self.case_label.setText(
            f"{self.current_item.case_name} · Run {self.current_item.run_no} / {max_run}"
        )
        self.prompt_edit.setPlainText(self.current_item.prompt)
        self.note_edit.clear()

        if self.current_item.state == "Running" and self.current_item.start_time:
            self.running = True
            self.start_time = self.current_item.start_time
            started = datetime.fromisoformat(self.start_time)
            elapsed = max(0.0, (datetime.now().astimezone() - started).total_seconds())
            self.timer_origin = time.perf_counter() - elapsed
            self.elapsed_timer.start()
            self._update_elapsed()
            if restoring:
                self._set_status("已恢复上次正在计时的任务，时长按本地墙钟继续计算。", "warning")
        else:
            self.running = False
            self.start_time = None
            self.elapsed_timer.stop()
            self.timer_label.setText("00:00.00")
        self._set_controls()

    def copy_prompt(self) -> None:
        if self.current_item is None:
            self._set_status("当前没有可复制的提示词。", "warning")
            return
        QApplication.clipboard().setText(self.current_item.prompt)
        self._set_status("提示词已复制。发送到 WorkBuddy 后按 F8 开始计时。", "success")

    def start_run(self) -> None:
        if self.current_item is None:
            if self.session is None or self.session.status != "Active":
                self.start_new_batch()
            if self.current_item is None:
                return
        if self.running:
            self._set_status("当前任务已经在计时。", "warning")
            return

        self.start_time = now_iso()
        try:
            self.store.begin_item(
                self.session.session_id, self.current_item.position, self.start_time
            )
        except Exception as exc:
            LOGGER.exception("Failed to start item")
            self._set_status(f"开始计时失败：{exc}", "error")
            return
        self.current_item = TaskItem(
            position=self.current_item.position,
            case_name=self.current_item.case_name,
            run_no=self.current_item.run_no,
            prompt=self.current_item.prompt,
            state="Running",
            start_time=self.start_time,
        )
        self.timer_origin = time.perf_counter()
        self.running = True
        self.elapsed_timer.start()
        self._update_elapsed()
        self._set_status("计时中。回答完成按 F9，异常按 F10。")
        self._set_controls()

    def finish_run(self, status: RunStatus) -> None:
        if not self.running or self.current_item is None or self.session is None:
            self._set_status("当前没有正在计时的任务。", "warning")
            return
        end_time = now_iso()
        duration = max(0.0, time.perf_counter() - self.timer_origin)
        note = self.note_edit.text().strip()
        if status is RunStatus.FAILED and not note:
            note = "手动标记失败"
        record = new_run_record(
            session=self.session,
            item=self.current_item,
            status=status,
            start_time=self.start_time,
            end_time=end_time,
            duration_seconds=duration,
            note=note,
        )
        try:
            self.session = self.store.finish_item(record)
        except Exception as exc:
            LOGGER.exception("Failed to persist completed item")
            self._set_status(f"保存失败，当前计时仍保留：{exc}", "error")
            return

        finished_label = f"{record.case_name} Run {record.run_no}"
        self.latest_record_id = record.record_id
        self.running = False
        self.start_time = None
        self.elapsed_timer.stop()
        self._display_record(record)
        self._refresh_current_item()

        if record.token_status is TokenStatus.PENDING:
            self._set_status(f"{finished_label} 已保存本地，正在查询 Token。", "success")
            self._start_token_query(record)
        else:
            self._set_status(f"{finished_label} 已保存本地。", "success")
            self.request_sync()

    def skip_run(self) -> None:
        if self.current_item is None or self.session is None:
            self._set_status("当前没有可跳过的任务。", "warning")
            return
        if self.running:
            self._set_status("计时中的任务请使用“失败 F10”结束。", "warning")
            return
        record = new_run_record(
            session=self.session,
            item=self.current_item,
            status=RunStatus.SKIPPED,
            start_time=None,
            end_time=None,
            duration_seconds=None,
            note=self.note_edit.text().strip(),
        )
        try:
            self.session = self.store.finish_item(record)
        except Exception as exc:
            LOGGER.exception("Failed to skip item")
            self._set_status(f"跳过失败：{exc}", "error")
            return
        self.latest_record_id = record.record_id
        self._display_record(record)
        self._refresh_current_item()
        self._set_status(f"{record.case_name} Run {record.run_no} 已记录为 Skipped。")
        self.request_sync()

    def _start_token_query(self, record: RunRecord, retry: bool = False) -> None:
        if record.record_id in self._token_workers:
            self._set_status("该记录的 Token 正在查询中。", "warning")
            return
        if not record.start_time or not record.end_time:
            self._set_status("该记录没有完整起止时间，无法查询 Token。", "error")
            return

        if record.record_id == self.latest_record_id:
            self.token_state_label.setText("查询中…")
        settle = self.config.stats_settle_seconds
        function = lambda: query_token_stats(
            script_path=Path(record.stats_script_path),
            start_time=record.start_time,
            end_time=record.end_time,
            token_name=record.token_name,
            powershell_executable=self.config.powershell_executable,
            settle_seconds=settle,
            timeout_seconds=self.config.stats_timeout_seconds,
        )
        worker = FunctionWorker(function, self)
        self._workers.add(worker)
        self._token_workers[record.record_id] = worker
        worker.succeeded.connect(
            lambda stats, record_id=record.record_id: self._on_token_success(record_id, stats)
        )
        worker.failed.connect(
            lambda message, trace, record_id=record.record_id: self._on_token_failure(
                record_id, message, trace
            )
        )
        worker.finished.connect(lambda worker=worker: self._cleanup_worker(worker))
        worker.start()

    def _on_token_success(self, record_id: str, stats: TokenStats) -> None:
        try:
            record = self.store.update_token_stats(record_id, stats)
        except Exception as exc:
            LOGGER.exception("Failed to save token stats")
            self._set_status(f"Token 已返回，但保存失败：{exc}", "error")
            return
        if record_id == self.latest_record_id:
            self._display_record(record)
        if stats.api_calls == 0:
            self._set_status("Token 查询返回 APICalls=0，已标记 NoData，可稍后重试。", "warning")
        else:
            self._set_status(
                f"Token 查询完成：{stats.api_calls} calls，{stats.total_tokens:,} tokens。",
                "success",
            )
        self.request_sync()

    def _on_token_failure(self, record_id: str, message: str, trace: str) -> None:
        LOGGER.error("Token query failed for %s: %s\n%s", record_id, message, trace)
        try:
            record = self.store.update_token_error(record_id, message)
        except Exception:
            LOGGER.exception("Failed to persist token error")
            self._set_status(f"Token 查询失败且状态保存失败：{message}", "error")
            return
        if record_id == self.latest_record_id:
            self._display_record(record)
        self._set_status(f"Token 查询失败：{message}。可稍后重试。", "warning")
        self.request_sync()

    def retry_token(self) -> None:
        try:
            record = self.store.latest_retryable_token_record()
        except Exception as exc:
            self._set_status(f"读取待重试记录失败：{exc}", "error")
            return
        if record is None:
            self._set_status("没有需要重试的 Token 记录。")
            return
        self.latest_record_id = record.record_id
        self._display_record(record)
        self._set_status(f"正在重试 {record.case_name} Run {record.run_no} 的 Token 查询。")
        self._start_token_query(record, retry=True)

    def request_sync(self) -> None:
        if self._is_closing:
            return
        if self._sync_worker is not None and self._sync_worker.isRunning():
            self._sync_again = True
            return
        records = self.store.pending_records()
        if not records:
            pending = self.store.count_pending()
            if pending:
                self.sync_button.setText(f"同步待处理结果 ({pending})")
            else:
                self.sync_button.setText("同步待处理结果")
            return

        self.sync_button.setEnabled(False)
        self.sync_button.setText(f"同步中… ({len(records)})")
        backup_dir = self.config.backups

        def synchronize():
            grouped: dict[str, list[RunRecord]] = defaultdict(list)
            for record in records:
                grouped[record.workbook_path].append(record)
            rows: dict[str, int] = {}
            errors: list[str] = []
            backups: list[str] = []
            for workbook_path, group in grouped.items():
                try:
                    outcome = ExcelResultsSync(Path(workbook_path), backup_dir).sync_records(group)
                except Exception as exc:
                    errors.append(f"{Path(workbook_path).name}: {exc}")
                else:
                    rows.update(outcome.row_by_record)
                    if outcome.backup_path:
                        backups.append(str(outcome.backup_path))
            return {"rows": rows, "errors": errors, "backups": backups}

        worker = FunctionWorker(synchronize, self)
        self._workers.add(worker)
        self._sync_worker = worker
        worker.succeeded.connect(self._on_sync_success)
        worker.failed.connect(self._on_sync_worker_failure)
        worker.finished.connect(lambda worker=worker: self._cleanup_worker(worker))
        worker.start()

    def _on_sync_success(self, result: dict[str, object]) -> None:
        rows = result["rows"]
        errors = result["errors"]
        if rows:
            try:
                self.store.mark_synced(rows)
            except Exception as exc:
                LOGGER.exception("Failed to mark records as synced")
                self._set_status(f"Excel 已写入，但本地同步标记失败：{exc}", "error")
        self._sync_worker = None
        self.sync_button.setEnabled(True)
        pending = self.store.count_pending()
        self.sync_button.setText(
            f"同步待处理结果 ({pending})" if pending else "同步待处理结果"
        )
        if errors:
            self._set_status(f"Excel 暂未完全同步：{errors[0]}", "warning")
        elif rows:
            self._set_status(f"已同步 {len(rows)} 条结果到 Excel。", "success")
        if self._sync_again:
            self._sync_again = False
            QTimer.singleShot(100, self.request_sync)

    def _on_sync_worker_failure(self, message: str, trace: str) -> None:
        LOGGER.error("Sync worker failed: %s\n%s", message, trace)
        self._sync_worker = None
        self.sync_button.setEnabled(True)
        self.sync_button.setText(f"同步待处理结果 ({self.store.count_pending()})")
        self._set_status(f"同步失败，结果仍在本地：{message}", "warning")

    def _cleanup_worker(self, worker: FunctionWorker) -> None:
        self._workers.discard(worker)
        for record_id, candidate in list(self._token_workers.items()):
            if candidate is worker:
                self._token_workers.pop(record_id, None)
        worker.deleteLater()
        if self._close_when_idle:
            QTimer.singleShot(0, self._try_close_when_idle)

    def _display_record(self, record: RunRecord) -> None:
        self.token_state_label.setText(
            f"{record.case_name} · Run {record.run_no} · {record.token_status.value}"
        )
        if record.token_status is TokenStatus.SUCCESS:
            self.api_calls_value.setText(f"{record.api_calls:,}")
            self.error_calls_value.setText(f"{record.error_calls:,}")
            self.input_tokens_value.setText(f"{record.input_tokens:,}")
            self.output_tokens_value.setText(f"{record.output_tokens:,}")
            self.total_tokens_value.setText(f"{record.total_tokens:,}")
            self.api_time_value.setText(f"{record.api_use_time:,.3f}s")
        else:
            for label in (
                self.api_calls_value,
                self.error_calls_value,
                self.input_tokens_value,
                self.output_tokens_value,
                self.total_tokens_value,
                self.api_time_value,
            ):
                label.setText("—")
        self.retry_token_button.setEnabled(
            record.token_status in {TokenStatus.NO_DATA, TokenStatus.ERROR}
        )

    def _update_elapsed(self) -> None:
        if self.running:
            self.timer_label.setText(format_elapsed(time.perf_counter() - self.timer_origin))

    def _set_controls(self) -> None:
        has_item = self.current_item is not None
        self.copy_button.setEnabled(has_item and not self.running)
        self.start_button.setEnabled(has_item and not self.running)
        self.success_button.setEnabled(has_item and self.running)
        self.failed_button.setEnabled(has_item and self.running)
        self.skip_button.setEnabled(has_item and not self.running)
        self.new_batch_button.setEnabled(not self.running)
        self.mode_combo.setEnabled(not self.running)
        self.prompt_sheet_combo.setEnabled(
            not self.running and self.prompt_sheet_combo.count() > 0
        )
        self.refresh_sheets_button.setEnabled(not self.running)
        self.prompt_edit.setReadOnly(True)
        if self.latest_record_id is None:
            retryable = self.store.latest_retryable_token_record()
            self.retry_token_button.setEnabled(retryable is not None)

    def _on_prompt_sheet_changed(self) -> None:
        sheet_name = self.prompt_sheet_combo.currentData()
        if not sheet_name:
            return
        self.config.prompt_sheet = sheet_name
        try:
            self.config.save()
        except OSError:
            LOGGER.exception("Failed to persist prompt sheet selection")
            self._set_status("Sheet 已切换，但未能保存为下次默认值。", "warning")

        if self.session is not None and self.session.status == "Active":
            self._set_status(
                f"下一个新批次将使用 Sheet「{sheet_name}」；当前批次仍来自"
                f"「{self.session.prompt_sheet}」。"
            )
        else:
            self.refresh_cases_preview()

    def _set_status(self, text: str, level: str = "info") -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("level", level)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.config.update_from(dialog.values())
        try:
            self.config.save()
        except Exception as exc:
            self._set_status(f"设置保存失败：{exc}", "error")
            return
        self._reload_prompt_sheets(preferred=self.config.prompt_sheet, show_status=False)
        if self.session is None or self.session.status != "Active":
            self.refresh_cases_preview()
        else:
            self._set_status("设置已保存，将从下一个批次开始生效。", "success")

    def _on_always_on_top_changed(self, checked: bool) -> None:
        self._apply_always_on_top(checked, persist=True)

    def _apply_always_on_top(self, checked: bool, persist: bool) -> None:
        position = self.pos()
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.move(position)
        self.show()
        if persist:
            self.config.always_on_top = checked
            try:
                self.config.save()
            except OSError:
                LOGGER.exception("Failed to persist always-on-top setting")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._allow_close:
            self._is_closing = True
            self.initial_sync_timer.stop()
            self.hotkeys.unregister()
            event.accept()
            return
        if self.running:
            answer = QMessageBox.question(
                self,
                "计时仍在进行",
                "关闭后本次开始时间仍会保留，下次启动将按墙钟恢复计时。是否关闭？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if any(worker.isRunning() for worker in self._workers):
            self._is_closing = True
            self.initial_sync_timer.stop()
            self._close_when_idle = True
            self._set_status("正在等待后台 Token/Excel 任务安全结束，然后自动关闭。", "warning")
            QTimer.singleShot(250, self._try_close_when_idle)
            event.ignore()
            return
        self._is_closing = True
        self.initial_sync_timer.stop()
        self.hotkeys.unregister()
        event.accept()

    def _try_close_when_idle(self) -> None:
        if any(worker.isRunning() for worker in self._workers):
            QTimer.singleShot(250, self._try_close_when_idle)
            return
        self._close_when_idle = False
        self._allow_close = True
        self.close()
