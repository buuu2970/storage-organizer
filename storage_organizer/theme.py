"""見た目の設定（色・余白・角丸・文字・アイコン）を1か所にまとめる。

Windows 11 になじむ落ち着いたライトテーマ。各画面では個別のスタイルを書かず、
ここで定義した QSS と、ウィジェットのプロパティ（kind / role）で見た目を切り替える。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

# --- 色 ---------------------------------------------------------------------
BG = "#F3F3F3"          # 画面の背景（薄いグレー）
SURFACE = "#FFFFFF"     # カード・パネル
SURFACE_ALT = "#F9F9F9"
BORDER = "#E0E0E0"      # 薄い境界線
BORDER_STRONG = "#C8C8C8"
TEXT = "#1B1B1B"        # 本文
TEXT_SUB = "#5C5C5C"    # 補足（薄すぎない濃さ）
ACCENT = "#0067C0"      # アクセント（青系1色）
ACCENT_HOVER = "#1975C5"
ACCENT_PRESSED = "#005BA9"
ACCENT_SOFT = "#E5F0FA"  # 選択中の背景
FOCUS = "#000000"
DANGER = "#C42B1C"      # ごみ箱への移動など、実際に破壊的な操作だけに使う
DANGER_HOVER = "#B02719"
WARN = "#9D5D00"        # 注意（文字色。色だけに頼らずアイコン・文言も添える）
OK = "#0F7B0F"

# --- 寸法（8px 単位） -----------------------------------------------------------
S1, S2, S3, S4 = 8, 16, 24, 32
RADIUS = 8
RADIUS_L = 12

# --- 文字 ---------------------------------------------------------------------
FONT_FAMILIES = ["Yu Gothic UI", "Meiryo UI", "Segoe UI", "Noto Sans CJK JP", "Noto Sans JP", "sans-serif"]
SIZE_CAPTION, SIZE_BODY, SIZE_SUBTITLE, SIZE_TITLE, SIZE_DISPLAY = 9, 10, 12, 16, 22


def font(size: int = SIZE_BODY, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(size)
    f.setWeight(weight)
    return f


def qss() -> str:
    family = ", ".join(f'"{f}"' for f in FONT_FAMILIES)
    return f"""
