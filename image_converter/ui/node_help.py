from __future__ import annotations

from dataclasses import dataclass
from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from image_converter.domain.node_graph import NodeType


NODE_HELP_DEFAULT_WIDTH = 500
NODE_HELP_DEFAULT_HEIGHT = 400
NODE_HELP_MIN_WIDTH = 440
NODE_HELP_MIN_HEIGHT = 330
NODE_HELP_AUTO_CLOSE_DELAY_MS = 350
NODE_HELP_CURSOR_POLL_MS = 100
NODE_HELP_ANCHOR_GAP = 8


def _popup_button_icon(kind: str) -> QIcon:
    icon = QIcon()
    for mode, color in (
        (QIcon.Mode.Normal, QColor("#CAD6E2")),
        (QIcon.Mode.Active, QColor("#FFFFFF")),
        (QIcon.Mode.Selected, QColor("#FFFFFF")),
    ):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(color, 1.5))
        if kind == "pin":
            painter.drawLine(5, 3, 11, 3)
            painter.drawLine(6, 4, 6, 8)
            painter.drawLine(10, 4, 10, 8)
            painter.drawLine(4, 8, 12, 8)
            painter.drawLine(8, 8, 8, 14)
        else:
            painter.drawLine(4, 4, 12, 12)
            painter.drawLine(12, 4, 4, 12)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


@dataclass(slots=True, frozen=True)
class NodeHelpContent:
    title: str
    summary: str
    details: tuple[tuple[str, str], ...]
    note: str = ""


