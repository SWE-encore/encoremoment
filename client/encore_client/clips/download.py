"""Download a saved clip as MP4, cut exactly to its chosen part.

The chosen part is what the borders under the progress bar mark (``trim_ms``,
clips/panel.py), or the whole file for a clip without one. The file is
re-encoded (H.264 + AAC, ``+faststart``), so the cut is exact and the video
plays almost everywhere.

A clip without a screen recording downloads as one camera video. A clip with
one (clips/screen.py) downloads either as two files of the same length,
``<name>-camera.mp4`` and ``<name>-screen.mp4`` (silent: the screen's sound is
not recorded), or as one video with the camera as a box over the screen
(``Box``: where and how big, chosen in ui/download_dialog.py). Moments the
screen was not recorded are black; its last frame holds for up to a second at
its end, as in the Screen view.

ffmpeg does the work (it is in the README's install line). The dialog runs
``command()`` in a QProcess and reads ``-progress`` from its output. Output goes
to ``.part`` files next to the chosen name and is renamed at the end
(``finish``), so a failed or cancelled download leaves nothing half written.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

CAMERA, TWO_FILES, OVERLAY = "camera", "two files", "camera on the screen"
TAIL_MS = 1_000          # the screen's last frame holds this long (ui/screen_view.py)
LAST_FRAME_MS = 100      # a screen piece starting after the file's end shows its last frame
DEFAULT_FPS = 10
MIN_BOX_WIDTH = 0.1      # of the screen's width
H264_ENCODERS = ("libx264", "libopenh264")


# ---- what to cut -------------------------------------------------------------------

def chosen_range(trim, duration_ms: int) -> tuple[int, int]:
    """The part of the file between the borders, ``(start, end)`` ms; the whole
    file without a valid ``trim_ms``."""
    if len(trim) == 2 and 0 <= trim[0] < trim[1] and trim[0] < duration_ms:
        return int(trim[0]), min(int(trim[1]), duration_ms)
    return 0, duration_ms


@dataclass(frozen=True)
class Piece:
    """A stretch of the screen video: ``duration_ms`` long, starting at
    ``screen_ms`` of the screen file, or black (None)."""
    duration_ms: int
    screen_ms: int | None = None


def screen_pieces(segments, start_ms: int, end_ms: int) -> tuple[Piece, ...]:
    """The screen for ``start_ms``-``end_ms`` of the camera file, from the clip's
    ScreenSegments (clips/screen_map.py), back to back."""
    segments = sorted(segments, key=lambda s: s.media_ms[0])
    pieces, t = [], start_ms
    for i, segment in enumerate(segments):
        a, b = segment.media_ms
        held = b + TAIL_MS
        if i + 1 < len(segments):
            held = min(held, segments[i + 1].media_ms[0])
        lo, hi = max(a, t), min(held, end_ms)
        if hi <= lo:
            continue
        if lo > t:
            pieces.append(Piece(lo - t))
        pieces.append(Piece(hi - lo, segment.screen_at(min(lo, max(a, b - LAST_FRAME_MS)))))
        t = hi
    if t < end_ms:
        pieces.append(Piece(end_ms - t))
    return tuple(pieces)


@dataclass(frozen=True)
class Box:
    """The camera box over the screen: its left and top edges and its width as
    fractions of the screen picture. Its height keeps the camera's shape."""
    x: float = 0.73
    y: float = 0.71
    width: float = 0.25

    def height(self, camera_aspect: float, screen_aspect: float) -> float:
        return self.width * screen_aspect / camera_aspect

    def clamped(self, camera_aspect: float, screen_aspect: float) -> "Box":
        """Inside the picture, at least ``MIN_BOX_WIDTH`` wide and no taller than it."""
        widest = min(1.0, camera_aspect / screen_aspect)
        width = min(widest, max(MIN_BOX_WIDTH, self.width))
        box = replace(self, width=width)
        height = box.height(camera_aspect, screen_aspect)
        return replace(box, x=min(1.0 - width, max(0.0, self.x)), y=min(1.0 - height, max(0.0, self.y)))

    def pixels(self, screen_size, camera_size) -> tuple[int, int, int, int]:
        """``(x, y, width, height)`` in the screen picture, even sizes for H.264."""
        (sw, sh), (cw, ch) = screen_size, camera_size
        box = self.clamped(cw / ch, sw / sh)
        width = max(2, round(box.width * sw / 2) * 2)
        height = max(2, min(sh, round(width * ch / cw / 2) * 2))
        return (min(sw - width, max(0, round(box.x * sw))), min(sh - height, max(0, round(box.y * sh))),
                width, height)


