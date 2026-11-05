"""Colours and the application stylesheet."""

from PyQt6.QtGui import QColor, QPalette

BG = "#0d1218"
SIDEBAR = "#090d12"
SURFACE = "#141b24"
SURFACE2 = "#1b2430"
SURFACE3 = "#233040"
BORDER = "#263241"
TEXT = "#e6edf3"
MUTED = "#8b98a8"
ACCENT = "#4cb8ff"
ACCENT_HOVER = "#74c9ff"
ACCENT_TEXT = "#04121f"
OK = "#3ecf8e"
WARN = "#f2b84b"
ERR = "#ff6b6b"

STATUS_COLORS = {
    "Running": OK,
    "Ready": TEXT,
    "Update available": WARN,
    "Not installed": MUTED,
}

QSS = f"""
* {{ outline: 0; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QToolTip {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; border-radius: 6px; }}
#Sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
#Sidebar QLabel {{ background: transparent; }}
#Brand {{ font-size: 15pt; font-weight: 700; letter-spacing: 1px; }}
#BrandSub {{ color: {MUTED}; font-size: 8.5pt; }}
#NavButton {{ text-align: left; border: none; background: transparent; padding: 10px 14px;
             border-radius: 8px; color: {MUTED}; font-size: 10.5pt; }}
#NavButton:hover {{ background: {SURFACE}; color: {TEXT}; }}
#NavButton:checked {{ background: {SURFACE2}; color: {TEXT}; font-weight: 600; }}
#PageTitle {{ font-size: 18pt; font-weight: 700; }}
#SectionTitle {{ font-size: 12pt; font-weight: 600; }}
#Muted, QLabel[muted="true"] {{ color: {MUTED}; }}
#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#Card[selected="true"] {{ border: 2px solid {ACCENT}; }}
#Card:hover {{ border-color: {ACCENT}; }}
#Panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
#Panel QLabel, #Card QLabel {{ background: transparent; }}
#Banner {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 12px; }}
#Banner QLabel {{ background: transparent; }}
#StatusBar {{ background: {SIDEBAR}; border-top: 1px solid {BORDER}; }}
#StatusBar QLabel {{ background: transparent; color: {MUTED}; }}
#Pill {{ border-radius: 10px; padding: 3px 10px; background: {SURFACE2}; color: {MUTED}; font-size: 9pt; }}
QPushButton {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 14px; color: {TEXT}; }}
QPushButton:hover {{ border-color: {ACCENT}; background: {SURFACE3}; }}
QPushButton:pressed {{ background: {SURFACE}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {SURFACE2}; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: none; font-weight: 700; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:disabled {{ background: {SURFACE3}; color: {MUTED}; }}
QPushButton[kind="running"] {{ background: #1f3b2f; color: {OK}; border: 1px solid #2d6b51; font-weight: 700; }}
QPushButton[kind="danger"] {{ color: {ERR}; }}
QPushButton[kind="danger"]:hover {{ border-color: {ERR}; }}
QPushButton[kind="flat"] {{ background: transparent; border: none; color: {MUTED}; padding: 4px 8px; }}
QPushButton[kind="flat"]:hover {{ color: {TEXT}; }}
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px; selection-background-color: {ACCENT}; selection-color: {ACCENT_TEXT}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE2}; border: 1px solid {BORDER}; selection-background-color: {SURFACE3}; selection-color: {TEXT}; }}
QListWidget::item {{ padding: 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {SURFACE3}; color: {TEXT}; }}
QProgressBar {{ background: {SURFACE2}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SURFACE3}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ height: 0; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 16px; padding: 14px 12px 12px 12px; background: {SURFACE}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {MUTED}; background: {BG}; }}
QGroupBox QLabel, QGroupBox QCheckBox {{ background: transparent; }}
QCheckBox {{ spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:indeterminate {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {SURFACE2}, stop:0.45 {SURFACE2}, stop:0.46 {MUTED}, stop:0.54 {MUTED}, stop:0.55 {SURFACE2}, stop:1 {SURFACE2}); border-color: {MUTED}; }}
QMenu {{ background: {SURFACE2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {SURFACE3}; }}
QSplitter::handle {{ background: {BORDER}; }}
"""


def palette() -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG, QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE, QPalette.ColorRole.AlternateBase: SURFACE2,
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.Button: SURFACE2,
        QPalette.ColorRole.ButtonText: TEXT, QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: ACCENT_TEXT, QPalette.ColorRole.ToolTipBase: SURFACE2,
        QPalette.ColorRole.ToolTipText: TEXT, QPalette.ColorRole.PlaceholderText: MUTED,
        QPalette.ColorRole.Link: ACCENT,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    return p
