"""The player window's progress bar, and the clip borders under it.

For the film it is a plain progress bar. While a saved clip shows, the bar
covers the whole clip file: the chosen part between two yellow borders, and the
hidden editing margins outside it in grey (ClipPanel). A click on the bar jumps
there. Under the bar, ``TrimLane`` has a grip for each border; dragging one moves
that border into the margins, or into the clip, and shows that frame.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget

BORDER = QColor("#f9a825")
MARGIN = QColor(110, 110, 110, 170)
GRAB_PX = 10       # a press this close to a border's grip drags it
LANE_PX = 14
BAND_PX = 8        # height of the grey margin band over the bar


class TrimSlider(QSlider):
    """A horizontal progress bar that can show a chosen part (``set_chosen``)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.chosen: tuple[int, int] | None = None   # in the bar's values; None for the film

    def set_chosen(self, chosen: tuple[int, int] | None) -> None:
        if chosen != self.chosen:
            self.chosen = chosen
            self.update()

    def _rects(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        cc, style = QStyle.ComplexControl.CC_Slider, self.style()
        return (style.subControlRect(cc, opt, QStyle.SubControl.SC_SliderGroove, self),
                style.subControlRect(cc, opt, QStyle.SubControl.SC_SliderHandle, self))

    def x_for(self, value: int) -> float:
        """The x of the handle's centre at ``value``."""
        groove, handle = self._rects()
        span = max(1, groove.width() - handle.width())
        return groove.x() + handle.width() / 2 + QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), int(value), span)

    def value_for(self, x: float) -> int:
        groove, handle = self._rects()
        span = max(1, groove.width() - handle.width())
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), round(x - groove.x() - handle.width() / 2), span)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.chosen is not None and event.button() == Qt.MouseButton.LeftButton:
            _, handle = self._rects()
            if not handle.contains(event.position().toPoint()):
                # Jump the handle under the pointer; the press then drags it from there.
                self.setValue(self.value_for(event.position().x()))
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().paintEvent(event)
        if self.chosen is None or self.maximum() <= self.minimum():
            return
        groove, _ = self._rects()
        left, right = self.x_for(self.minimum()), self.x_for(self.maximum())
        xs, xe = self.x_for(self.chosen[0]), self.x_for(self.chosen[1])
        top = groove.center().y() - BAND_PX / 2
        p = QPainter(self)
        p.fillRect(QRectF(left, top, xs - left, BAND_PX), MARGIN)
        p.fillRect(QRectF(xe, top, right - xe, BAND_PX), MARGIN)
        p.setPen(QPen(BORDER, 3))
        for x in (xs, xe):
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))


class TrimLane(QWidget):
    """Grips for the chosen part's borders, under a ``TrimSlider``.

    Emits ``border_moved("start" | "end", value)`` while a grip is dragged and
    ``border_released()`` when it is let go."""

    border_moved = Signal(str, int)
    border_released = Signal()

    def __init__(self, slider: TrimSlider, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.slider = slider
        self.dragging: str | None = None
        self.setFixedHeight(LANE_PX)
        self.setMouseTracking(True)
        policy = self.sizePolicy()
        policy.setRetainSizeWhenHidden(True)   # the film's controls do not move
        self.setSizePolicy(policy)
        self.setToolTip("Drag a yellow border to keep more or less of the clip.\n"
                        "Grey on the bar: the clip's hidden editing margins.")

    def x_for(self, value: int) -> float:
        offset = self.mapFromGlobal(self.slider.mapToGlobal(QPoint(0, 0))).x()
        return offset + self.slider.x_for(value)

    def value_for(self, x: float) -> int:
        offset = self.mapFromGlobal(self.slider.mapToGlobal(QPoint(0, 0))).x()
        return self.slider.value_for(x - offset)

    def border_at(self, x: float) -> str | None:
        chosen = self.slider.chosen
        if chosen is None:
            return None
        xs, xe = self.x_for(chosen[0]), self.x_for(chosen[1])
        near = [(abs(x - xs), "start"), (abs(x - xe), "end")]
        near = [(d, name) for d, name in near if d <= GRAB_PX]
        if len(near) == 2:   # borders close together: the side of the pointer decides
            return "start" if x < (xs + xe) / 2 else "end"
        return near[0][1] if near else None

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = self.border_at(event.position().x())
        if self.dragging is None:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        x = event.position().x()
        if self.dragging is not None:
            self.border_moved.emit(self.dragging, self.value_for(x))
            return
        self.setCursor(Qt.CursorShape.SizeHorCursor if self.border_at(x)
                       else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.dragging is not None:
            self.border_moved.emit(self.dragging, self.value_for(event.position().x()))
            self.dragging = None
            self.border_released.emit()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        chosen = self.slider.chosen
        if chosen is None or self.slider.maximum() <= self.slider.minimum():
            return
        left, right = self.x_for(self.slider.minimum()), self.x_for(self.slider.maximum())
        xs, xe = self.x_for(chosen[0]), self.x_for(chosen[1])
        mid = self.height() / 2
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(QRectF(left, mid - 1, right - left, 2), MARGIN)
        chosen_band = QColor(BORDER)
        chosen_band.setAlpha(90)
        p.fillRect(QRectF(xs, mid - 2, xe - xs, 4), chosen_band)
        h = self.height()
        # "[" at the start border and "]" at the end border.
        p.fillRect(QRectF(xs - 2, 0, 4, h), BORDER)
        p.fillRect(QRectF(xs - 2, h - 4, 9, 4), BORDER)
        p.fillRect(QRectF(xe - 2, 0, 4, h), BORDER)
        p.fillRect(QRectF(xe - 7, h - 4, 9, 4), BORDER)


def make_trim_bar(parent: QWidget | None = None) -> tuple[TrimSlider, TrimLane, QWidget]:
    """The bar and the lane under it, in one widget for the controls row."""
    box = QWidget(parent)
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    slider = TrimSlider()
    lane = TrimLane(slider)
    lane.hide()
    layout.addWidget(slider)
    layout.addWidget(lane)
    return slider, lane, box