@dataclass(frozen=True)
class Job:
    """One download: ``outputs`` are the final files (two for ``TWO_FILES``:
    camera, then screen)."""
    camera: Path
    start_ms: int
    end_ms: int
    outputs: tuple[Path, ...]
    mode: str = CAMERA
    screen: Path | None = None
    pieces: tuple[Piece, ...] = ()
    box: Box = Box()

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def parts(self) -> tuple[Path, ...]:
        return tuple(path.with_name(path.name + ".part") for path in self.outputs)


def default_name(clip) -> str:
    """``encore-laughter-00-12-05``: safe in any file system."""
    name = f"encore-{clip.event_type}-{clip.timecode}"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def output_paths(chosen: Path, mode: str) -> tuple[Path, ...]:
    """The files a download writes for the name the user chose."""
    chosen = Path(chosen)
    if chosen.suffix.lower() != ".mp4":
        chosen = chosen.with_name(chosen.name + ".mp4")
    if mode != TWO_FILES:
        return (chosen,)
    return chosen.with_name(f"{chosen.stem}-camera.mp4"), chosen.with_name(f"{chosen.stem}-screen.mp4")


# ---- ffmpeg ------------------------------------------------------------------------

@dataclass(frozen=True)
class Media:
    width: int
    height: int
    fps: int
    audio: bool
    duration_ms: int

    @property
    def size(self) -> tuple[int, int]:
        return _even(self.width), _even(self.height)


def tools() -> tuple[str, str] | None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return (ffmpeg, ffprobe) if ffmpeg and ffprobe else None


@lru_cache(maxsize=4)
def h264_encoder(ffmpeg: str) -> str | None:
    try:
        listing = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                 text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return next((name for name in H264_ENCODERS
                 if re.search(rf"^\s*V\S*\s+{re.escape(name)}\s", listing, re.MULTILINE)), None)


def unavailable_reason() -> str | None:
    """Why clips cannot be downloaded here, or None."""
    found = tools()
    if found is None:
        return "ffmpeg is not installed (sudo apt install ffmpeg)"
    if h264_encoder(found[0]) is None:
        return "this ffmpeg has no H.264 encoder (libx264 or libopenh264)"
    return None


def probe(path: Path, ffprobe: str = "ffprobe") -> Media:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"cannot read {Path(path).name}: {result.stderr.strip()[-200:]}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"{Path(path).name} has no video")
    return Media(int(video["width"]), int(video["height"]), _fps(video),
                 any(s.get("codec_type") == "audio" for s in streams),
                 round(float(info.get("format", {}).get("duration", 0)) * 1000))


def _fps(video) -> int:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(video.get(key, "")).partition("/")
        try:
            rate = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= rate <= 60:
            return round(rate)
    return DEFAULT_FPS