NODE_HELP_CONTENT: dict[NodeType, NodeHelpContent] = {
    NodeType.TEXTURE_INPUT: NodeHelpContent(
        title="Texture",
        summary="Загружает текстуру и предоставляет изображение и отдельные каналы.",
        details=(
            ("Texture", "Путь к исходному файлу текстуры."),
            ("RGBA", "Полное цветное изображение с альфа-каналом."),
            ("R / G / B / A", "Отдельные grayscale-каналы исходного изображения."),
            ("Alpha", "Если альфа отсутствует, выход A заполняется белым значением 255."),
        ),
        note="Color Space и Data Role сохраняются как метаданные и не изменяют пиксели при чтении.",
    ),
    NodeType.COLOR: NodeHelpContent(
        title="Color",
        summary="Создаёт изображение, полностью заполненное выбранным RGBA-цветом.",
        details=(
            ("Color", "Значения Red, Green, Blue и Alpha в диапазоне 0–255."),
            ("Resolution", "Ширина и высота создаваемого изображения."),
            ("Outputs", "Готовое RGBA-изображение и его отдельные R, G, B, A каналы."),
        ),
    ),
    NodeType.CONSTANT_CHANNEL: NodeHelpContent(
        title="Constant",
        summary="Создаёт однородный grayscale-канал с постоянным значением.",
        details=(
            ("Value", "Яркость канала от 0 — чёрный до 255 — белый."),
            ("Resolution", "Берётся из downstream-ветки или целевого Output."),
        ),
    ),
    NodeType.INVERT_CHANNEL: NodeHelpContent(
        title="Invert",
        summary="Инвертирует значения grayscale-канала.",
        details=(
            ("In", "Исходный канал."),
            ("Out", "Результат по формуле 255 − In: чёрное становится белым и наоборот."),
        ),
        note="Если нода отключена, In проходит без изменений.",
    ),
    NodeType.LEVELS_CHANNEL: NodeHelpContent(
        title="Levels",
        summary="Настраивает входной и выходной диапазоны grayscale-канала.",
        details=(
            ("Black", "Входное значение, которое становится нижней границей диапазона."),
            ("White", "Входное значение, которое становится верхней границей диапазона."),
            ("Gamma", "Нелинейно регулирует средние тона."),
            ("Output Min", "Минимальное значение результирующего канала."),
            ("Output Max", "Максимальное значение результирующего канала."),
        ),
        note="Значения за Black/White ограничиваются диапазоном. Отключённая нода пропускает In.",
    ),
    NodeType.REMAP_CHANNEL: NodeHelpContent(
        title="Remap",
        summary="Линейно переносит один диапазон значений grayscale-канала в другой.",
        details=(
            ("Input Min", "Нижняя граница исходного диапазона."),
            ("Input Max", "Верхняя граница исходного диапазона."),
            ("Output Min", "Значение результата для Input Min."),
            ("Output Max", "Значение результата для Input Max."),
        ),
        note="Значения вне Input Min/Max ограничиваются краями. Отключённая нода пропускает In.",
    ),
    NodeType.CLAMP_CHANNEL: NodeHelpContent(
        title="Clamp",
        summary="Ограничивает grayscale-канал заданным минимальным и максимальным значением.",
        details=(
            ("In", "Исходный канал."),
            ("Min", "Все значения ниже Min заменяются на Min."),
            ("Max", "Все значения выше Max заменяются на Max."),
        ),
        note="Если нода отключена, In проходит без изменений.",
    ),
    NodeType.THRESHOLD_CHANNEL: NodeHelpContent(
        title="Threshold",
        summary="Преобразует grayscale-канал в жёсткую чёрно-белую маску.",
        details=(
            ("In", "Исходный grayscale-канал."),
            ("Threshold", "Значения ниже порога становятся 0, остальные — 255."),
            ("Out", "Бинарная маска без промежуточных серых значений."),
        ),
        note="Если нода отключена, In проходит без изменений.",
    ),
    NodeType.BLUR_CHANNEL: NodeHelpContent(
        title="Blur",
        summary="Размывает grayscale-канал равномерным Box Blur.",
        details=(
            ("In", "Исходный grayscale-канал."),
            ("Radius", "Радиус размытия в пикселях, от 0 до 64."),
            ("Out", "Сглаженный канал с более мягкими переходами."),
        ),
        note="Radius 0 или отключённая нода возвращают In без изменений.",
    ),
    NodeType.DILATE_CHANNEL: NodeHelpContent(
        title="Dilate",
        summary="Расширяет светлые области grayscale-маски.",
        details=(
            ("In", "Исходный grayscale-канал или маска."),
            ("Radius", "Радиус расширения в пикселях, от 0 до 64."),
            ("Out", "Результат Max Filter: белые области становятся шире."),
        ),
        note="Radius 0 или отключённая нода возвращают In без изменений.",
    ),
    NodeType.ERODE_CHANNEL: NodeHelpContent(
        title="Erode",
        summary="Расширяет тёмные области и сужает светлые области grayscale-маски.",
        details=(
            ("In", "Исходный grayscale-канал или маска."),
            ("Radius", "Радиус эрозии в пикселях, от 0 до 64."),
            ("Out", "Результат Min Filter: белые области становятся уже."),
        ),
        note="Radius 0 или отключённая нода возвращают In без изменений.",
    ),
    NodeType.BLEND_CHANNEL: NodeHelpContent(
        title="Blend Channel",
        summary="Смешивает два grayscale-канала выбранной математической операцией.",
        details=(
            ("A", "Базовый grayscale-канал."),
            ("B", "Второй grayscale-канал."),
            ("Blend Mode", "Multiply, Add, Subtract, Max, Min или Average."),
            ("Opacity", "Смешивает результат операции обратно с A: 0% = A, 100% = результат."),
        ),
        note="Если нода отключена, A проходит без изменений.",
    ),
    NodeType.LUMINANCE: NodeHelpContent(
        title="Luminance",
        summary="Собирает R, G и B в один grayscale-канал яркости.",
        details=(
            ("R", "Красная составляющая."),
            ("G", "Зелёная составляющая."),
            ("B", "Синяя составляющая."),
            ("L", "Расчётная яркость RGB-изображения."),
        ),
        note="Если нода отключена, вход R проходит в L без изменений.",
    ),
    NodeType.MIX_IMAGE: NodeHelpContent(
        title="Mix Image",
        summary="Смешивает два RGBA-изображения в одно.",
        details=(
            ("A", "Базовое изображение. Используется при значении маски 0."),
            ("B", "Второе изображение. Используется при значении маски 255."),
            (
                "Mask",
                "Необязательная grayscale-маска смешивания. При подключении заменяет Factor.",
            ),
            (
                "Factor",
                "Общее смешивание без Mask: 0% показывает A, 100% показывает B.",
            ),
            (
                "Resolution",
                "Определяет размер результата; остальные входы приводятся к нему.",
            ),
            ("Mask Filter", "Фильтр изменения размера маски."),
        ),
        note="Если нода отключена, изображение A проходит без изменений.",
    ),
    NodeType.BLEND_IMAGE: NodeHelpContent(
        title="Blend Image",
        summary="Смешивает два RGBA-изображения выбранным Blend Mode.",
        details=(
            ("A", "Базовое RGBA-изображение."),
            ("B", "Второе RGBA-изображение."),
            ("Blend Mode", "Multiply, Add, Subtract, Max, Min или Average для каждого канала."),
            ("Opacity", "Сила эффекта: 0% показывает A, 100% — полный результат смешивания."),
            ("Mask", "Необязательно ограничивает эффект: 0 оставляет A, 255 применяет результат."),
            ("Resolution", "Определяет размер результата и ресайз остальных входов."),
        ),
        note="Mask Filter управляет ресайзом маски. Отключённая нода пропускает A.",
    ),
    NodeType.NORMAL_MAP: NodeHelpContent(
        title="Normal Map",
        summary="Исправляет и преобразует tangent-space Normal Map без изменения Alpha.",
        details=(
            ("Normal", "Исходная RGB/RGBA tangent-space Normal Map."),
            ("Flip Red", "Инвертирует X-компоненту нормали в красном канале."),
            (
                "Flip Green",
                "Инвертирует Y-компоненту: это преобразование DirectX ↔ OpenGL.",
            ),
            (
                "Reconstruct Blue",
                "Восстанавливает положительный Z из каналов R и G.",
            ),
            ("Normalize", "Приводит длину каждого RGB-вектора к единице."),
            (
                "Strength",
                "Масштабирует X/Y: 0% даёт плоскую нормаль, 100% сохраняет силу.",
            ),
        ),
        note=(
            "Strength, отличный от 100%, и Reconstruct Blue автоматически сохраняют "
            "корректную длину вектора. Если нода отключена, изображение проходит без изменений."
        ),
    ),
    NodeType.HEIGHT_TO_NORMAL: NodeHelpContent(
        title="Height to Normal",
        summary="Создаёт tangent-space Normal Map из grayscale-карты высот.",
        details=(
            ("Height", "Исходная карта высот: чёрный ниже, белый выше."),
            ("Strength", "Сила наклона нормалей; 0% создаёт плоскую нормаль."),
            ("Radius", "Предварительно сглаживает Height перед вычислением производных."),
            ("Convention", "OpenGL создаёт Y+, DirectX — инвертированный зелёный канал Y−."),
            ("Normal", "Готовая нормализованная RGB normal map."),
        ),
        note="Для Unreal обычно выбирайте DirectX, для Unity — OpenGL.",
    ),
    NodeType.NORMAL_BLEND: NodeHelpContent(
        title="Normal Blend",
        summary="Корректно объединяет основную и detail normal map методом RNM.",
        details=(
            ("Base", "Основная tangent-space normal map."),
            ("Detail", "Detail normal map, переориентируемая относительно Base."),
            ("Mask", "Необязательно ограничивает detail: 0 оставляет Base, 255 применяет RNM."),
            ("Detail Strength", "Масштабирует X/Y detail-нормали перед смешиванием."),
            ("Normal", "Нормализованный результат с альфой Base."),
        ),
        note=(
            "Обычный Blend Image для normal map математически неверен. "
            "Обе карты должны использовать одну ориентацию Y."
        ),
    ),
    NodeType.COLOR_ADJUST: NodeHelpContent(
        title="Color Adjust",
        summary="Выполняет основные цветовые коррекции Base Color или Emissive в одной ноде.",
        details=(
            ("Exposure", "Экспозиция в стопах: +1 удваивает яркость."),
            ("Brightness", "Линейно сдвигает яркость от −100 до +100."),
            ("Contrast", "100% без изменений; меньше снижает, больше усиливает контраст."),
            ("Saturation", "0% даёт grayscale, 100% сохраняет исходную насыщенность."),
            ("Hue", "Сдвигает оттенок на −180…+180 градусов."),
            ("Gamma", "Корректирует средние тона; 1.0 без изменений."),
        ),
        note="Альфа-канал всегда сохраняется без изменений.",
    ),
    NodeType.TRANSFORM_2D: NodeHelpContent(
        title="Transform 2D",
        summary="Трансформирует текстуру внутри её canvas без изменения рабочего процесса графа.",
        details=(
            ("Flip", "Отражает изображение по горизонтали или вертикали."),
            ("Rotate", "Поворачивает на 0°, 90°, 180° или 270° по часовой стрелке."),
            ("Offset", "Сдвигает изображение по X/Y в пикселях."),
            ("Scale", "Масштабирует изображение относительно центра canvas."),
            ("Address", "Clamp растягивает края, Repeat повторяет, Mirror чередует отражённые тайлы."),
            ("Filter", "Фильтр масштабирования текстуры."),
        ),
        note="Поворот 90° или 270° меняет местами естественную ширину и высоту результата.",
    ),
    NodeType.RESIZE_CANVAS: NodeHelpContent(
        title="Resize / Canvas",
        summary="Приводит карту к точному или Power-of-Two размеру с контролем кадрирования.",
        details=(
            ("Output Size", "Exact использует Width/Height; POT выбирает степень двойки от входа."),
            ("Stretch", "Заполняет размер с возможным изменением пропорций."),
            ("Fit", "Вписывает всё изображение и добавляет прозрачные поля."),
            ("Fill", "Заполняет canvas без искажения и обрезает лишнее по центру."),
            ("Crop", "Не масштабирует: обрезает или дополняет прозрачным canvas."),
            ("Pad", "Уменьшает только слишком большую карту и добавляет прозрачные поля."),
            ("Filter", "Nearest, Bilinear, Bicubic или Lanczos."),
        ),
    ),
    NodeType.SPLIT_RGBA: NodeHelpContent(
        title="Split RGBA",
        summary="Разделяет одно RGBA-изображение на четыре grayscale-канала.",
        details=(
            ("RGBA", "Исходное цветное изображение."),
            ("R / G / B / A", "Отдельные красный, зелёный, синий и альфа-каналы."),
        ),
    ),
    NodeType.COMBINE_RGBA: NodeHelpContent(
        title="Combine RGBA",
        summary="Собирает отдельные grayscale-каналы в одно RGBA-изображение.",
        details=(
            ("R", "Красный канал результата; обязателен."),
            ("G", "Зелёный канал результата; обязателен."),
            ("B", "Синий канал результата; обязателен."),
            ("A", "Необязательный альфа-канал; без подключения используется 255."),
            ("RGBA", "Собранное цветное изображение."),
        ),
    ),
    NodeType.SET_ALPHA: NodeHelpContent(
        title="Apply Mask",
        summary="Применяет grayscale-маску к RGBA-изображению.",
        details=(
            ("Image", "Исходное RGBA-изображение."),
            ("Mask", "Grayscale-маска, приводимая к размеру изображения."),
            ("Apply To", "Replace Alpha, Multiply Alpha, Multiply RGB или Multiply RGBA."),
            ("Mask Filter", "Nearest, Bilinear или Lanczos при изменении размера маски."),
        ),
        note="Без подключённой Mask изображение проходит без изменений.",
    ),
    NodeType.VIEW: NodeHelpContent(
        title="View",
        summary="Показывает промежуточный результат графа без экспорта файла.",
        details=(
            ("Image", "Показывает подключённое RGBA-изображение."),
            ("In", "Показывает grayscale-канал, развёрнутый в RGB для просмотра."),
            ("Priority", "Если подключены оба входа, используется Image."),
        ),
        note="Используйте display flag, чтобы вывести эту ноду в Preview.",
    ),
    NodeType.PBR_SHADER: NodeHelpContent(
        title="PBR Shader",
        summary="Собирает карты материала и отправляет их в интерактивный GPU PBR Preview.",
        details=(
            ("Base Color / Normal / Emissive", "Полноцветные карты материала."),
            (
                "Packed / Mask",
                "Декодируется как Unity URP/HDRP либо Unreal ORM/MRA/RMA по Workflow.",
            ),
            (
                "AO / Roughness / Smoothness / Metallic / Opacity",
                "Отдельные каналы имеют приоритет над Packed / Mask.",
            ),
            (
                "Normal Input",
                "From Workflow использует OpenGL Y+ для Unity и DirectX Y− для Unreal.",
            ),
            (
                "Normal Check",
                "Слева показывает ожидаемый workflow, справа тот же материал с Flip Green.",
            ),
        ),
        note=(
            "Roughness и Smoothness нельзя подключать одновременно. Display flag и двойной "
            "клик открывают PBR Preview; upstream-изменения обновляют его автоматически."
        ),
    ),
    NodeType.OUTPUT_RGBA: NodeHelpContent(
        title="Output",
        summary="Формирует и экспортирует итоговое RGB или RGBA-изображение.",
        details=(
            ("Image", "Необязательная RGBA-основа результата."),
            ("R / G / B", "Без Image обязательны; с Image заменяют соответствующие каналы."),
            ("A", "Заменяет или умножает альфу Image согласно Alpha Input."),
            ("Output Mode", "Сохраняет результат как RGB или RGBA."),
            ("Resolution", "Auto, размер выбранного входа или Custom Size."),
            ("Output Name", "Имя и расширение экспортируемого файла."),
            ("Output Path", "Необязательный полный или относительный путь результата."),
            ("Profile", "Настраивает каналы и имя под выбранный целевой пайплайн."),
        ),
        note="Render flag включает экспорт этой ноды; display flag отправляет её в Preview.",
    ),
}


