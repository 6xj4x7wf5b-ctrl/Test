"""Navigation paths reused from the original Vue AppIcon component."""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

PATHS = {
    "home": ["M4 10.5L12 4L20 10.5", "M6.5 9.5V19H17.5V9.5", "M10 19V13H14V19"],
    "monitoring": ["M5 18V11", "M10 18V6", "M15 18V9", "M20 18V4"],
    "grid": ["M5 5H10V10H5Z", "M14 5H19V10H14Z", "M5 14H10V19H5Z", "M14 14H19V19H14Z"],
    "database": ["M5 7C5 5.343 8.134 4 12 4C15.866 4 19 5.343 19 7C19 8.657 15.866 10 12 10C8.134 10 5 8.657 5 7Z",
                 "M5 7V12C5 13.657 8.134 15 12 15C15.866 15 19 13.657 19 12V7",
                 "M5 12V17C5 18.657 8.134 20 12 20C15.866 20 19 18.657 19 17V12"],
    "settings": ["M12 9.2A2.8 2.8 0 1 1 12 14.8A2.8 2.8 0 0 1 12 9.2Z",
                 "M19.4 13.5L20 12L19.4 10.5L17.8 10L17.3 8.5L15.8 7.9L14.7 6.6L13.1 6.8L12 6L10.9 6.8L9.3 6.6L8.2 7.9L6.7 8.5L6.2 10L4.6 10.5L4 12L4.6 13.5L6.2 14L6.7 15.5L8.2 16.1L9.3 17.4L10.9 17.2L12 18L13.1 17.2L14.7 17.4L15.8 16.1L17.3 15.5L17.8 14L19.4 13.5Z"],
}


def navigation_icon(name):
    paths = "".join(f'<path d="{path}"/>' for path in PATHS[name])
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#aec0d3" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
