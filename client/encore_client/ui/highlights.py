"""Tonight's Highlights view (R4, Jira EM-13).

All decisions live in ``clips.highlights``; this widget shows the result.
A play button appears only for clips whose file is finalized and checked.
Playback uses the Saved clips player, so the content player pauses as before.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..clips.highlights import Highlight, build_highlights
from ..clips.model import load_index
from .formatting import format_ms

EMPTY = "No finalized reactions yet."
COLUMNS = ("#", "Reaction", "Intensity", "Time", "Title", "Clip")


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


class HighlightsPanel(QWidget):
    play_requested = Signal(object)   # Path of a checked clip file

    def __init__(self, session, index_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.session = session
        self.index_path = index_path
        self.items: list[Highlight] = []
        self._index_stamp = None
        self._index_clips = []
        self._shown = None

        self.empty = QLabel(EMPTY)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(lambda *_: self._show_selected())
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.buttons = QHBoxLayout()

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.detail)
        layout.addLayout(self.buttons)
        self.refresh()

    # ---- data ---------------------------------------------------------------------

    def _clips(self):
        """The clip index, reread when the file changes (a pending clip finishing)."""
        try:
            stat = self.index_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp != self._index_stamp:
            self._index_stamp = stamp
            try:
                self._index_clips = load_index(self.index_path) if stamp else []
            except (OSError, ValueError, KeyError, TypeError):
                self._index_clips = []
        return self._index_clips

    def refresh(self) -> None:
        self.items = build_highlights(self.session.view, self._clips(), self.index_path.parent)
        if self.items == self._shown:
            return
        selected = self._selected_id()
        self._shown = self.items
        self.empty.setVisible(not self.items)
        self.table.setVisible(bool(self.items))
        self.table.setRowCount(len(self.items))
        for row, h in enumerate(self.items):
            cells = (str(h.rank), h.type, _fmt(h.intensity), format_ms(h.timecode_ms),
                     h.title or "—", h.clip_summary)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        keep = next((i for i, h in enumerate(self.items) if h.event_id == selected), None)
        if keep is not None:
            self.table.setCurrentCell(keep, 0)
        self._show_selected()

    def text(self) -> str:
        """Plain-text listing (also used by tests)."""
        if not self.items:
            return EMPTY
        return "\n".join(
            f"{h.rank}. {h.type} · intensity {_fmt(h.intensity)} · {format_ms(h.timecode_ms)} · "
            f"{h.title or 'untitled'} · {h.event_id} · {h.clip_summary}"
            for h in self.items)

    # ---- selection and playback ------------------------------------------------------

    def _selected_id(self) -> str | None:
        row = self.table.currentRow()
        return self._shown[row].event_id if self._shown and 0 <= row < len(self._shown) else None

    def selected(self) -> Highlight | None:
        row = self.table.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _show_selected(self) -> None:
        while self.buttons.count():
            widget = self.buttons.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        h = self.selected()
        if h is None:
            self.detail.setText("Select a highlight to see its clip." if self.items else "")
            return
        lines = [f"{h.type} · confidence {_fmt(h.confidence)} · {h.event_id}"]
        for link in h.clips:
            shared = f" (shared with {', '.join(link.shared_with)})" if link.shared_with else ""
            lines.append(f"{link.name}: {link.status}{shared}" if link.clip_id else link.status)
        self.detail.setText("\n".join(lines))
        for link in h.playable:
            button = QPushButton(f"▶ Play {link.name.lower()}")
            button.clicked.connect(lambda _=False, p=link.path: self.play_requested.emit(p))
            self.buttons.addWidget(button)
        self.buttons.addStretch(1)

    def play_buttons(self) -> list[QPushButton]:
        return [self.buttons.itemAt(i).widget() for i in range(self.buttons.count())
                if isinstance(self.buttons.itemAt(i).widget(), QPushButton)]