def node_help_content(node_type: NodeType) -> NodeHelpContent | None:
    return NODE_HELP_CONTENT.get(node_type)


class NodeHelpPopup(QFrame):
    """Interactive node help that auto-closes unless it is pinned."""

    def __init__(
        self,
        content: NodeHelpContent,
        parent: QWidget | None = None,
    ) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(parent, flags)
        self.content = content
        self._anchor_rect = QRect()
        self._auto_close_armed = False
        self._pinned = False

        self.setObjectName("NodeHelpPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumSize(NODE_HELP_MIN_WIDTH, NODE_HELP_MIN_HEIGHT)
        self.resize(NODE_HELP_DEFAULT_WIDTH, NODE_HELP_DEFAULT_HEIGHT)
        self.setWindowTitle(f"{content.title} — Help")
        self.setStyleSheet(
            """
            QFrame#NodeHelpPopup {
                background: #1C232B;
                border: 1px solid #526273;
                border-radius: 7px;
            }
            QScrollArea#NodeHelpScroll, QWidget#NodeHelpDetails {
                background: transparent;
                border: none;
            }
            QLabel#NodeHelpTitle {
                color: #F2F6FB;
                font-size: 17px;
                font-weight: 600;
            }
            QLabel#NodeHelpSummary {
                color: #DCE6F0;
                font-size: 14px;
            }
            QLabel#NodeHelpKey {
                color: #7FC1FF;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#NodeHelpValue {
                color: #B8C6D4;
                font-size: 13px;
            }
            QLabel#NodeHelpNote {
                color: #92A4B7;
                font-size: 12px;
                font-style: italic;
            }
            QToolButton {
                color: #CAD6E2;
                background: transparent;
                border: 1px solid transparent;
                border-radius: 4px;
                font-size: 14px;
            }
            QToolButton:hover {
                background: #303B47;
                border-color: #536577;
            }
            QToolButton:checked {
                color: #FFFFFF;
                background: #2E6EB5;
                border-color: #5BA7FF;
            }
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(14, 10, 12, 12)
        root_layout.setSpacing(8)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)

        self.title_label = QLabel(content.title)
        self.title_label.setObjectName("NodeHelpTitle")
        header_layout.addWidget(self.title_label, 1)

        self.pin_button = QToolButton()
        self.pin_button.setIcon(_popup_button_icon("pin"))
        self.pin_button.setIconSize(QSize(16, 16))
        self.pin_button.setCheckable(True)
        self.pin_button.setFixedSize(26, 24)
        self.pin_button.setToolTip("Закрепить подсказку")
        self.pin_button.setAccessibleName("Закрепить подсказку")
        self.pin_button.toggled.connect(self.set_pinned)
        header_layout.addWidget(self.pin_button)

        self.close_button = QToolButton()
        self.close_button.setIcon(_popup_button_icon("close"))
        self.close_button.setIconSize(QSize(16, 16))
        self.close_button.setFixedSize(26, 24)
        self.close_button.setToolTip("Закрыть")
        self.close_button.setAccessibleName("Закрыть подсказку")
        self.close_button.clicked.connect(self.close)
        header_layout.addWidget(self.close_button)
        root_layout.addLayout(header_layout)

        self.summary_label = QLabel(content.summary)
        self.summary_label.setObjectName("NodeHelpSummary")
        self.summary_label.setWordWrap(True)
        root_layout.addWidget(self.summary_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("NodeHelpScroll")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.details_host = QWidget()
        self.details_host.setObjectName("NodeHelpDetails")
        details_layout = QVBoxLayout(self.details_host)
        details_layout.setContentsMargins(0, 0, 4, 0)
        details_layout.setSpacing(8)

        self.detail_labels: dict[str, QLabel] = {}
        for key, value in content.details:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)

            key_label = QLabel(key)
            key_label.setObjectName("NodeHelpKey")
            key_label.setFixedWidth(72)
            key_label.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(key_label)

            value_label = QLabel(value)
            value_label.setObjectName("NodeHelpValue")
            value_label.setWordWrap(True)
            value_label.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
            row.addWidget(value_label, 1)
            details_layout.addLayout(row)
            self.detail_labels[key] = value_label

        self.note_label = QLabel(content.note)
        self.note_label.setObjectName("NodeHelpNote")
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(bool(content.note))
        details_layout.addWidget(self.note_label)
        details_layout.addStretch(1)
        self.scroll_area.setWidget(self.details_host)
        root_layout.addWidget(self.scroll_area, 1)

        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(NODE_HELP_CURSOR_POLL_MS)
        self._cursor_timer.timeout.connect(self._check_auto_close)

    @property
    def is_pinned(self) -> bool:
        return self._pinned

    def set_pinned(self, pinned: bool) -> None:
        if self._pinned == pinned:
            return

        geometry = self.geometry()
        self._pinned = pinned
        self.pin_button.setToolTip(
            "Открепить подсказку" if pinned else "Закрепить подсказку"
        )
        self.close_button.setVisible(not pinned)

        flags = Qt.WindowType.Tool
        if pinned:
            flags |= (
                Qt.WindowType.WindowTitleHint
                | Qt.WindowType.WindowSystemMenuHint
                | Qt.WindowType.WindowCloseButtonHint
                | Qt.WindowType.WindowMaximizeButtonHint
            )
        else:
            flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags)
        self.setGeometry(geometry)
        self.show()
        self.raise_()

    def show_near(self, anchor_rect: QRect) -> None:
        self._anchor_rect = anchor_rect.normalized()
        self._auto_close_armed = False

        position = QPoint(
            self._anchor_rect.right() + NODE_HELP_ANCHOR_GAP,
            self._anchor_rect.top() - 4,
        )
        screen = QGuiApplication.screenAt(self._anchor_rect.center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if position.x() + self.width() > available.right():
                position.setX(
                    self._anchor_rect.left() - self.width() - NODE_HELP_ANCHOR_GAP
                )
            position.setX(
                max(available.left(), min(position.x(), available.right() - self.width()))
            )
            position.setY(
                max(available.top(), min(position.y(), available.bottom() - self.height()))
            )

        self.move(position)
        self.show()
        self.raise_()
        self._cursor_timer.start()
        QTimer.singleShot(NODE_HELP_AUTO_CLOSE_DELAY_MS, self._arm_auto_close)

    def _arm_auto_close(self) -> None:
        self._auto_close_armed = True

    def _check_auto_close(self, cursor_position: QPoint | None = None) -> None:
        if self._pinned or not self._auto_close_armed or not self.isVisible():
            return
        cursor_position = cursor_position or QCursor.pos()
        active_rect = self.frameGeometry().united(self._anchor_rect).adjusted(-6, -6, 6, 6)
        if not active_rect.contains(cursor_position):
            self.close()

    def closeEvent(self, event) -> None:
        self._cursor_timer.stop()
        super().closeEvent(event)
