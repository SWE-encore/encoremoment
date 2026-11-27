"""Download clip: save a saved clip's chosen part as MP4 (clips/download.py).

A clip without a screen recording downloads as one camera video. For a clip
with one, the user picks two files (camera and screen, the same length) or
the camera on the screen, and places the camera box on a still of the screen:
drag the box to move it, drag its corner or turn the wheel to resize it, or use
the Size slider. The next download in this run starts from the same place.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPointF, QProcess, QRectF, QSize, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLayout, QMessageBox,
                               QProgressBar, QPushButton, QRadioButton, QSlider, QVBoxLayout, QWidget)

from ..clips import download
from ..clips.model import playable_path

last_box = download.Box()   # where the previous download in this run put the camera box
AMBER = QColor("#f0b400")


def frame_image(path: Path, ms: int, ffmpeg: str) -> QImage | None:
    """One frame of ``path`` at ``ms``, for the placement preview."""
    try:
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{ms / 1000:.3f}",
             "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    image = QImage.fromData(result.stdout, "PNG") if result.returncode == 0 else QImage()
    return None if image.isNull() else image


class BoxPlacer(QWidget):
    """A still of the screen with the camera box on it."""
    changed = Signal()
    HANDLE = 14
    WIDTH = 480   # the still's width in the dialog

    def __init__(self, screen_image, camera_image, screen_size, camera_size, box, parent=None):
        super().__init__(parent)
        self.screen_image, self.camera_image = screen_image, camera_image
        self.screen_aspect = screen_size[0] / screen_size[1]
        self.camera_aspect = camera_size[0] / camera_size[1]
        self.box = box.clamped(self.camera_aspect, self.screen_aspect)
        self._drag = None   # (what, press position, box when pressed)
        self.setMouseTracking(True)
        self.setMinimumSize(360, round(360 / self.screen_aspect))
        self.setToolTip("Drag the camera box to move it; drag its corner or turn the wheel to resize it.")

    def sizeHint(self) -> QSize:
        return QSize(self.WIDTH, round(self.WIDTH / self.screen_aspect))

    def set_box(self, box) -> None:
        box = box.clamped(self.camera_aspect, self.screen_aspect)
        if box != self.box:
            self.box = box
            self.update()
            self.changed.emit()

    def picture_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        if w / h > self.screen_aspect:
            pw = h * self.screen_aspect
            return QRectF((w - pw) / 2, 0, pw, h)
        ph = w / self.screen_aspect
        return QRectF(0, (h - ph) / 2, w, ph)

    def box_rect(self) -> QRectF:
        picture = self.picture_rect()
        height = self.box.height(self.camera_aspect, self.screen_aspect)
        return QRectF(picture.x() + self.box.x * picture.width(), picture.y() + self.box.y * picture.height(),
                      self.box.width * picture.width(), height * picture.height())

    def handle_rect(self) -> QRectF:
        corner = self.box_rect().bottomRight()
        return QRectF(corner.x() - self.HANDLE, corner.y() - self.HANDLE, self.HANDLE, self.HANDLE)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202124"))
        picture = self.picture_rect()
        if self.screen_image is not None:
            painter.drawImage(picture, self.screen_image)
        else:
            painter.fillRect(picture, Qt.GlobalColor.black)
            painter.setPen(QColor("#bbbbbb"))
            painter.drawText(picture, Qt.AlignmentFlag.AlignCenter, "Screen picture not available")
        box = self.box_rect()
        if self.camera_image is not None:
            painter.drawImage(box, self.camera_image)
        else:
            painter.fillRect(box, QColor("#555555"))
        painter.setPen(QPen(AMBER, 2))
        painter.drawRect(box)
        painter.fillRect(self.handle_rect(), AMBER)

    def _where(self, pos: QPointF):
        if self.handle_rect().adjusted(-4, -4, 4, 4).contains(pos):
            return "resize"
        return "move" if self.box_rect().contains(pos) else None

    def mousePressEvent(self, event):
        what = self._where(event.position())
        self._drag = (what, event.position(), self.box) if what else None

    def mouseMoveEvent(self, event):
        if self._drag is None:
            what = self._where(event.position())
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if what == "resize"
                           else Qt.CursorShape.SizeAllCursor if what == "move" else Qt.CursorShape.ArrowCursor)
            return
        what, start, box = self._drag
        picture = self.picture_rect()
        dx = (event.position().x() - start.x()) / picture.width()
        dy = (event.position().y() - start.y()) / picture.height()
        if what == "move":
            self.set_box(replace(box, x=box.x + dx, y=box.y + dy))
        else:
            self.set_box(replace(box, width=box.width + dx))

    def mouseReleaseEvent(self, _event):
        self._drag = None

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        box = self.box
        width = box.width + 0.02 * steps
        grow = (width - box.width) / 2
        ratio = self.screen_aspect / self.camera_aspect
        self.set_box(replace(box, width=width, x=box.x - grow, y=box.y - grow * ratio))


class DownloadDialog(QDialog):
    """Save ``clip`` (its ``range_ms`` of the file, or its borders) as MP4."""
    saved = Signal(bool)   # True when the files are written, False when it failed

    def __init__(self, clip, base_dir: Path, range_ms=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Download clip")
        self.clip = clip
        self.process = None
        self.job = None
        self.saved_paths: tuple[Path, ...] = ()
        self.error = None
        self.screen_media = self.screen_path = None
        self.pieces = ()
        # Replaced in tests.
        self.ask_path = self._ask_path
        self.confirm_replace = self._confirm_replace

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.two_files = QRadioButton("Two files: camera and screen (the same length)")
        self.overlay = QRadioButton("Camera on the screen: one video")
        self.placer = None
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(round(download.MIN_BOX_WIDTH * 100), 100)
        self.size_label = QLabel("Camera box size")
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.save_btn = QPushButton("Save…")
        self.save_btn.setDefault(True)
        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.setVisible(False)
        self.close_btn = QPushButton("Cancel")
        self.save_btn.clicked.connect(self.save)
        self.folder_btn.clicked.connect(self._open_folder)
        self.close_btn.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        # The dialog takes the size its contents need, so wrapped text, the progress bar and
        # the still never overlap; the width matches the still in every mode.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.info.setMinimumWidth(BoxPlacer.WIDTH)
        layout.addWidget(self.info)
        try:
            self._prepare(Path(base_dir), range_ms)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.error = f"Cannot download this clip: {exc}"
            self.save_btn.setEnabled(False)
        if self.has_screen():
            layout.addWidget(self.two_files)
            layout.addWidget(self.overlay)
            layout.addWidget(self.placer, 1)
            sizes = QHBoxLayout()
            sizes.addWidget(self.size_label)
            sizes.addWidget(self.size_slider, 1)
            layout.addLayout(sizes)
            self.two_files.setChecked(True)
            self.two_files.toggled.connect(self._show_mode)
            self.size_slider.valueChanged.connect(self._slider_moved)
            self.placer.changed.connect(self._box_moved)
            self._box_moved()
            self._show_mode()
        layout.addWidget(self.note)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for button in (self.folder_btn, self.save_btn, self.close_btn):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status.setText(self.error or "")

    # ---- what can be downloaded -----------------------------------------------------

    def _prepare(self, base_dir: Path, range_ms):
        found = download.tools()
        reason = download.unavailable_reason()
        if found is None or reason:
            raise ValueError(reason)
        self.ffmpeg, ffprobe = found
        self.encoder = download.h264_encoder(self.ffmpeg)
        self.camera_path = playable_path(self.clip, base_dir)
        if self.camera_path is None:
            raise ValueError("its video file is missing")
        self.camera = download.probe(self.camera_path, ffprobe)
        self.start_ms, self.end_ms = (tuple(range_ms) if range_ms
                                      else download.chosen_range(self.clip.trim_ms, self.camera.duration_ms))
        seconds = (self.end_ms - self.start_ms) / 1000
        self.info.setText(f"{self.clip.event_type} · {self.clip.timecode}: {seconds:.1f} s, "
                          "the part between the borders, as MP4.")
        if self.clip.screen:
            path = (base_dir / self.clip.screen[0].path).resolve()
            pieces = download.screen_pieces(self.clip.screen, self.start_ms, self.end_ms)
            if path.is_file() and any(p.screen_ms is not None for p in pieces):
                self.screen_path, self.pieces = path, pieces
                self.screen_media = download.probe(path, ffprobe)
        if self.screen_path is None:
            self.note.setText("This clip has no screen recording for this part, "
                              "so it downloads as one camera video.")
            return
        first = next(p for p in self.pieces if p.screen_ms is not None)
        self.placer = BoxPlacer(frame_image(self.screen_path, first.screen_ms, self.ffmpeg),
                                frame_image(self.camera_path, self.start_ms, self.ffmpeg),
                                self.screen_media.size, self.camera.size, last_box)

    def has_screen(self) -> bool:
        return self.screen_path is not None and self.placer is not None

    def mode(self) -> str:
        if not self.has_screen():
            return download.CAMERA
        return download.TWO_FILES if self.two_files.isChecked() else download.OVERLAY

    def _show_mode(self):
        overlay = self.mode() == download.OVERLAY
        for widget in (self.placer, self.size_label, self.size_slider):
            widget.setVisible(overlay)
        self.note.setText(
            "Drag the camera box to place it; drag its corner, turn the wheel or use the slider to resize it. "
            "Moments the screen was not recorded are black." if overlay else
            "Saves NAME-camera.mp4 (with the room sound) and NAME-screen.mp4 (no sound: the screen's "
            "sound is not recorded). Moments the screen was not recorded are black.")

    def _box_moved(self):
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(round(self.placer.box.width * 100))
        self.size_slider.blockSignals(False)

    def _slider_moved(self, value):
        box = self.placer.box
        self.placer.set_box(replace(box, width=value / 100))

    # ---- saving ---------------------------------------------------------------------

    def _ask_path(self, suggested: Path) -> str:
        path, _ = QFileDialog.getSaveFileName(self, "Save clip as", str(suggested), "MP4 video (*.mp4)")
        return path

    def _confirm_replace(self, paths) -> bool:
        names = "\n".join(p.name for p in paths)
        answer = QMessageBox.question(self, "Replace files", f"These files already exist:\n{names}\n\nReplace them?")
        return answer == QMessageBox.StandardButton.Yes

    def save(self) -> bool:
        """Ask where, then start writing. False if nothing was started."""
        if self.process is not None:
            return False
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        chosen = self.ask_path(Path(folder or Path.home()) / f"{download.default_name(self.clip)}.mp4")
        if not chosen:
            return False
        mode = self.mode()
        outputs = download.output_paths(Path(chosen), mode)
        # The file dialog already asked about the chosen name itself.
        existing = [p for p in outputs if p.exists() and p != Path(chosen)]
        if existing and not self.confirm_replace(existing):
            return False
        global last_box
        box = self.placer.box if self.has_screen() else last_box
        last_box = box
        self.job = download.Job(self.camera_path, self.start_ms, self.end_ms, outputs, mode,
                                self.screen_path, self.pieces, box)
        try:
            args = download.command(self.job, self.camera, self.screen_media,
                                    ffmpeg=self.ffmpeg, encoder=self.encoder)
        except ValueError as exc:
            self._failed(str(exc))
            return False
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._read_progress)
        self.process.finished.connect(self._done)
        self.process.errorOccurred.connect(self._process_error)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.save_btn.setEnabled(False)
        self.folder_btn.setVisible(False)
        self.close_btn.setText("Cancel")
        self.status.setText("Saving…")
        self.status.setToolTip("")
        self.process.start(args[0], args[1:])
        return True

    def _read_progress(self):
        text = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        for line in text.splitlines():
            done = download.progress(line, self.job)
            if done is not None:
                self.progress.setValue(round(done * 100))

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._done(-1, QProcess.ExitStatus.CrashExit)

    def _done(self, code, status):
        process, self.process = self.process, None
        if process is None:
            return
        errors = bytes(process.readAllStandardError()).decode(errors="replace").strip()
        process.deleteLater()
        try:
            if code != 0 or status != QProcess.ExitStatus.NormalExit:
                raise OSError(errors[-300:] or "ffmpeg stopped")
            download.finish(self.job)
        except OSError as exc:
            download.cleanup(self.job)
            self._failed(f"Could not save the clip: {exc}")
            return
        self.saved_paths = self.job.outputs
        self.progress.setValue(100)
        folder = self.saved_paths[0].parent
        self.status.setText("Saved " + " and ".join(p.name for p in self.saved_paths)
                            + f" in {folder.name or folder}.")
        self.status.setToolTip(str(folder))
        self.save_btn.setEnabled(True)
        self.folder_btn.setVisible(True)
        self.close_btn.setText("Close")
        self.saved.emit(True)

    def _failed(self, message):
        self.error = message
        self.status.setText(message)
        self.progress.setVisible(False)
        self.save_btn.setEnabled(True)
        self.close_btn.setText("Close")
        self.saved.emit(False)

    def _open_folder(self):
        if self.saved_paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.saved_paths[0].parent)))

    def cancel(self):
        """Stop a download in progress and remove what it wrote."""
        process, self.process = self.process, None
        if process is not None:
            process.kill()
            process.waitForFinished(3000)
            process.deleteLater()
            download.cleanup(self.job)

    def reject(self):
        self.cancel()
        super().reject()
