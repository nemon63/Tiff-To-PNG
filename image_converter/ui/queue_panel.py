from __future__ import annotations

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.constants import FILE_DIALOG_FILTER
from image_converter.ui.common import QUEUE_HEADERS, _extract_local_paths


class QueueTableWidget(QTableWidget):
    paths_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _extract_local_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if _extract_local_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = _extract_local_paths(event)
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class QueuePanel(QWidget):
    paths_selected = pyqtSignal(list)
    paths_dropped = pyqtSignal(list)
    remove_requested = pyqtSignal()
    clear_requested = pyqtSignal()
    open_selected_set_in_graph_requested = pyqtSignal()
    open_selected_files_in_graph_requested = pyqtSignal()
    apply_graph_to_queue_requested = pyqtSignal()
    pbr_preview_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._interactive_widgets: list[QWidget] = []
        self._build_ui()

    def _build_ui(self) -> None:
        self.setObjectName("SectionPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title_label = QLabel("Очередь обработки")
        title_label.setObjectName("PanelTitle")
        layout.addWidget(title_label)

        subtitle_label = QLabel(
            "Добавьте текстуры или папки в очередь. Именно очередь задает вход для Batch Converter."
        )
        subtitle_label.setObjectName("PanelSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(6)
        self.add_files_button = QPushButton("+ Файлы")
        self.add_files_button.setToolTip("Добавить отдельные изображения в очередь.")
        self.add_files_button.clicked.connect(self._pick_files)
        controls_row.addWidget(self.add_files_button)

        self.add_folder_button = QPushButton("+ Папка")
        self.add_folder_button.setToolTip("Добавить папку и просканировать найденные в ней текстуры.")
        self.add_folder_button.clicked.connect(self._pick_folder)
        controls_row.addWidget(self.add_folder_button)

        self.remove_selected_button = QPushButton("Убрать")
        self.remove_selected_button.setToolTip("Убрать выбранные элементы из очереди.")
        self.remove_selected_button.clicked.connect(self.remove_requested.emit)
        controls_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("Очистить")
        self.clear_button.setObjectName("DangerButton")
        self.clear_button.setToolTip("Полностью очистить очередь.")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        controls_row.addWidget(self.clear_button)
        controls_row.addStretch(1)
        layout.addLayout(controls_row)

        graph_actions_row = QHBoxLayout()
        graph_actions_row.setSpacing(6)
        self.open_set_in_graph_button = QPushButton("Открыть набор в Graph")
        self.open_set_in_graph_button.setToolTip(
            "Открыть в Graph Workbench весь набор текстур из выбранной папки."
        )
        self.open_set_in_graph_button.clicked.connect(self.open_selected_set_in_graph_requested.emit)
        graph_actions_row.addWidget(self.open_set_in_graph_button)

        self.open_files_in_graph_button = QPushButton("Открыть файлы в Graph")
        self.open_files_in_graph_button.setToolTip(
            "Открыть в Graph Workbench только выбранные файлы из очереди."
        )
        self.open_files_in_graph_button.clicked.connect(self.open_selected_files_in_graph_requested.emit)
        graph_actions_row.addWidget(self.open_files_in_graph_button)

        self.apply_graph_to_queue_button = QPushButton("Применить граф к очереди")
        self.apply_graph_to_queue_button.setObjectName("PrimaryButton")
        self.apply_graph_to_queue_button.setToolTip(
            "Применить текущий graph template к каждому набору текстур в очереди."
        )
        self.apply_graph_to_queue_button.clicked.connect(self.apply_graph_to_queue_requested.emit)
        graph_actions_row.addWidget(self.apply_graph_to_queue_button)

        self.pbr_preview_button = QPushButton("PBR Preview")
        self.pbr_preview_button.setToolTip(
            "Показать выбранный Texture Set на PBR-сфере или плоскости."
        )
        self.pbr_preview_button.clicked.connect(self.pbr_preview_requested.emit)
        graph_actions_row.addWidget(self.pbr_preview_button)
        layout.addLayout(graph_actions_row)

        self.graph_apply_preflight_label = QLabel(
            "Graph template пока не задан. Откройте набор в Graph, чтобы увидеть какие наборы очереди подойдут."
        )
        self.graph_apply_preflight_label.setObjectName("SummaryText")
        self.graph_apply_preflight_label.setWordWrap(True)
        layout.addWidget(self.graph_apply_preflight_label)

        self.drop_hint = QLabel("Перетащите сюда файлы или папки с текстурами.")
        self.drop_hint.setObjectName("DropHint")
        self.drop_hint.setWordWrap(True)
        layout.addWidget(self.drop_hint)

        self.summary_label = QLabel("Очередь пуста. Добавьте файлы или папки.")
        self.summary_label.setObjectName("SummaryText")
        layout.addWidget(self.summary_label)

        self.texture_set_validation_label = QLabel(
            "Texture Set Validator: добавьте распознанные PBR-карты."
        )
        self.texture_set_validation_label.setObjectName("SummaryText")
        self.texture_set_validation_label.setWordWrap(True)
        layout.addWidget(self.texture_set_validation_label)

        self.table = QueueTableWidget()
        self.table.setColumnCount(len(QUEUE_HEADERS))
        self.table.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.verticalHeader().setVisible(False)
        header_tooltips = (
            "Имя исходного файла.",
            "Определенный тип карты.",
            "Размер файла на диске.",
            "Разрешение исходной текстуры.",
            "Готовность элемента к обработке.",
            "Ожидаемый результат. В режиме 'Только Packed' показывает итоговую packed texture, в которую войдет карта.",
        )
        for column, tooltip in enumerate(header_tooltips):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setToolTip(tooltip)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 150)
        header.resizeSection(1, 90)
        header.resizeSection(2, 80)
        header.resizeSection(3, 90)
        header.resizeSection(4, 150)
        header.resizeSection(5, 180)
        self.table.paths_dropped.connect(self.paths_dropped.emit)
        layout.addWidget(self.table, 1)

        self._register_interactive(
            self.add_files_button,
            self.add_folder_button,
            self.remove_selected_button,
            self.clear_button,
            self.open_set_in_graph_button,
            self.open_files_in_graph_button,
            self.apply_graph_to_queue_button,
            self.pbr_preview_button,
            self.table,
        )

    def _register_interactive(self, *widgets: QWidget) -> None:
        self._interactive_widgets.extend(widgets)

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выберите изображения", "", FILE_DIALOG_FILTER)
        if paths:
            self.paths_selected.emit(paths)

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку с текстурами")
        if path:
            self.paths_selected.emit([path])

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in self._interactive_widgets:
            widget.setEnabled(enabled)

    def set_graph_apply_preflight_summary(self, text: str) -> None:
        self.graph_apply_preflight_label.setText(text)

    def set_texture_set_validation_summary(self, text: str) -> None:
        self.texture_set_validation_label.setText(text)

    def minimumSizeHint(self) -> QSize:
        return QSize(240, 180)

    def sizeHint(self) -> QSize:
        return QSize(480, 360)
