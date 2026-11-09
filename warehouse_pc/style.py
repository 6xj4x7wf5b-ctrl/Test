"""Compact industrial console theme, retaining the project's blue accent."""
from pathlib import Path

TOKENS = {
    'bg_app': '#111c28', 'bg_surface': '#e9edf1', 'bg_panel': '#ffffff',
    'bg_soft': '#f4f6f8', 'sidebar': '#172635', 'sidebar_hover': '#22384c',
    'primary': '#2f6fdf', 'primary_hover': '#245bc0', 'blue_soft': '#eaf1fb',
    'text_strong': '#1f2c3b', 'text': '#344456', 'muted': '#66778a',
    'border': '#bfc9d3', 'border_muted': '#aebbc9',
    'green': '#498765', 'orange': '#ac7b38', 'red': '#b55252',
}
STYLE = """
QWidget { font-family:'PingFang SC','Microsoft YaHei','Arial'; font-size:12px; color:#344456; }
QMainWindow,QWidget#canvas { background:#111c28; }
QFrame#workArea { background:#e9edf1; border:1px solid #394b5d; border-radius:2px; }
QFrame#sidebar { background:#172635; border:1px solid #304355; border-radius:2px; }
QFrame#sidebar QLabel { color:#aec0d3; background:transparent; }
QFrame#brandbox { background:#1c2e40; border:none; border-bottom:1px solid #34495d; border-radius:0; }
QFrame#sidebar QLabel#brand { color:#dce7f1; font-size:13px; font-weight:700; }
QPushButton#nav { color:#b9c9d9; background:transparent; text-align:left; border:none; border-left:3px solid transparent; padding:6px 9px; border-radius:1px; min-height:17px; }
QPushButton#nav[child="true"] { margin-left:16px; }
QPushButton#nav:checked { background:#294865; border-left:3px solid #4c91ef; color:#ffffff; font-weight:600; }
QPushButton#nav:hover { background:#22384c; color:#ffffff; }
QPushButton#navGroup { color:#dce8f3; background:transparent; text-align:left; border:none; padding:7px 9px; font-size:12px; font-weight:600; border-radius:1px; }
QPushButton#navGroup:hover { background:#22384c; }
QFrame#topbar { background:#20374c; border:1px solid #3b556e; border-bottom:2px solid #2f6fdf; border-radius:2px; }
QFrame#topbarInfo { background:transparent; border:none; }
QFrame#topbarDivider { background:#4a6075; border:none; }
QLabel#appTitle { color:#ffffff; font-size:20px; font-weight:700; }
QLabel#moduleTitle { color:#f1f5fa; font-size:14px; font-weight:700; }
QLabel#topbarSubtitle { color:#b5c7d9; font-size:11px; }
QLabel#statusPill { color:#d4dfeb; background:#192c3e; border:1px solid #4a6075; border-radius:2px; padding:4px 8px; font-size:11px; }
QFrame#topbar QPushButton { background:#2a435a; color:#f4f7fb; border:1px solid #5a7187; border-radius:2px; }
QFrame#topbar QPushButton:hover { background:#365570; border-color:#7c94aa; }
QFrame#topbar QPushButton#primary { background:#2f6fdf; color:white; border-color:#578de8; }
QFrame#topbar QPushButton#primary:hover { background:#245bc0; }
QFrame#topbar QPushButton:disabled,QFrame#topbar QPushButton#primary:disabled { background:#27384a; color:#8294a6; border-color:#3b4f62; }
QLabel#title,QLabel#pageTitle { font-size:16px; font-weight:600; color:#1f2c3b; }
QLabel#subtitle,QLabel#muted { color:#66778a; }
QLabel#cardTitle { color:#1f2c3b; font-size:14px; font-weight:600; }
QLabel#eyebrow { color:#66778a; font-size:11px; font-weight:600; }
QFrame#card,QFrame#section,QWidget#pageSurface { background:white; border:1px solid #bfc9d3; border-radius:2px; }
QFrame#hero { background:#ffffff; border:1px solid #b4c2d0; border-radius:2px; }
QFrame#stage,QFrame#console { background:white; border:1px solid #bfc9d3; border-radius:2px; }
QLabel#metric { font-size:22px; font-weight:700; color:#1f2c3b; }
QLabel#banner { background:#fff4e4; color:#88612b; border:1px solid #dbc39f; border-left:3px solid #ac7b38; border-radius:1px; padding:5px 10px; }
QPushButton { background:#f7f9fb; color:#1f2c3b; border:1px solid #b5c2d0; padding:4px 10px; border-radius:2px; font-weight:500; }
QPushButton:hover { background:#eaf1fb; border-color:#7796bc; }
QPushButton:pressed { background:#dce7f5; }
QPushButton:disabled { color:#8a98a7; background:#edf0f3; border-color:#ccd4dd; }
QPushButton#primary { background:#2f6fdf; color:white; border-color:#245bc0; }
QPushButton#primary:hover { background:#245bc0; }
QPushButton#danger { background:#fff3f1; color:#a94444; border-color:#ceaaaa; }
QGroupBox { background:white; border:1px solid #bfc9d3; border-radius:2px; padding:17px 10px 9px; margin-top:8px; font-weight:600; }
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
QTableWidget { background:white; alternate-background-color:#f4f6f8; color:#344456; border:1px solid #bfc9d3; border-radius:1px; gridline-color:#dce2e9; selection-background-color:#e2ecfa; selection-color:#204b82; }
QHeaderView::section,QTableCornerButton::section { background:#e0e6ed; color:#344456; padding:5px 8px; border:none; border-right:1px solid #cad3dd; border-bottom:1px solid #b4c0cd; font-weight:600; }
QTableWidget::item { padding:3px 6px; }
QTabWidget::pane { border:1px solid #bfc9d3; border-radius:1px; background:white; }
QTabBar::tab { background:#e0e6ed; color:#53667a; padding:5px 10px; border:1px solid #bfc9d3; border-radius:1px; margin-right:2px; }
QTabBar::tab:selected { background:#ffffff; color:#245bc0; border-top:2px solid #2f6fdf; border-bottom-color:white; padding-top:4px; font-weight:600; }
QPlainTextEdit { background:#f4f6f8; border:1px solid #bfc9d3; border-radius:1px; padding:6px; font-family:monospace; }
QStatusBar { background:#111c28; color:#b5c7d9; border-top:1px solid #304355; }
QStatusBar QLabel#statusClock { color:#b5c7d9; }
QStatusBar::item { border:none; }
QScrollArea { border:none; background:transparent; }
QStackedWidget,QWidget#overviewBody { background:#e9edf1; }
QCheckBox { spacing:5px; }
QScrollBar:vertical { background:#e1e6ec; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#aab8c7; border-radius:1px; min-height:24px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
""".replace("@ICONS@", (Path(__file__).resolve().parent / "assets").as_posix())
