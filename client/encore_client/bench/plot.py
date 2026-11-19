"""Scrolling plot of model scores, room intensity, trigger levels and received events."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

COLORS = {"laughter": "#e07a1f", "scream": "#c62828", "gasp": "#1e6fd9"}
INTENSITY = "#8a8a8a"
SPAN_MS = 30_000
MARGIN = (40, 12, 12, 26)  # left, top, right, bottom


class ScorePlot(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(240)
        self.frames: deque = deque(maxlen=4_000)  # (centre ms, scores, intensity)
        self.events: list[dict[str, Any]] = []
        self.triggers: dict[str, float] = {}
        self.origin_ms: int | None = None
        self.now_ms: int | None = None
        self.note = ""

    def reset(self, origin_ms: int, triggers: dict[str, float], note: str = "") -> None:
        self.frames.clear()
        self.events = []
        self.origin_ms = self.now_ms = origin_ms
        self.triggers = dict(triggers)
        self.note = note
        self.update()

    def add_frames(self, frames: Iterable) -> None:
        for f in frames:
            self.frames.append(((f.start_ms + f.end_ms) // 2, dict(f.scores), f.intensity))

    def set_events(self, events: Iterable[dict[str, Any]], now_ms: int | None) -> None:
        self.events = list(events)
        if now_ms is not None:
            self.now_ms = now_ms
        self.update()

    # ---- drawing -----------------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text, grid = pal.color(QPalette.ColorRole.Text), pal.color(QPalette.ColorRole.Mid)
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        left, top, right, bottom = MARGIN
        area = QRectF(left, top, self.width() - left - right, self.height() - top - bottom)
        if area.width() < 50 or area.height() < 50:
            return

        end = max(self.now_ms or 0, self.frames[-1][0] if self.frames else 0)
        start = end - SPAN_MS
        if self.origin_ms is not None and start < self.origin_ms:  # a short run: the axis starts at 0 s
            start, end = self.origin_ms, self.origin_ms + SPAN_MS

        def x(t):
            return area.left() + (t - start) / SPAN_MS * area.width()

        def y(v):
            return area.bottom() - max(0.0, min(1.0, v)) * area.height()

        # grid and axes
        p.setPen(QPen(grid, 1))
        for v in (0.0, 0.25, 0.5, 0.75, 1.0):
            p.drawLine(QPointF(area.left(), y(v)), QPointF(area.right(), y(v)))
        p.setPen(text)
        for v in (0.0, 0.5, 1.0):
            p.drawText(QRectF(0, y(v) - 8, left - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{v:.1f}")
        if self.origin_ms is not None:
            first = -((self.origin_ms - start) // 5_000) * 5_000  # first multiple of 5 s on screen
            for rel in range(max(first, 0), end - self.origin_ms + 1, 5_000):
                px = x(self.origin_ms + rel)
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(px, area.top()), QPointF(px, area.bottom()))
                p.setPen(text)
                p.drawText(QRectF(px - 30, area.bottom() + 4, 60, 18), Qt.AlignmentFlag.AlignHCenter,
                           f"{rel // 1000} s")

        p.save()
        p.setClipRect(area)
        # events the PC received: one band per event, from start to end (or now while open)
        for ev in self.events:
            cap = ev["capture"]
            color = QColor(COLORS.get(ev["reaction"]["type"], INTENSITY))
            e_end = cap["end_ms"] if cap["end_ms"] is not None else end
            if e_end < start:
                continue
            fill = QColor(color)
            fill.setAlpha(45 if ev["phase"] == "FINAL" else 75)
            band = QRectF(QPointF(x(cap["start_ms"]), area.top()), QPointF(x(e_end), area.bottom()))
            p.fillRect(band, fill)
            p.setPen(QPen(color, 1.5))
            for t in cap["triggers_ms"]:
                p.drawLine(QPointF(x(t), area.top()), QPointF(x(t), area.top() + 10))
            p.drawText(QRectF(band.left() + 3, area.top() + 10, 160, 16), Qt.AlignmentFlag.AlignLeft,
                       f"{ev['reaction']['type']} {ev['phase']}")
        # trigger levels, dashed
        for reaction, level in self.triggers.items():
            color = QColor(COLORS.get(reaction, INTENSITY))
            p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(area.left(), y(level)), QPointF(area.right(), y(level)))
        # intensity, then the scores on top
        visible = [f for f in self.frames if f[0] >= start - 1_000]
        if visible:
            p.setPen(QPen(QColor(INTENSITY), 1.2, Qt.PenStyle.DotLine))
            p.drawPolyline(QPolygonF([QPointF(x(t), y(i)) for t, _, i in visible]))
            for reaction, color in COLORS.items():
                p.setPen(QPen(QColor(color), 2))
                p.drawPolyline(QPolygonF([QPointF(x(t), y(s.get(reaction, 0.0))) for t, s, _ in visible]))
        p.restore()

        # label the trigger levels; reactions sharing a level share a line
        levels: dict[float, list[str]] = {}
        for reaction, level in self.triggers.items():
            levels.setdefault(round(level, 2), []).append(reaction)
        fm = p.fontMetrics()
        for level, names in levels.items():
            xr = area.right() - 4
            for name in reversed(names):
                label = f"{name} ≥ {level:.2f}"
                xr -= fm.horizontalAdvance(label)
                p.setPen(QColor(COLORS.get(name, INTENSITY)))
                p.drawText(QPointF(xr, y(level) - 4), label)
                xr -= 10

        # legend and messages
        lx = area.right() - 300
        for label, color in [*COLORS.items(), ("intensity", INTENSITY)]:
            p.setPen(QPen(QColor(color), 3))
            p.drawLine(QPointF(lx, area.top() + 8), QPointF(lx + 14, area.top() + 8))
            p.setPen(text)
            p.drawText(QPointF(lx + 18, area.top() + 12), label)
            lx += 75
        if not self.frames and not self.events:
            p.setPen(text)
            p.drawText(area, Qt.AlignmentFlag.AlignCenter,
                       self.note or "Press Start, then laugh, scream or gasp (or open an audio file).")
        elif self.note:
            p.setPen(text)
            p.drawText(QRectF(area.left() + 6, area.bottom() - 20, area.width(), 18), Qt.AlignmentFlag.AlignLeft,
                       self.note)
        p.end()
