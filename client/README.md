# PC client

The Encore Moment PC client (Python, PySide6, GStreamer). It currently contains the **controlled player**: the base that the session, sensing and clip features build on.

> **요약 (한국어)** — 로컬 영상을 재생하는 플레이어입니다. 재생·일시정지·탐색·배속을 지원하고, 재생 상태가 바뀔 때마다 계약(`contracts/`)의 `playback_anchor` 메시지를 만들어 반응 시각을 콘텐츠 타임코드로 바꿀 수 있게 합니다. 오디오는 48 kHz로 `webrtcechoprobe`를 거치도록 되어 있어 이후 같은 파이프라인에 에코 제거(R1)를 붙일 수 있습니다.

## What the player does

- Plays a local video file (MP4/H.264+AAC, WebM, MKV, …) with play/pause, seek (slider, ←/→ = ±5 s), speed (0.5–2×) and Space to toggle.
- Sends a **playback anchor** whenever playback changes: opened (paused at 0), play, pause, seek landed, speed change, end of file, and every 10 s while playing. Anchors are `playback_anchor` messages from [`contracts/`](../contracts/README.md). They are shown in the window and can be written to a JSON Lines file.
- **Epochs** follow the contract: a new epoch on every seek and every new file; pause, resume and speed change keep the epoch. `AnchorTracker.map_time(capture_ms)` turns any capture time into `(epoch, content timecode)`, or `None` if no anchor existed yet (contract status `pending`).
- Routes audio through `volume ! webrtcechoprobe` at 48 kHz in the same top-level pipeline, so the microphone branch with `webrtcdsp` can be added there later (Submission B, R1). If the probe plugin is missing the player still plays and the status bar says the echo reference is unavailable.

Not included yet: session start/end, sending anchors to the server (EM-23), camera/microphone capture, playlists or subtitles.

## Setup (Ubuntu 24.04)

```sh
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav ffmpeg
python3 -m venv --system-site-packages .venv   # system site packages provide PyGObject
.venv/bin/pip install -r client/requirements.txt
```

`gstreamer1.0-libav` is optional but widens codec support. Without it, H.264 uses `openh264dec`/`vah264dec` and AAC uses `faad` from the other plugin sets.

## Run

```sh
cd client
tools/make_test_video.sh samples/test-movie.mp4 30   # clip with an on-screen clock and a beep every second
../.venv/bin/python -m encore_client.player samples/test-movie.mp4 --anchors-out anchors.jsonl
```

Options: `--title` (reported title, default file name), `--session-id`, `--autoplay`, `--audio-sink "fakesink sync=true"` (no sound device).

## Test

```sh
cd client
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m unittest discover -s tests -v
```

The tests generate their own clips with ffmpeg, play them headless, and check anchors, epochs, positions (±50–250 ms), speed change, end of file, the paused first frame and the window. Set `ENCORE_SCREENSHOT=out.png` to save what the window rendered.

## Layout

| Path | Purpose |
|---|---|
| `encore_client/clock.py` | `monotonic_ms()`: the only clock used for capture times |
| `encore_client/player/anchors.py` | Epochs, anchors, timecode mapping. No GStreamer/Qt, reusable by EM-23 |
| `encore_client/player/pipeline.py` | GStreamer pipeline; `poll()` handles the bus, sends anchors, returns the newest frame |
| `encore_client/player/window.py` | PySide6 window: video view, controls, anchor log |
| `tools/make_test_video.sh` | Generates test clips with ffmpeg (nothing copyrighted is committed) |

## Known limits and risks

- **Echo cancellation plugin missing on Ubuntu 24.04.** `gstreamer1.0-plugins-bad` 1.24.2 there does not ship `webrtcdsp`/`webrtcechoprobe`, and no other apt package provides them. R1 needs them: build that plugin from source against `libwebrtc-audio-processing`, use a distribution that packages it, or choose another echo-cancel route (for example PipeWire's echo-cancel module). This decision belongs to R1 (EM-10).
- Anchor capture time is taken when GStreamer reports the change, without subtracting audio output latency (typically tens of ms). The 10 s heartbeat bounds drift.
- Frames are copied into Qt as RGBx at full resolution. Fine for 1080p; 4K would need GL rendering or downscaling in `VIDEO_CAPS`.
- A paused frame can be about two frames ahead of the reported position because of the sink's one-buffer queue.
- Muting uses −80 dB instead of 0, because `volume=0` makes GStreamer send gap buffers that stop the audio clock.
