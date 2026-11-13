"""Compact industrial console theme, retaining the project's blue accent."""
from pathlib import Path

TOKENS = {
    'bg_app': '#111c28', 'bg_surface': '#f2f4f7', 'bg_panel': '#ffffff',
    'bg_soft': '#f4f6f8', 'sidebar': '#172635', 'sidebar_hover': '#22384c',
    'primary': '#2f6fdf', 'primary_hover': '#245bc0', 'blue_soft': '#eaf1fb',
    'text_strong': '#1f2c3b', 'text': '#344456', 'muted': '#66778a',
    'border': '#d5dde5', 'border_muted': '#aebbc9',
    'green': '#498765', 'orange': '#ac7b38', 'red': '#b55252',
}
STYLE = """
QWidget { font-family:'PingFang SC','Microsoft YaHei','Arial'; font-size:12px; color:#344456; }
QMainWindow,QWidget#canvas { background:#111c28; }
QFrame#workArea { background:#f2f4f7; border:1px solid #394b5d; border-radius:2px; }
QFrame#sidebar { background:#172635; border:1px solid #263c50; border-radius:2px; }
QFrame#sidebar QLabel { color:#aec0d3; background:transparent; }
QFrame#brandbox { background:#1c2e40; border:none; border-bottom:1px solid #34495d; border-radius:0; }
QFrame#sidebar QLabel#brand { color:#dce7f1; font-size:13px; font-weight:700; }
QPushButton#nav { color:#b9c9d9; background:transparent; text-align:left; border:none; border-left:3px solid transparent; padding:6px 9px; border-radius:1px; min-height:17px; }
QPushButton#nav[child="true"] { margin-left:16px; }
QPushButton#nav:checked { background:#294865; border-left:3px solid #4c91ef; color:#ffffff; font-weight:600; }
QPushButton#nav:hover { background:#22384c; color:#ffffff; }
QPushButton#navGroup { color:#dce8f3; background:transparent; text-align:left; border:none; padding:7px 9px; font-size:12px; font-weight:600; border-radius:1px; }
QPushButton#navGroup:hover { background:#22384c; }
QFrame#topbar { background:#1b3247; border:1px solid #30485e; border-bottom:2px solid #2f6fdf; border-radius:2px; }
QFrame#topbarInfo { background:transparent; border:none; }
QFrame#topbarDivider { background:#4a6075; border:none; }
QLabel#appTitle { color:#ffffff; font-size:16px; font-weight:600; }
QLabel#moduleTitle { color:#f1f5fa; font-size:20px; font-weight:700; }
QLabel#topbarSubtitle { color:#b5c7d9; font-size:11px; }
QLabel#statusPill { color:#b8c9d9; background:transparent; border:none; padding:4px 8px; font-size:11px; }
QLabel#demoBadge { color:#edd49c; background:#31414e; border:1px solid #796a4e; padding:3px 7px; border-radius:2px; font-size:11px; }
QLabel#alarmPill { color:#ffe0b2; background:#59412b; border:1px solid #8b6845; padding:3px 7px; border-radius:2px; font-size:11px; }
QFrame#topbar QPushButton { background:#2a435a; color:#f4f7fb; border:1px solid #5a7187; border-radius:2px; }
QFrame#topbar QPushButton:hover { background:#365570; border-color:#7c94aa; }
QFrame#topbar QPushButton#primary { background:#2f6fdf; color:white; border-color:#578de8; }
QFrame#topbar QPushButton#primary:hover { background:#245bc0; }
QFrame#topbar QPushButton:disabled,QFrame#topbar QPushButton#primary:disabled { background:#27384a; color:#8294a6; border-color:#3b4f62; }
QLabel#title,QLabel#pageTitle { font-size:16px; font-weight:600; color:#1f2c3b; }
QLabel#subtitle,QLabel#muted { color:#66778a; }
QLabel#cardTitle { color:#1f2c3b; font-size:14px; font-weight:600; }
QLabel#eyebrow { color:#66778a; font-size:11px; font-weight:600; }
QFrame#card,QFrame#section,QWidget#pageSurface { background:white; border:1px solid #d5dde5; border-radius:2px; }
QFrame#hero { background:#ffffff; border:1px solid #d5dde5; border-radius:2px; }
QFrame#stage,QFrame#console { background:white; border:1px solid #d5dde5; border-radius:2px; }
QLabel#metric { font-size:22px; font-weight:700; color:#1f2c3b; }
QLabel#banner { background:#fff4e4; color:#88612b; border:1px solid #dbc39f; border-left:3px solid #ac7b38; border-radius:1px; padding:5px 10px; }
QPushButton { background:#f7f9fb; color:#1f2c3b; border:1px solid #b5c2d0; padding:4px 10px; border-radius:2px; font-weight:500; }
QPushButton:hover { background:#eaf1fb; border-color:#7796bc; }
QPushButton:pressed { background:#dce7f5; }
QPushButton:disabled { color:#8a98a7; background:#edf0f3; border-color:#ccd4dd; }
QPushButton#primary { background:#2f6fdf; color:white; border-color:#245bc0; }
QPushButton#primary:hover { background:#245bc0; }
QPushButton#danger { background:#fff3f1; color:#a94444; border-color:#ceaaaa; }
QGroupBox { background:white; border:1px solid #d5dde5; border-radius:2px; padding:17px 10px 9px; margin-top:8px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; color:#1f2c3b; }
QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QDateTimeEdit { background:white; border:1px solid #aebbc9; padding:3px 6px; border-radius:2px; min-height:18px; }
QAbstractSpinBox QLineEdit,QComboBox QLineEdit { border:none; background:transparent; padding:0; min-height:0; }
QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus,QDateTimeEdit:focus { border-color:#2f6fdf; }
QComboBox { padding-right:24px; }
QComboBox::drop-down,QDateTimeEdit::drop-down { subcontrol-origin:padding; subcontrol-position:top right; width:20px; background:#f1f4f7; border:none; border-left:1px solid #ccd4dd; }
QComboBox::drop-down:hover,QDateTimeEdit::drop-down:hover { background:#e4ecf7; }
QSpinBox::up-button,QDoubleSpinBox::up-button,QDateTimeEdit::up-button { subcontrol-origin:border; subcontrol-position:top right; width:16px; background:#f1f4f7; border:1px solid #aebbc9; border-bottom-color:#ccd4dd; }
QSpinBox::down-button,QDoubleSpinBox::down-button,QDateTimeEdit::down-button { subcontrol-origin:border; subcontrol-position:bottom right; width:16px; background:#f1f4f7; border:1px solid #aebbc9; border-top:none; }
QComboBox::down-arrow,QDateTimeEdit::down-arrow { image:url("@ICONS@/chevron-down.svg"); width:10px; height:6px; }
QSpinBox::up-arrow,QDoubleSpinBox::up-arrow,QDateTimeEdit::up-arrow { image:url("@ICONS@/chevron-up.svg"); width:8px; height:5px; }
QSpinBox::down-arrow,QDoubleSpinBox::down-arrow { image:url("@ICONS@/chevron-down.svg"); width:8px; height:5px; }
QComboBox QAbstractItemView { background:white; color:#344456; border:1px solid #aebbc9; selection-background-color:#eaf1fb; selection-color:#245bc0; }
QTableWidget { background:white; alternate-background-color:#f4f6f8; color:#344456; border:1px solid #d5dde5; border-radius:1px; gridline-color:#dce2e9; selection-background-color:#e2ecfa; selection-color:#204b82; }
QHeaderView::section,QTableCornerButton::section { background:#f0f3f6; color:#526173; padding:6px 8px; border:none; border-right:1px solid #e0e6ed; border-bottom:1px solid #d5dde5; font-weight:600; }
QTableWidget::item { padding:3px 6px; }
QTabWidget::pane { border:1px solid #d5dde5; border-radius:1px; background:white; }
QWidget#settingsPanel { background:white; }
QWidget#settingsScopePage { background:#f2f4f7; }
QTabBar::tab { background:#edf1f5; color:#53667a; padding:5px 10px; border:1px solid #d5dde5; border-radius:1px; margin-right:2px; }
QTabBar::tab:selected { background:#ffffff; color:#245bc0; border-top:2px solid #2f6fdf; border-bottom-color:white; padding-top:4px; font-weight:600; }
QPlainTextEdit { background:#f4f6f8; border:1px solid #d5dde5; border-radius:1px; padding:6px; font-family:monospace; }
QStatusBar { background:#111c28; color:#b5c7d9; border-top:1px solid #304355; }
QStatusBar QLabel#statusClock { color:#b5c7d9; }
QStatusBar::item { border:none; }
QScrollArea { border:none; background:transparent; }
QStackedWidget,QWidget#overviewBody { background:#f2f4f7; }
QLabel#sectionTitle { color:#25374a; font-size:14px; font-weight:600; }
QLabel#sectionMeta { color:#708093; font-size:11px; font-weight:400; background:transparent; }
QFrame#sectionDivider { background:#e2e7ed; border:none; }
QFrame#toolbar,QWidget#toolbar { background:transparent; border:none; }
QFrame#filterPanel { background:white; border:1px solid #d5dde5; border-radius:2px; }
QLabel#filterLabel { color:#66778a; font-size:12px; background:transparent; }
QStackedWidget#warehouseViews,QWidget#warehouseCardArea { background:transparent; }
QFrame#homeMetrics { background:transparent; border:none; }
QFrame#summaryCard { background:#ffffff; border:1px solid #d5dde5; border-radius:2px; }
QFrame#summaryCard[tone="blue"] { background:#edf3fb; border-color:#d3e0ef; border-left:3px solid #668fc5; }
QFrame#summaryCard[tone="green"] { background:#edf5f2; border-color:#d4e5de; border-left:3px solid #6e9c89; }
QFrame#summaryCard[tone="slate"] { background:#eef1f7; border-color:#d9dfea; border-left:3px solid #8294b2; }
QFrame#summaryCard[tone="amber"] { background:#faf4e9; border-color:#eadfc9; border-left:3px solid #baa16d; }
QFrame#measurementCard { background:#ffffff; border:1px solid #d5dde5; border-top:2px solid #8294b2; border-radius:2px; }
QFrame#measurementCard[reading="grain"] { background:#faf7f0; border-top-color:#baa16d; }
QFrame#measurementCard[reading="indoor"] { background:#f0f5fc; border-top-color:#668fc5; }
QFrame#measurementCard[reading="outdoor"] { background:#f0f7f4; border-top-color:#6e9c89; }
QLabel#readingValue { color:#25374a; font-size:18px; font-weight:600; background:transparent; }
QFrame#warehouseCard { background:#fbfcfe; border:1px solid #d5dde5; border-radius:2px; }
QFrame#warehouseCard:hover { border-color:#8da9ca; background:#f5f8fc; }
QPushButton#secondary,QToolButton#secondary { color:#465d75; background:#f7f9fb; border:1px solid #c5d0dc; padding:4px 8px; font-weight:400; }
QPushButton#secondary:hover,QToolButton#secondary:hover { color:#245bc0; background:#eaf1fb; border-color:#93afd2; }
QPushButton#secondary:disabled,QToolButton#secondary:disabled { color:#8a98a7; background:#edf0f3; border-color:#d5dde5; }
QPushButton#quietButton,QToolButton#quietButton { color:#516780; background:transparent; border:1px solid transparent; padding:4px 7px; }
QPushButton#quietButton:hover,QToolButton#quietButton:hover { color:#245bc0; background:#eaf1fb; border-color:#cbdcf3; }
QToolButton { border:1px solid #c5d0dc; border-radius:2px; padding:4px 8px; background:#f7f9fb; }
QToolButton::menu-indicator { width:8px; height:6px; subcontrol-position:right center; right:2px; }
QMenu { background:#ffffff; color:#344456; border:1px solid #c9d3de; padding:4px; }
QMenu::item { padding:7px 24px 7px 12px; }
QMenu::item:selected { background:#eaf1fb; color:#245bc0; }
QMenu::item:disabled { color:#9aa5b1; }
QMenu::separator { height:1px; background:#e2e7ee; margin:4px; }
QToolTip { color:#344456; background:#ffffff; border:1px solid #bdcad7; padding:5px; }
QCheckBox { spacing:5px; }
QScrollBar:vertical { background:#e1e6ec; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#aab8c7; border-radius:1px; min-height:24px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
""".replace("@ICONS@", (Path(__file__).resolve().parent / "assets").as_posix())
