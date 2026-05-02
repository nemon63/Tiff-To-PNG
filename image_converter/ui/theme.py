from __future__ import annotations

APP_STYLESHEET = """
QMainWindow,
QWidget {
    background: #15191E;
    color: #D8DEE6;
    font-family: "Segoe UI Variable Text", "Segoe UI";
    font-size: 12px;
}

QFrame#TopToolbar,
QFrame#SidebarPanel,
QFrame#WorkspaceCard,
QWidget#SectionPanel {
    background: #1C2229;
    border: 1px solid #303842;
    border-radius: 6px;
}

QScrollArea {
    border: none;
    background: transparent;
}

QLabel {
    background: transparent;
    border: none;
}

QLabel#AppTitle {
    color: #F2F5F8;
    font-size: 15px;
    font-weight: 700;
}

QLabel#PanelTitle {
    color: #E7EDF4;
    font-size: 13px;
    font-weight: 700;
}

QLabel#PanelSubtitle,
QLabel#SummaryText,
QLabel#InspectorHintText,
QLabel#ViewerMetaText,
QLabel#ViewerHintText,
QLabel#PreviewMetaText {
    color: #8B97A5;
}

QLabel#StatusPill,
QLabel#CanvasBadge {
    color: #CFE1FF;
    background: #253244;
    border: 1px solid #3C526F;
    border-radius: 4px;
    padding: 3px 8px;
}

QLabel#DropHint {
    background: #202B38;
    color: #C9D7E7;
    border: 1px dashed #4A5A6C;
    border-radius: 5px;
    padding: 8px 10px;
}

QLabel#WarningBanner {
    color: #F3D28A;
    background: #2E2717;
    border: 1px solid #6C5724;
    border-radius: 5px;
    padding: 8px 10px;
}

QLabel#AssetName,
QLabel#PreviewFileName {
    color: #F0F4F8;
    font-size: 13px;
    font-weight: 700;
}

QGroupBox {
    background: #1A2027;
    border: 1px solid #2E3640;
    border-radius: 5px;
    margin-top: 16px;
    padding: 12px 10px 10px 10px;
    font-weight: 700;
    color: #C8D2DE;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: #AEB9C6;
}

QLineEdit,
QSpinBox,
QComboBox,
QPlainTextEdit,
QTableWidget {
    background: #11161B;
    color: #DCE3EB;
    border: 1px solid #303A45;
    border-radius: 4px;
    padding: 5px 7px;
    selection-background-color: #315A8C;
    selection-color: #FFFFFF;
}

QLineEdit:focus,
QSpinBox:focus,
QComboBox:focus,
QPlainTextEdit:focus,
QTableWidget:focus {
    border: 1px solid #5BA7FF;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QPushButton {
    background: #252D36;
    color: #DCE3EB;
    border: 1px solid #3A4653;
    border-radius: 4px;
    padding: 6px 10px;
}

QPushButton:hover {
    background: #2F3945;
    border-color: #4A596A;
}

QPushButton:pressed {
    background: #1E252D;
}

QPushButton#PrimaryButton {
    background: #2E6EB5;
    color: #FFFFFF;
    border: 1px solid #4187D5;
    font-weight: 700;
}

QPushButton#PrimaryButton:hover {
    background: #367FCF;
}

QPushButton#ModeButton {
    background: #1C2229;
    color: #AEB9C6;
    border: 1px solid #303A45;
    padding: 6px 12px;
}

QPushButton#ModeButton:checked {
    background: #2A3D55;
    color: #F0F6FF;
    border: 1px solid #4D7FBA;
    font-weight: 700;
}

QPushButton#DangerButton {
    background: #372124;
    color: #F2A4A4;
    border: 1px solid #734148;
}

QPushButton#GhostButton {
    background: #1C2229;
    color: #C7D0DA;
    border: 1px solid #34404C;
}

QCheckBox,
QRadioButton {
    color: #D8DEE6;
    spacing: 7px;
}

QHeaderView::section {
    background: #202730;
    color: #AEB9C6;
    border: none;
    border-bottom: 1px solid #303A45;
    padding: 6px;
    font-weight: 700;
}

QTableWidget {
    gridline-color: transparent;
    alternate-background-color: #171D23;
}

QTableWidget::item {
    padding: 5px;
}

QTableWidget::item:selected {
    background: #284B75;
    color: #FFFFFF;
}

QTabWidget::pane {
    border: 1px solid #303842;
    border-radius: 5px;
    top: -1px;
}

QTabBar::tab {
    background: #1B2128;
    color: #9EABB8;
    border: 1px solid #303842;
    border-bottom: none;
    padding: 7px 14px;
    margin-right: 2px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}

QTabBar::tab:selected {
    background: #252D36;
    color: #F0F4F8;
}

QSplitter::handle {
    background: #15191E;
}

QSplitter::handle:horizontal {
    width: 6px;
}

QSplitter::handle:vertical {
    height: 6px;
}

QFrame#InspectorRow,
QFrame#InspectorCard,
QFrame#InspectorMetricTile,
QFrame#ViewerHeaderCard,
QFrame#ViewerToolbarCard,
QFrame#ViewerCanvasCard {
    background: #1A2027;
    border: 1px solid #303842;
    border-radius: 5px;
}

QFrame#InspectorCompactRow {
    background: transparent;
    border: none;
}

QLabel#InspectorKey,
QLabel#InspectorInlineKey,
QLabel#MetricKey {
    color: #8B97A5;
    font-size: 11px;
    font-weight: 700;
}

QLabel#InspectorValue,
QLabel#InspectorInlineValue,
QLabel#MetricValue {
    color: #E4EAF1;
    font-weight: 600;
}

QPushButton#CanvasControlButton,
QToolButton#ChannelChip {
    background: rgba(20, 27, 34, 220);
    color: #DCE6F0;
    border: 1px solid rgba(92, 110, 130, 190);
    border-radius: 4px;
    padding: 4px 7px;
    font-weight: 700;
}

QToolButton#ChannelChip:checked {
    background: #2E6EB5;
    color: #FFFFFF;
    border-color: #5BA7FF;
}

QGraphicsView {
    background: #15191E;
    border: 1px solid #303842;
    border-radius: 5px;
}

QStatusBar {
    background: #11161B;
    border-top: 1px solid #303842;
    color: #9EABB8;
}

QMenuBar {
    background: #11161B;
    color: #C7D0DA;
    border-bottom: 1px solid #303842;
}

QMenuBar::item {
    background: transparent;
    padding: 4px 10px;
}

QMenuBar::item:selected {
    background: #253244;
}

QMenu {
    background: #1C2229;
    color: #D8DEE6;
    border: 1px solid #303842;
}

QMenu::item:selected {
    background: #284B75;
}

QDockWidget {
    color: #D8DEE6;
}

QDockWidget::title {
    background: #202730;
    border: 1px solid #303842;
    padding: 5px;
    text-align: left;
}
"""
