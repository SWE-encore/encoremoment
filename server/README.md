# Session server

The server side of a viewing session (EM-3, EM-11, EM-12). It speaks encore.v1 over one WebSocket per session, finds audience reactions in the PC's microphone audio with YAMNet, and streams the reaction events back to the PC.

```text
PC ──control_update / playback_anchor (text)──▶ ServerSession ──control_ack──▶ PC
   ──audio_chunk (binary, 0.5 s)──▶ windows (0.975 s every 0.48 s) ──▶ model ──▶ detector ──event_update──▶ PC
```

It is a first working server, not the full design: live sessions and replay cursors stay in memory. The optional PostgreSQL archive retains finalized reaction metadata across restarts.

## Run

For the containerized server, model verification, and an end-to-end retry smoke test, see the [Compose runbook](../deploy/README.md).

The quickest way to try it is the test bench (`client/README.md`, "Test bench"): one window that runs this server inside the app and shows the model's scores live, with a microphone or an audio file.

To run the server on its own, from the repository root, because the server loads `contracts/` from the checkout, like the client:

```sh
python -m venv .venv-server
.venv-server/bin/python -m pip install -r server/requirements-yamnet.txt
PYTHONPATH=server .venv-server/bin/python -m encore_server --model yamnet
```

The first start downloads YAMNet from TensorFlow Hub (set `TFHUB_CACHE_DIR` to keep it). TensorFlow runs on the CPU; one window takes a few milliseconds.

Without TensorFlow, `--model loudness` runs the same pipeline with a stand-in that calls any loud sound laughter. Every event it makes says `source.model: loudness-standin`, so its output cannot be mistaken for the real model's. The development requirements (`requirements-dev.txt`) include everything except TensorFlow, so the tests and the stand-in run in the shared `.venv`:

```sh
PYTHONPATH=server .venv/bin/python -m encore_server --model loudness
```

The server listens on `127.0.0.1:8765` by default. On a shared host, keep it there and reach it from the PC through an SSH tunnel instead of opening a port:

```sh
ssh -N -L 8765:127.0.0.1:8765 YOUR_ACCOUNT@SERVER_HOST
```

Then, on the PC, stream a WAV file as the microphone (`client/README.md` explains `--mic-wav`):

```sh
cd client
../.venv/bin/python -m encore_client --headless --mic-wav laugh.wav --timeout 5
```

The server prints each acknowledgement and event as it commits it.

## Optional PostgreSQL event history (EM-29)

For a separate test run, apply the migration to a dedicated PostgreSQL database (local or RDS) and run the server on a separate port. Set `DATABASE_URL` in the server environment. On RDS, use the private endpoint and supply `sslmode=verify-full` and `sslrootcert` with the AWS CA bundle path in the connection settings. Keep credentials on the server. The runtime role needs SELECT and INSERT on `reaction_events`. Use a schema owner for the migration.

```sh
.venv/bin/python -m pip install -r server/requirements-history.txt
# With PGHOST, PGDATABASE and PGUSER set for the schema owner:
psql -v ON_ERROR_STOP=1 -f server/sql/001_reaction_events.sql
PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --record-history --port 8876
curl http://127.0.0.1:8876/v1/sessions/SESSION_ID/events
```

With `--record-history`, the server commits each FINAL's session ID, event ID and JSON metadata to a local SQLite outbox before sending the FINAL to the PC. The background writer removes each row only after PostgreSQL commits it. A restart drains pending rows even if the PC never reconnects. If the local disk cannot commit, the stream closes before that FINAL is delivered and reconnect retries it. Raw media stays on the PC.

`ENCORE_HISTORY_OUTBOX` selects the SQLite file. The default is `encore-server/history-outbox.sqlite3` under `XDG_STATE_HOME` (normally `~/.local/state`). Use a private, persistent directory dedicated to the destination database, including across container replacements. For example, mount `/var/lib/encore` and set `ENCORE_HISTORY_OUTBOX=/var/lib/encore/history-outbox.sqlite3`. Keep that filesystem and its pending rows when rotating PostgreSQL credentials. Starting without `--record-history` leaves it untouched.

The history route uses the existing loopback/SSH-tunnel trust model. Its JSON response contains `session_id` and an `events` array in recording order. A session with no archived events returns an empty array. The unflagged server runs with the original in-memory behavior, including when `DATABASE_URL` is present. WebM files stay on the PC.

