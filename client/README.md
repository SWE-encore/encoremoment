# PC client integration

See the [root README](../README.md) for the common entry point and team workflow, and the [architecture document](../docs/architecture.md) for the current component boundaries.

- Normal launch: `python -m encore_client` (run from this folder)
- Built-in demo server and integrated UI: `python -m encore_client --demo`
- Windows UI development: `python -m encore_client --demo --no-player`
- Automated delivery check: `python -m encore_client --demo --headless`
- Integration tests: `python -m pytest -q` from the repository root

`ui_main.py` and `python -m encore_client.session` are aliases for the common entry point.
The documentation below preserves implementation details for the player and EM-23. Earlier test counts describe their original validation; consult the PR for the latest integration results.

# PC client

The Encore Moment PC client (Python, PySide6, GStreamer). It currently contains the **controlled player**: the base that the session, sensing and clip features build on.

> **Summary** — The player supports local video playback, pause, seek, and speed changes. Playback changes produce contract (`contracts/`) `playback_anchor` messages that map reaction capture times to content timecodes. Audio is routed at 48 kHz through `webrtcechoprobe`, allowing echo cancellation (R1) to be added to the same pipeline later.

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

> **Summary** — Start and end sessions and display reaction events delivered by the server. Messages follow the reference receiver rules in `contracts/`, so duplicate results leave a single event. A dropped connection shows a red "Disconnected" status and its reason. Until the real server (EM-3) exists, `fake_server` sends the contract fixtures. Run instructions and reproduction records: [docs/em-23-delivery.md](docs/em-23-delivery.md).