def _even(n: int) -> int:
    return max(2, n // 2 * 2)


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def _exact(fps: int, ms: int) -> str:
    """Filters that make a video exactly ``ms`` long at ``fps``: the last frame
    holds if the input ends early."""
    seconds = _seconds(ms)
    return (f"fps={fps},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={seconds},"
            f"trim=duration={seconds},setpts=PTS-STARTPTS,format=yuv420p,setsar=1")


def command(job: Job, camera: Media, screen: Media | None = None, *,
            ffmpeg: str = "ffmpeg", encoder: str = "libx264") -> list[str]:
    """The ffmpeg command writing ``job.parts``; progress goes to stdout."""
    fps, length = camera.fps, _seconds(job.duration_ms)
    cw, ch = camera.size
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-y",
            "-ss", _seconds(job.start_ms), "-t", length, "-i", str(job.camera)]
    graph = [f"[0:v]scale={cw}:{ch},{_exact(fps, job.duration_ms)}[cam]"]
    if job.mode != CAMERA:
        if screen is None or job.screen is None or not job.pieces:
            raise ValueError("this clip has no screen recording")
        sw, sh = screen.size
        labels, inputs = [], 1
        for i, piece in enumerate(job.pieces):
            if piece.screen_ms is None:
                graph.append(f"color=c=black:s={sw}x{sh}:r={fps}:d={_seconds(piece.duration_ms)},"
                             f"format=yuv420p,setsar=1[s{i}]")
            else:
                args += ["-ss", _seconds(piece.screen_ms), "-t", _seconds(piece.duration_ms),
                         "-i", str(job.screen)]
                graph.append(f"[{inputs}:v]scale={sw}:{sh}:force_original_aspect_ratio=decrease,"
                             f"pad={sw}:{sh}:(ow-iw)/2:(oh-ih)/2,{_exact(fps, piece.duration_ms)}[s{i}]")
                inputs += 1
            labels.append(f"[s{i}]")
        graph.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[screen]")
    if job.mode == OVERLAY:
        x, y, w, h = job.box.pixels((sw, sh), (cw, ch))
        graph.append(f"[cam]scale={w}:{h}[box]")
        graph.append(f"[screen][box]overlay=x={x}:y={y}:eof_action=pass,format=yuv420p[out]")
    args += ["-filter_complex", ";".join(graph)]
    video = ["-c:v", encoder, "-pix_fmt", "yuv420p", "-r", str(fps)]
    video += ["-preset", "veryfast", "-crf", "20"] if encoder == "libx264" else ["-b:v", "4M"]
    sound = ["-map", "0:a?", "-c:a", "aac", "-b:a", "128k"]
    tail = ["-t", length, "-movflags", "+faststart", "-f", "mp4"]
    parts = job.parts
    if job.mode == CAMERA:
        args += ["-map", "[cam]", *video, *sound, *tail, str(parts[0])]
    elif job.mode == TWO_FILES:
        args += ["-map", "[cam]", *video, *sound, *tail, str(parts[0])]
        args += ["-map", "[screen]", *video, "-an", *tail, str(parts[1])]
    elif job.mode == OVERLAY:
        args += ["-map", "[out]", *video, *sound, *tail, str(parts[0])]
    else:
        raise ValueError(f"unknown download mode {job.mode!r}")
    return args


def progress(line: str, job: Job) -> float | None:
    """The finished fraction from one ``-progress`` line, if it says."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms") and value.isdigit() and job.duration_ms > 0:
        return min(1.0, int(value) / 1000 / job.duration_ms)
    if key == "progress" and value == "end":
        return 1.0
    return None


def finish(job: Job) -> None:
    """Rename the finished ``.part`` files to the chosen names."""
    for part, final in zip(job.parts, job.outputs):
        if not part.is_file() or not part.stat().st_size:
            raise OSError(f"{final.name} was not written")
    for part, final in zip(job.parts, job.outputs):
        os.replace(part, final)


def cleanup(job: Job) -> None:
    for part in job.parts:
        part.unlink(missing_ok=True)


def run(job: Job, camera: Media, screen: Media | None = None) -> None:
    """Download without the UI (tests, scripts): blocks until written."""
    ffmpeg, _ffprobe = tools() or ("ffmpeg", "ffprobe")
    encoder = h264_encoder(ffmpeg) or "libx264"
    try:
        result = subprocess.run(command(job, camera, screen, ffmpeg=ffmpeg, encoder=encoder),
                                capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise OSError(f"ffmpeg failed: {result.stderr.strip()[-300:]}")
        finish(job)
    finally:
        cleanup(job)
