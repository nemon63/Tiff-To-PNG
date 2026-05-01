from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        title_label = QLabel("Журнал пайплайна")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        subtitle_label = QLabel("Warnings, errors and export results.")
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.log_edit, 1)

    def append_line(self, line: str) -> None:
        self.log_edit.appendPlainText(line)
        scrollbar = self.log_edit.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear(self) -> None:
        self.log_edit.clear()
