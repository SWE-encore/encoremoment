# PC client integration

공통 실행 및 협업 기준은 [루트 README](../README.md), 현재 계층 경계는 [구조 문서](../docs/architecture.md)를 참고하세요.

- 일반 실행: `python -m encore_client` (이 폴더에서 실행)
- 내장 데모 서버 + 통합 UI: `python -m encore_client --demo`
- Windows UI 개발: `python -m encore_client --demo --no-player`
- 자동 전달 확인: `python -m encore_client --demo --headless`
- 통합 테스트: 저장소 루트에서 `python -m pytest -q`

`ui_main.py`와 `python -m encore_client.session`은 공통 진입점의 별칭입니다.
아래 문서는 기존 플레이어/EM-23 구현의 상세 참고 기록입니다. 예전 테스트 통과 횟수는 당시 기록이며 최신 통합 검증 결과는 PR을 확인하세요.

# PC client

The Encore Moment PC client (Python, PySide6, GStreamer). It currently contains the **controlled player**: the base that the session, sensing and clip features build on.

> **요약 (한국어)** — 로컬 영상을 재생하는 플레이어입니다. 재생·일시정지·탐색·배속을 지원하고, 재생 상태가 바뀔 때마다 계약(`contracts/`)의 `playback_anchor` 메시지를 만들어 반응 시각을 콘텐츠 타임코드로 바꿀 수 있게 합니다. 오디오는 48 kHz로 `webrtcechoprobe`를 거치도록 되어 있어 이후 같은 파이프라인에 에코 제거(R1)를 붙일 수 있습니다.

## What the player does

- Plays a local video file (MP4/H.264+AAC, WebM, MKV, …) with play/pause, seek (slider, ←/→ = ±5 s), speed (0.5–2×) and Space to toggle.
- Sends a **playback anchor** whenever playback changes: opened (paused at 0), play, pause, seek landed, speed change, end of file, and every 10 s while playing. Anchors are `playback_anchor` messages from [`contracts/`](../contracts/README.md). They are shown in the window and can be written to a JSON Lines file.
- **Epochs** follow the contract: a new epoch on every seek and every new file; pause, resume and speed change keep the epoch. `AnchorTracker.map_time(capture_ms)` turns any capture time into `(epoch, content timecode)`, or `None` if no anchor existed yet (contract status `pending`).
- Routes audio through `volume ! webrtcechoprobe` at 48 kHz in the same top-level pipeline, so the microphone branch with `webrtcdsp` can be added there later (Submission B, R1). If the probe plugin is missing the player still plays and the status bar says the echo reference is unavailable.

Session start/end and result delivery are in `encore_client/session/` (EM-23, see below). Not included yet: camera/microphone capture, reconnect and replay (EM-20), playlists or subtitles.

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

## Session and result delivery (EM-23)

> **요약 (한국어)** — 세션을 시작·종료하고, 서버가 보낸 반응 이벤트를 PC 화면에 띄웁니다. 받은 메시지는 `contracts/`의 참조 수신기 규칙으로 처리하므로 같은 결과가 두 번 와도 이벤트는 하나만 남습니다. 연결이 끊기면 빨간 "Disconnected"와 이유를 보여 줍니다. 실제 서버(EM-3) 전에는 `fake_server`가 계약 샘플을 보내 줍니다. 실행 절차와 재현 기록: [docs/em-23-delivery.md](docs/em-23-delivery.md).

- **Session.** Start opens the result stream `ws://<server>/v1/sessions/<session_id>/stream` and sends the player's newest playback anchor (title and time basis) as soon as it opens; later anchors follow while connected. End closes the stream.
- **Delivery.** Every server message goes through `contracts/reference/encore_contract.py` (loaded, not copied): cursor order, one event per `event_id`, newer revisions only, stale generations refused.
- **Connection status.** Not started → Connecting → Connected → Disconnected (with the reason) or Session ended. A peer that answers nothing for 15 s counts as dropped. While disconnected, results are greyed out as possibly stale and outgoing messages are kept in `unsent`; resending them and replaying results is EM-20.
- **Delivery log.** `--log FILE` writes every step (start, open, in, out, unsent, closed, end) as JSON Lines.
- **WebSocket.** `session/ws.py` is a small standard-library RFC 6455 implementation (text frames, ping/pong, close, `wss://`), so nothing new needs installing.

```sh
cd client
# terminal 1: the fake server plays the server side of a sample
../.venv/bin/python -m encore_client.session.fake_server ../contracts/samples/04_duplicate_delivery.json
# terminal 2: player + session panel, playing the PC side of the same sample
../.venv/bin/python -m encore_client.session samples/test-movie.mp4 \
    --scenario ../contracts/samples/04_duplicate_delivery.json --log delivery.jsonl
# or without a window
../.venv/bin/python -m encore_client.session --scenario ../contracts/samples/04_duplicate_delivery.json --headless
```

Fake server options: `--interval` (seconds before each server message, default 0.5), `--drop-after N` (cut the connection after N messages), `--port`.

## Test

```sh
cd client
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m unittest discover -s tests -v
```

The session tests (`tests/test_session_delivery.py`) need neither Qt nor GStreamer: `python3 -m unittest tests.test_session_delivery -v`.

The player tests generate their own clips with ffmpeg, play them headless, and check anchors, epochs, positions (±50–250 ms), speed change, end of file, the paused first frame and the window. Set `ENCORE_SCREENSHOT=out.png` to save what the window rendered.

## Layout

| Path | Purpose |
|---|---|
| `encore_client/clock.py` | `monotonic_ms()`: the only clock used for capture times |
| `encore_client/player/anchors.py` | Epochs, anchors, timecode mapping. No GStreamer/Qt, reusable by EM-23 |
| `encore_client/player/pipeline.py` | GStreamer pipeline; `poll()` handles the bus, sends anchors, returns the newest frame |
| `encore_client/player/window.py` | PySide6 window: video view, controls, anchor log |
| `encore_client/session/session.py` | `Session` (connection state, send/receive, delivery log), `event_rows`, `ScenarioDriver`. No Qt |
| `encore_client/session/ws.py` | Standard-library WebSocket client/server |
| `encore_client/session/fake_server.py` | Plays the server side of a contract sample (development only) |
| `encore_client/session/panel.py` | PySide6 panel: Start/End, connection status, event table |
| `encore_client/session/contract.py` | Loads the shared receiver from `contracts/reference/` |
| `tools/make_test_video.sh` | Generates test clips with ffmpeg (nothing copyrighted is committed) |

## Known limits and risks

- **Echo cancellation plugin missing on Ubuntu 24.04.** `gstreamer1.0-plugins-bad` 1.24.2 there does not ship `webrtcdsp`/`webrtcechoprobe`, and no other apt package provides them. R1 needs them: build that plugin from source against `libwebrtc-audio-processing`, use a distribution that packages it, or choose another echo-cancel route (for example PipeWire's echo-cancel module). This decision belongs to R1 (EM-10).
- Anchor capture time is taken when GStreamer reports the change, without subtracting audio output latency (typically tens of ms). The 10 s heartbeat bounds drift.
- Frames are copied into Qt as RGBx at full resolution. Fine for 1080p; 4K would need GL rendering or downscaling in `VIDEO_CAPS`.
- A paused frame can be about two frames ahead of the reported position because of the sink's one-buffer queue.
- Muting uses −80 dB instead of 0, because `volume=0` makes GStreamer send gap buffers that stop the audio clock.
