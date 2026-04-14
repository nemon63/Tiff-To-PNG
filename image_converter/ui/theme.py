from __future__ import annotations

APP_STYLESHEET = """
QMainWindow, QWidget {
    background: #ECE7DE;
    color: #1E2933;
    font-family: "Segoe UI Variable Text";
    font-size: 13px;
}

QFrame#SidebarPanel,
QFrame#WorkspaceCard,
QWidget#SectionPanel {
    background: #FCFBF8;
    border: 1px solid #D8D0C4;
    border-radius: 18px;
}

QScrollArea {
    border: none;
    background: transparent;
}

QGroupBox {
    background: #F7F3EC;
    border: 1px solid #DED4C7;
    border-radius: 14px;
    margin-top: 18px;
    padding: 16px 14px 14px 14px;
    font-weight: 700;
    color: #4D5A67;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
    color: #324150;
}

QLabel#PanelTitle {
    color: #16212C;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLabel#PanelSubtitle {
    color: #6B7681;
    background: transparent;
    border: none;
}

QLabel#SummaryText {
    color: #6E7781;
    background: transparent;
    border: none;
}

QLabel#DropHint {
    background: #203649;
    color: #E0EDF9;
    border: 1px solid #34526B;
    border-radius: 12px;
    padding: 12px 14px;
}

QLabel#WarningBanner {
    color: #6A4700;
    background: #FFF1BF;
    border: 1px solid #F0D580;
    border-radius: 12px;
    padding: 10px 12px;
}

QLabel#AssetName {
    color: #16212C;
    font-size: 15px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLabel#PreviewFileName {
    color: #16212C;
    font-size: 14px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLabel#PreviewMetaText {
    color: #4C5B69;
    font-size: 13px;
    background: transparent;
    border: none;
}

QLabel#CanvasBadge {
    color: #F7F9FC;
    font-size: 12px;
    font-weight: 700;
    background: rgba(24, 35, 48, 190);
    border: 1px solid rgba(93, 112, 130, 180);
    border-radius: 10px;
    padding: 4px 8px;
}

QPushButton#CanvasControlButton {
    background: rgba(24, 35, 48, 210);
    color: #F7F9FC;
    border: 1px solid rgba(96, 114, 130, 180);
    border-radius: 10px;
    padding: 4px 8px;
    font-weight: 700;
}

QPushButton#CanvasControlButton:hover {
    background: rgba(34, 48, 64, 225);
}

QToolButton#ChannelChip {
    background: rgba(24, 35, 48, 210);
    color: #DCE6F0;
    border: 1px solid rgba(96, 114, 130, 180);
    border-radius: 10px;
    padding: 4px 8px;
    font-weight: 700;
}

QToolButton#ChannelChip:hover {
    background: rgba(34, 48, 64, 225);
}

QToolButton#ChannelChip:checked {
    background: #F3E2C6;
    color: #192430;
    border: 1px solid #E4C99E;
}

QFrame#InspectorRow {
    background: #F6F2EA;
    border: 1px solid #E1D7C9;
    border-radius: 14px;
}

QFrame#InspectorCard,
QFrame#InspectorMetricTile,
QFrame#ViewerHeaderCard,
QFrame#ViewerToolbarCard,
QFrame#ViewerCanvasCard {
    background: #F7F3EC;
    border: 1px solid #DED4C7;
    border-radius: 16px;
}

QFrame#InspectorCompactRow {
    background: transparent;
    border: none;
    border-radius: 0;
}

QLabel#InspectorKey {
    color: #7A8087;
    font-size: 11px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLabel#InspectorValue {
    color: #182430;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
    border: none;
}

QLabel#InspectorInlineKey {
    color: #6E7A86;
    font-size: 11px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLabel#InspectorInlineValue {
    color: #182430;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
    border: none;
}

QLabel#InspectorHintText,
QLabel#ViewerMetaText,
QLabel#ViewerHintText {
    color: #607081;
    background: transparent;
    border: none;
}

QLabel#MetricKey {
    color: #748291;
    font-size: 11px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLabel#MetricValue {
    color: #182430;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
    border: none;
}

QLineEdit,
QSpinBox,
QComboBox,
QPlainTextEdit,
QTableWidget {
    background: #FFFFFF;
    border: 1px solid #CCC2B5;
    border-radius: 12px;
    padding: 7px 9px;
    selection-background-color: #D9E7F5;
    selection-color: #13202C;
}

QLineEdit:focus,
QSpinBox:focus,
QComboBox:focus,
QPlainTextEdit:focus,
QTableWidget:focus {
    border: 1px solid #A96831;
}

QComboBox {
    min-width: 88px;
}

QPushButton {
    background: #F0EADF;
    color: #1E2933;
    border: 1px solid #D7CCBD;
    border-radius: 12px;
    padding: 8px 12px;
}

QPushButton:hover {
    background: #E7DECF;
}

QPushButton#PrimaryButton {
    background: #B36A2E;
    color: #FFFFFF;
    border: none;
    padding: 10px 14px;
    font-weight: 700;
}

QPushButton#PrimaryButton:hover {
    background: #C47736;
}

QPushButton#DangerButton {
    background: #FFF3EE;
    color: #A73D2F;
    border: 1px solid #EFC2B8;
}

QPushButton#GhostButton {
    background: #FCFBF8;
    color: #324150;
    border: 1px solid #D8D0C4;
    padding: 6px 10px;
}

QCheckBox,
QRadioButton {
    spacing: 8px;
}

QHeaderView::section {
    background: #F3EEE5;
    color: #344150;
    border: none;
    border-bottom: 1px solid #E1D8CB;
    padding: 8px;
    font-weight: 700;
}

QTableWidget {
    gridline-color: transparent;
    alternate-background-color: #FBF8F2;
}

QTableWidget::item {
    padding: 6px;
}

QSplitter::handle {
    background: transparent;
}

QSplitter::handle:horizontal {
    width: 10px;
}

QSplitter::handle:vertical {
    height: 10px;
}

QStatusBar {
    background: #FCFBF8;
    border-top: 1px solid #D8D0C4;
}
"""