In history mode, the server sends live events and microphone acknowledgements immediately. A background writer archives FINAL metadata, including FINALs created when a PC disconnects normally. If PostgreSQL becomes unavailable, the writer retries while the server is running, even after the PC leaves. The history endpoint flushes pending entries for the requested session before reading PostgreSQL and returns HTTP 503 during an outage. An orderly server stop finalizes open reactions and attempts a last flush. A restart with pending rows can resume serving while PostgreSQL is unavailable. A new process can reuse a session ID with fresh event IDs and retrieve previously archived events.

To run the database integration test, point `ENCORE_TEST_DATABASE_URL` at a dedicated test database and run `python -m pytest -q server/tests/test_event_history.py` with the history and test dependencies installed.

## What it does

| Part | File | Behaviour |
|---|---|---|
| Stream | `server.py` | `/v1/sessions/<session_id>/stream`; any other path gets 404. On connect it first sends every committed result after `?after=<cursor>` (default 0), so a reconnecting PC catches up (R11). A new connection for the same session replaces the old one. A normal close (1000, the PC's End) ends the session; a dropped connection leaves it open. |
| Session state | `session.py` | Mirrors the PC's state with the contract's own receiver (`SessionView`), so sensor generations, anchors and audio admission follow exactly the rules the PC applies. Every control gets a `control_ack`: `applied`, `duplicate` (a retried idempotency key) or `rejected` with the receiver's reason. Invalid messages are counted and not answered. |
| Audio admission | `session.py` | Audio is analysed only while the microphone is on, in its current generation (R8). Retried chunks are ignored, and old-generation chunks still in flight after the microphone is turned off are never analysed. |
| Windows | `audio.py` | 0.5 s chunks become 0.975 s windows every 0.48 s (one YAMNet frame each). A missing chunk, a time jump or a new generation starts a fresh run, so no window spans missing audio. |
| Model | `models.py` | YAMNet's AudioSet classes, combined per reaction: laughter = Laughter, Baby laughter, Giggle, Snicker, Belly laugh, Chuckle/chortle; scream = Screaming; gasp = Gasp. |
| Intensity | `audio.py` | Loudness above the room's quiet level (10th percentile of the last minute), 0 to 1 over 40 dB (R3). |
| Face frames | [vision.py](encore_server/vision.py) | Runs Face Landmarker in its model environment and returns a smile score for each analysed frame. |
| Rolling fusion | [fusion.py](encore_server/fusion.py) | Combines audio and face observations from the last five seconds for laughter detection. The [tab demo](../client/tab-demo/README.md) describes the thresholds and capture timing. |
| Audio-only detector | `detector.py` | OPEN when a reaction's score reaches its trigger (`detector-v2`: laughter 0.25, scream 0.4, gasp 0.3), UPDATE when it fires again at least 2 s after the last trigger, FINAL after 1.5 s below 60 % of the trigger. Turning the microphone off, the PC ending the session, or 2 s without audio closes open events. |
| Playback | `session.py` | Each event's capture times are mapped to content timecode through the PC's playback anchors (R3). Without an anchor at or before the event, playback stays `pending`; it is never guessed. If the content or seek epoch changed before the event ended, the end timecode is `null`. |

Every event carries `source.model`, `source.config` (including the window and thresholds for fused laughter) and the `aec` status of the audio it came from, so results from different models or settings can be told apart.

## Not done yet

- **Tuning on real audience audio.** The laughter level was chosen on FSD50K development clips and measured on separate FSD50K evaluation clips ([EM-11](../docs/em-11-evaluation.md)). Scream and gasp are still starting values. None has been checked on recorded laughter, screams or gasps in a living room with the content playing. This needs the team's test recordings, ideally with AEC on (EM-10).
- **Persistence and the session API** (EM-20, design §IV): live cursor and control state are process-local. Start/End HTTP and authentication remain open.
- **Distributed inference.** The server runs YAMNet off the event loop and starts a local subprocess when face inference is enabled.

## Test

```sh
.venv/bin/python -m pytest -q server/tests
```

- `test_detector.py`: windows, room baseline and the OPEN/UPDATE/FINAL detector.
- `test_server_session.py`: acknowledgements, audio admission, playback mapping, replay, and that the PC's receiver accepts everything the server sends.
- `test_server_end_to_end.py`: the real client (`Session`, `WavMicrophone`) against the real server on a local port.
- `test_yamnet_model.py`: the real YAMNet adapter. It is skipped unless TensorFlow Hub is installed, and it downloads the model the first time.