- **Session.** Start opens the result stream `ws://<server>/v1/sessions/<session_id>/stream` and sends the player's newest playback anchor (title and time basis) as soon as it opens; later anchors follow while connected. End closes the stream.
- **Delivery.** Every server message goes through `contracts/reference/encore_contract.py` (loaded, not copied): cursor order, one event per `event_id`, newer revisions only, stale generations refused.
- **Connection status.** Not started → Connecting → Connected → Disconnected (with the reason) or Session ended. A peer that answers nothing for 15 s counts as dropped. While disconnected, results are greyed out as possibly stale and outgoing messages are kept in `unsent`; resending them and replaying results is EM-20.
- **Sensor controls.** `Session.set_sensor(sensor, enabled)` sends a `control_update` with the next generation (or the device's own, e.g. `CameraController.generation`). A live session turns the microphone on at Start (R11), since new events are only accepted while it is on; controls made before the connection opens are sent first. Everything the PC sends shares one `seq` counter.
- **Delivery log.** `--log FILE` writes every step (start, open, in, out, unsent, closed, end) as JSON Lines.
- **Microphone audio.** `Session.send_audio(pcm, capture_ms)` sends 16 kHz mono PCM16 in binary frames (contracts/README.md, "Audio frames"), only while the microphone is on and numbered per generation. Audio captured while disconnected is dropped, not sent late. Until real capture exists, `--mic-wav FILE.wav` streams a file in real time as a stand-in microphone (`sensing/wav_mic.py`).
- **WebSocket.** `session/ws.py` is a small standard-library RFC 6455 implementation (text and binary frames, ping/pong, close, `wss://`), so nothing new needs installing.

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

Fake server options: `--interval` (seconds before each server message, default 0.5), `--drop-after N` (cut the connection after N messages), `--save-audio FILE.wav` (write the audio it receives), `--port`.

To check audio upload, stream a WAV and compare what the fake server saved (convert other files first with `ffmpeg -i IN -ac 1 -ar 16000 -sample_fmt s16 OUT.wav`):

```sh
cd client
# terminal 1
../.venv/bin/python -m encore_client.session.fake_server ../contracts/samples/07_microphone_audio.json --save-audio received.wav
# terminal 2: a live session (microphone on at Start) streaming the file
../.venv/bin/python -m encore_client --headless --session-id ses-sample-07 --mic-wav laugh.wav --timeout 5
```

## Live microphone without the player

On machines without the Ubuntu player (for example macOS), the main window can still run a live session from the PC microphone and show the results in the normal timeline and Highlights:

```sh
cd client
../.venv/bin/python -m encore_client --no-player --mic --local-server loudness
# stricter loudness trigger in a noisy room
../.venv/bin/python -m encore_client --no-player --mic --local-server loudness --trigger laughter=0.6
# or a session server started elsewhere (for example through an SSH tunnel)
../.venv/bin/python -m encore_client --no-player --mic --server ws://127.0.0.1:8765
```

**Microphone on/off (R8, EM-17).** With `--mic` or `--mic-wav`, a control under the sensing line shows the actual microphone state and turns it off and on during a session. Off closes the capture device, stops the upload and sends `control_update` with a new generation, so new events from the old generation are refused on the PC and on the server; an event that was already open is still closed. On opens the device again in a new generation. The camera state is never changed by it. Start turns the microphone on; End closes the device. If the device cannot open, the microphone stays off and the error is shown.

`--local-server` starts the test bench's built-in session server (`loudness` stand-in or `yamnet`). Without a player there is no content position, so Start sends a *microphone clock* anchor (`content_id: mic-clock`): timeline times are seconds since Start, and the title says so. Nothing is echo-cancelled (`aec: off`); use headphones if anything else is playing. This is a demonstration path, not the product's capture path (R1).

## Test bench

`python -m encore_client.bench` tries reaction detection without the player, a second terminal or a server account. One window starts a session server inside the app, streams audio from the PC microphone or a file, and shows the model's scores live next to the events the PC receives through the normal contract path.

```sh
cd client
../.venv/bin/python -m encore_client.bench
```

Or, from anywhere, `scripts/encore-bench` in the repository: it finds the checkout's `.venv` and uses YAMNet when TensorFlow is installed there. To start it from the app menu or dock instead, run `scripts/install-bench-shortcut.sh` once (Ubuntu and other freedesktop desktops; `--remove` takes it out). Started that way, its output goes to `~/.cache/encore/bench.log` and a failure shows as a notification.

- **Audio in.** The default microphone, or any audio or video file (whatever ffmpeg reads is converted to 16 kHz mono). Files play at 1×, 4× or 16×, and an event's content time is its position in the file. Nothing is echo-cancelled, so use headphones if anything else is playing. If the input delivers exact silence for 1.5 s, the bench says the microphone is probably muted in the system settings.
- **Server.** Built-in, with the loudness stand-in (any loud sound counts as laughter) or YAMNet; or the URL of a server started separately (`python -m encore_server`, possibly through an SSH tunnel). Scores are visible only with the built-in server; events show either way.
- **Trigger levels.** The detector's level per reaction, applied at the next Start. Results from non-default levels say so in `source.config`, for example `bench gasp=0.30 laughter=0.30 scream=0.40`.
- **Plot.** The last 30 s: each reaction's score (solid), room intensity (dotted), trigger levels (dashed), and every received event as a band from its start to its end.
- **Stop** turns the microphone off first, so the server finishes open events (R8), then ends the session. **Save recording…** keeps the microphone audio of the last run as a contract-format WAV, to replay it with other levels or to share with the model owner.

YAMNet needs TensorFlow in the same environment (about 2 GB installed; the model downloads once on first use):

```sh
../.venv/bin/python -m pip install -r ../server/requirements-yamnet.txt
../.venv/bin/python -m encore_client.bench --model yamnet
```

Other options: `--server URL`, `--file PATH`, `--speed 1|4|16`.

Microphones that cannot supply 16 kHz mono PCM16 use their supported native
format instead. The bench downmixes and resamples it to the contract format
with NumPy and SoXR (included in `requirements-dev.txt` via the client
requirements). Streaming conversion preserves filter state between device
reads and accounts for resampler delay in capture timestamps.

Stop closes the capture device immediately while waiting for final events.
A lost server connection also stops capture, preserves the last recording for
saving, and leaves the disconnect reason visible. Press Start to begin a new run.

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
| `encore_client/sensing/wav_mic.py`, `live_mic.py` | Stand-in microphones: a WAV file in real time, or the raw PC microphone (no AEC) |
| `encore_client/bench/` | Test bench: built-in server, live scores plot, events (development only) |
| `tools/make_test_video.sh` | Generates test clips with ffmpeg (nothing copyrighted is committed) |

## Known limits and risks

- **Echo cancellation plugin missing on Ubuntu 24.04.** `gstreamer1.0-plugins-bad` 1.24.2 there does not ship `webrtcdsp`/`webrtcechoprobe`, and no other apt package provides them. R1 needs them: build that plugin from source against `libwebrtc-audio-processing`, use a distribution that packages it, or choose another echo-cancel route (for example PipeWire's echo-cancel module). This decision belongs to R1 (EM-10).
- Anchor capture time is taken when GStreamer reports the change, without subtracting audio output latency (typically tens of ms). The 10 s heartbeat bounds drift.
- Frames are copied into Qt as RGBx at full resolution. Fine for 1080p; 4K would need GL rendering or downscaling in `VIDEO_CAPS`.
- A paused frame can be about two frames ahead of the reported position because of the sink's one-buffer queue.
- Muting uses −80 dB instead of 0, because `volume=0` makes GStreamer send gap buffers that stop the audio clock.