* {{ font-family: {family}; font-size: {SIZE_BODY}pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Page {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: {SIZE_TITLE}pt; font-weight: 600; }}
QLabel[role="display"] {{ font-size: {SIZE_DISPLAY}pt; font-weight: 600; }}
QLabel[role="subtitle"] {{ font-size: {SIZE_SUBTITLE}pt; font-weight: 600; }}
QLabel[role="caption"] {{ font-size: {SIZE_CAPTION}pt; color: {TEXT_SUB}; }}
QLabel[role="sub"] {{ color: {TEXT_SUB}; }}
QLabel[role="warn"] {{ color: {WARN}; }}
QLabel[role="danger"] {{ color: {DANGER}; }}

QFrame#Card, QWidget#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS_L}px; }}
QFrame#DetailPanel {{ background: transparent; border: none; }}
QFrame#Panel, QWidget#Panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}
QFrame#SelectionBar {{ background: {ACCENT_SOFT}; border: 1px solid #B9D5EE; border-radius: {RADIUS}px; }}
QFrame#Divider {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

QPushButton, QToolButton {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS - 2}px;
    padding: 5px 14px; min-height: 22px;
}}
QPushButton:hover, QToolButton:hover {{ background: {SURFACE_ALT}; border-color: #B0B0B0; }}
QPushButton:pressed, QToolButton:pressed {{ background: #EDEDED; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {FOCUS}; padding: 4px 13px; }}
QPushButton:disabled, QToolButton:disabled {{ color: #8A8A8A; background: #F5F5F5; border-color: {BORDER}; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; color: white; border: 1px solid {ACCENT_PRESSED}; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[kind="primary"]:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton[kind="primary"]:disabled {{ background: #9DBFE0; color: #F0F0F0; }}
QPushButton[kind="danger"] {{ background: {DANGER}; color: white; border: 1px solid #A32416; font-weight: 600; }}
QPushButton[kind="danger"]:hover {{ background: {DANGER_HOVER}; }}
QPushButton[kind="danger"]:disabled {{ background: #E4AAA4; color: #FBEFEE; }}
QPushButton[kind="subtle"], QToolButton[kind="subtle"] {{ background: transparent; border: 1px solid transparent; }}
QPushButton[kind="subtle"]:hover, QToolButton[kind="subtle"]:hover {{ background: #EAEAEA; }}
QPushButton[kind="link"] {{ background: transparent; border: none; color: {ACCENT}; padding: 2px 4px; }}
QPushButton[kind="link"]:hover {{ text-decoration: underline; }}
QPushButton[kind="link"]:disabled {{ color: #8A8A8A; background: transparent; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-bottom: 1px solid #8A8A8A;
    border-radius: {RADIUS - 2}px; padding: 4px 8px; min-height: 22px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-bottom: 2px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT}; }}

QListWidget#Nav {{ background: transparent; border: none; outline: none; }}
QListWidget#Nav::item {{ padding: 8px 10px; margin: 2px 4px; border-radius: {RADIUS - 2}px; color: {TEXT}; }}
QListWidget#Nav::item:hover {{ background: #E9E9E9; }}
QListWidget#Nav::item:selected {{ background: {SURFACE}; color: {TEXT}; font-weight: 600; border-left: 3px solid {ACCENT}; }}

QListView, QTableView, QTreeView, QTreeWidget, QListWidget, QTableWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; outline: none;
    alternate-background-color: {SURFACE_ALT};
}}
QListView#Grid {{ background: {BG}; border: none; }}
QTableView::item:selected, QTreeView::item:selected, QListWidget::item:selected, QTableWidget::item:selected,
QTreeWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QHeaderView::section {{ background: {SURFACE_ALT}; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px 8px; font-weight: 600; color: {TEXT_SUB}; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: {RADIUS}px; background: {SURFACE}; top: -1px; }}
QTabBar::tab {{ background: transparent; border: none; padding: 8px 14px; margin-right: 2px; color: {TEXT_SUB}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; font-weight: 600; border-bottom: 3px solid {ACCENT}; }}

QGroupBox {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; margin-top: 18px;
    padding: 12px 8px 8px 8px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: #C2C2C2; border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: #9E9E9E; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ background: #E6E6E6; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QSplitter::handle {{ background: transparent; width: 8px; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
"""


def set_kind(widget, kind: str):
    """ボタンの種類（primary / danger / subtle / link）を設定する。"""
    widget.setProperty("kind", kind)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def set_role(label, role: str):
    """ラベルの役割（title / subtitle / caption / sub / warn / danger）を設定する。"""
    label.setProperty("role", role)
    label.style().unpolish(label)
    label.style().polish(label)
    return label


# --- アイコン（同じ線の太さの SVG で統一） ------------------------------------------
_PATHS = {
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "files": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
             '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "sparkle": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
    "video": '<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1'
                'M4.9 19.1 7 17M17 7l2.1-2.1"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
    "film": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    "play": '<path d="M8 5v14l11-7z"/>',
    "check": '<path d="m5 12 5 5 9-10"/>',
    "pause": '<path d="M9 5v14M15 5v14"/>',
    "flag": '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "shield": '<path d="M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z"/>',
    "more": '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "folder": '<path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "warning": '<path d="M12 3 2 20h20zM12 10v5M12 17.5v.5"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.5M3.5 12h.5M3.5 18h.5"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    "drive": '<rect x="3" y="12" width="18" height="7" rx="2"/><path d="M5 12 7 5h10l2 7M16 15.5h.5"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    "undo": '<path d="M9 14 4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3"/>',
    "star": '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9z"/>',
}


def icon(name: str, color: str = TEXT, size: int = 20) -> QIcon:
    """名前を指定して線画アイコンを作る。"""
    return QIcon(pixmap(name, color, size))


def pixmap(name: str, color: str = TEXT, size: int = 20, dpr: float = 2.0) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(QSize(int(size * dpr), int(size * dpr)))
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter, QRectF(0, 0, size * dpr, size * dpr))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def qcolor(hex_color: str) -> QColor:
    return QColor(hex_color)


def apply(app) -> None:
    app.setStyle("Fusion")  # OS ごとの差を減らし、QSS を安定して効かせる
    app.setFont(font())
    app.setStyleSheet(qss())
