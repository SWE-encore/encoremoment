# R1–R11 acceptance record (EM-28)

This record shows what `main` does today against requirements R1–R11 (`documents/submission-b/sections/requirements.tex`) and the check cases in the specification (`specifications.tex`). Before submission, rerun it on the release commit and replace the results.

- **Commit:** `0c5ce60` (main, "Merge pull request #37 from SWE-encore/reviewed/em-27-desktop")
- **Date run:** 2026-11-23
- **Previous run:** 2026-11-20 on `a82b6b5`. Since then, PRs #22–#37 have been merged: records, reconnect and End, highlights, deletion, AEC, PC clip recording, delivery, history, Compose and Kubernetes.

## Two products, kept apart

| | PC app | Tab prototype |
|---|---|---|
| Code | `client/encore_client` and `server/encore_server` | `client/tab-demo` and the same server with face fusion |
| Sensors | PC microphone (or a WAV stand-in) with the player's echo reference; GStreamer camera (`v4l2src`) behind explicit consent | A shared Chrome tab: its audio and frames stand in for the microphone and the camera |
| Clips | Pinned rolling disk buffer and WebM export (`clips/recorder.py`) | OPFS segment ring and WebM clips in the browser |

A result for one product is never counted for the other. `client/tab-demo` has not changed since `a82b6b5`, so its results carry over from the previous run.

## Status words

- **Pass:** every check case that applies was run and matched the expected result. "(automated)" means automated tests only, with no recorded manual run on real devices.
- **Partial:** some check cases pass; the others are not built or not run.
- **Unverified:** the feature is built, but the evidence the check case asks for does not exist yet.
- **Not built:** no implementation on `main`.
- **N/A:** the requirement does not apply to this product.
- **Not checked:** it may exist, but this run did not test it.

## Summary

| R | Requirement | Jira (owner) | PC app | Tab prototype | Open work |
|---|---|---|---|---|---|
| R1 | Echo cancellation | EM-10 (Yujeong) | **Unverified** | N/A (analyses the tab's own audio) | Run the real cancellation test: `webrtcdsp` is missing on Ubuntu 24.04 |
| R2 | Reaction recognition | EM-11, EM-26 (unassigned) | **Unverified** | **Unverified** (one manual run) | Labeled evaluation; PANNs vs YAMNet in the spec |
| R3 | Event records | EM-12 (unassigned) | **Pass** (automated) | Not checked | — |
| R4 | Highlights | EM-13 (Soyeon) | **Pass** (automated) | Not built | Manual UI check |
| R5 | Camera activation | EM-14 (Soyeon) | **Partial** | Not checked (Chrome share prompt) | Device, disk and network inspection with a real webcam |
| R6 | Camera indication | EM-15 (Soyeon) | **Partial** | Not checked (Chrome sharing bar) | Indicator vs a real webcam pipeline |
| R7 | Review and deletion | EM-16 (Soyeon) | **Pass** (automated) | **Not built** | Tab prototype deletion, if it stays in the demo |
| R8 | Microphone control | EM-17 (Soyeon) | **Pass** (automated) | Not checked | Manual check with a real microphone |
| R9 | Viewing interface | EM-18 (Soyeon) | **Pass** (automated) | Not checked | Manual UI check |
| R10 | Reaction clips | EM-19 (Chul), EM-27 (Yujeong) | **Partial** | **Partial** | Real webcam run; injected signal delay |
| R11 | Viewing sessions | EM-20 (unassigned) | **Partial** | **Partial** | A second session needs an app restart |

PC app on `main`: 5 pass, 4 partial, 2 unverified, 0 not built (on 2026-11-20: 1 pass, 6 partial, 1 unverified, 3 not built).

## Environment

| | Local (developer PC) | CI (run 45001411950 "Client integration", run 45001411913 "Server deployment smoke") |
|---|---|---|
| OS | Ubuntu 24.04.3 LTS, kernel 7.0.0-43-generic | GitHub `ubuntu-24.04` image |
| Python | 3.12.3, PySide6 6.8.3, websockets 15.0.1, numpy 2.5.5, soxr 1.1.0 | Same requirements files |
| GStreamer | 1.24.2 through PyGObject. CLI tools are not installed. `webrtcechoprobe` and `webrtcdsp` are missing. | 1.24.2 with tools, plugins-base/good/bad and libav. `webrtcdsp` is missing there too. |
| Node | v22.23.3 | `actions/setup-node@v4` |
| Browser | Chromium 156 (embedded browser), previous run | none |
| Containers | Docker with Compose v5.1.1; `postgres:16` | `postgres:16` service; Compose and `kubectl kustomize` |
| YAMNet | TensorFlow 2.18.1 and tensorflow_hub 0.16.1, in a separate environment built from `server/requirements-yamnet.txt` | not installed |

## Runs

| # | What | Command (from the repository root unless noted) | Result |
|---|---|---|---|
| 1 | Python suite | `.venv/bin/python -m pytest -q -rs` | **311 passed, 3 skipped**, locally and in CI. Skips: `test_yamnet_model.py` (no tensorflow_hub); `test_echo_capture.py::test_synthetic_echo_mixture_and_seek` (no `webrtcdsp`); the PostgreSQL integration test (needs a database, see run 2). |
| 2 | PostgreSQL history | `ENCORE_TEST_DATABASE_URL=… python -m pytest -q server/tests/test_event_history.py` | **13 passed** locally against `postgres:16`, and in CI. |
| 3 | Tab prototype Node tests | `node --test client/tab-demo/tests/*.test.mjs` | CI: **8/8 pass**. Local: 7 pass, 1 skipped (the VP8/Opus decode test needs the GStreamer CLI). |
| 4 | Contract demo, headless | `cd client && ../.venv/bin/python -m encore_client --demo --headless` | Exit 0. `evt-0004` gasp OPEN → FINAL rev 3. Four kinds of duplicates ignored and one `wrong_session` rejected, as before. |
| 5 | PC → server, loudness stand-in | Server: `PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --port 8799`. PC: `cd client && ../.venv/bin/python -m encore_client --headless --no-player --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5` | Exit 0. `evt-0001` laughter OPEN → UPDATE → FINAL. 18 chunks sent, 0 dropped. End now turns the microphone off first: the server applied generation 2, then "session ended". |
| 6 | PC → server, YAMNet | Same as run 5 with `--model yamnet` | Exit 0. 18 chunks, **no event** for a 440 Hz tone. There is still no positive YAMNet case on the PC path. |
| 7 | Compose fixture | `docker compose -f deploy/compose.yaml up --build --wait`, then `docker compose … exec -T server python deploy/smoke.py` and `python3 deploy/smoke.py --pc --server ws://127.0.0.1:<port>` | Local: healthy. The smoke gives one FINAL with last cursor 2. The PC smoke gives one FINAL mapped from 1 ms to 1936 ms. CI also renders both Kubernetes configurations, checks their probes, and checks that history survives a database stop and a server recreation. |
| 8 | History during outages | Scripted against `postgres:16` (three cases from the #15 review) | Database down at End: the FINAL is archived 1.6 s after the database returns. Down during a live session: the microphone-off ack and the FINAL still arrive, and the FINAL is archived after recovery. Server stopped mid-reaction: the reaction is closed and archived. |
| 9 | Browser recording | `/tests/playback.html` | Carried over (tab prototype unchanged): **PASS**, 1 file, 470,925 bytes, VP8 + Opus. With resize: **PASS**, 2 files. |
| 10 | Tab prototype, manual (user) | `client/tab-demo/README.md` steps | Carried over: build `70b1738`. FINAL scream, audio confidence 0.543, first trigger latency 573 ms. Not repeated on `main`. |

`tone.wav` is 2 s of silence, 3 s of a 440 Hz tone at half scale, then 4 s of silence (mono, 16 kHz, 16-bit). The loudness stand-in calls any loud sound laughter, so run 5 checks the pipeline, not recognition.

## Per requirement

### R1 Echo cancellation: PC app Unverified

- **Expected:** compare AEC on and off on soundtrack-only, audience-only and overlapping recordings, including seeks and output-device changes. Residual playback is suppressed, and laughter, scream and gasp stay recognizable.
- **Built (#33):** the live microphone is cancelled against the player's reference (`player/echo.py`, `sensing/live_mic.py`). Each chunk carries its effective AEC state (`active`, `warming_up`, `reference_unavailable`, `off`). The server marks a window by all the samples it overlaps (`test_aec_windows.py`). The workspace shows "Echo cancellation: <state>".
- **Passing tests:** the plumbing tests in `test_echo_capture.py`: capture time and AEC state stay with the PCM through jitter and resampling; a mixed chunk is never reported active; the device clock survives past 2 GiB.
- **Not run anywhere:** `test_synthetic_echo_mixture_and_seek`. It is the only test that measures cancellation (ERLE above 3 dB on a synthetic mixture, then reference reset on seek). It needs `webrtcdsp` and `webrtcechoprobe`, which Ubuntu 24.04 does not ship (`client/README.md`, "Known limits"). It is skipped locally and in CI.
- **Missing:** no recording comparison exists.
- **Owner:** EM-10 (Yujeong). It needs a decision on how to get the plugin (build from source, another distribution, or PipeWire echo-cancel). R12 (EM-21) depends on it.

### R2 Reaction recognition: Unverified

- **Expected:** labeled laughter, scream, gasp, speech, soundtrack-only and mixed recordings. Camera-off operation, missing or occluded faces, one strong and several weak responses. Per-class errors and measured detection delay.
- **Actual:** unchanged since the previous run. YAMNet classes are combined into the three reactions; the detector and fusion tests pass. A tone gives no YAMNet event (run 6). There is no labeled set, so there are no per-class errors and no delay measurement. One manual tab-prototype scream event had a 573 ms first-trigger latency.
- **Spec mismatch:** the specification still names PANNs at 32 kHz; the code runs YAMNet on 16 kHz windows.
- **Owner:** EM-11 and EM-26, both unassigned.

### R3 Event records: PC app Pass (automated)

- **Expected:** replaying the same input or result twice gives one event. Timecodes stay right before and after pause, seek, rate change and title change, including delayed network delivery.
- **Actual:**
  - duplicate handling: `test_same_result_twice_leaves_one_event` and run 4;
  - pause, seek, rate and title: `test_anchors.py` and the server's anchor tests;
  - delayed delivery: `test_playback_timecode.py` (#22), including a late anchor read after the server applied it.

  With `--record-history`, FINAL records also survive a restart (run 2), which R3 does not require.
- **Observation:** a recorded start can be up to one analysis window early. In run 5 the event starts at 1.4 s, but the tone starts at 2.0 s.

### R4 Highlights: PC app Pass (automated)

- **Expected:** mixed intensities, equal scores, shared clips, camera-off events, failed exports and pending deletions. Check the ordering and every displayed link.
- **Actual (#26):** `test_highlights.py` (17 tests) covers:
  - ordering by intensity, ties by trigger time then event ID, missing intensity last, and the same order whatever the arrival order;
  - only FINAL events listed;
  - events without footage listed without a link, with the reason from the clip plan;
  - one link only for a ready or partial clip whose file exists and is not empty;
  - no link while pending, failed or being deleted;
  - a shared clip linked from both events;
  - parts of a capped clip named in order;
  - the panel rereads the index when a pending clip finishes.
- **Not done:** a manual run of the window.

### R5 Camera activation: PC app Partial

- **Expected:** inspect device access, disk writes and network packets before enable, after deny and after disable. Race disable against queued upload and clip completion.
- **Actual:** the PC app now has a real camera path (`clips/capture.py`, GStreamer `v4l2src`) behind `CameraControl`. These pass with fake devices and `videotestsrc`:
  - the 14 camera-control tests;
  - `test_privacy_cancel_and_camera_off_metadata`;
  - `test_offline_camera_audio_stays_local_and_cannot_leak_into_reconnect`;
  - `test_camera_image_belongs_to_one_audio_interval_and_one_connection`.

  No run with a real webcam inspected device access, disk writes and network packets.
- **Owner:** EM-14 (Soyeon). EM-27 (Yujeong) covers the desktop camera wiring.

### R6 Camera indication: PC app Partial

- **Expected:** switch views during capture, stop while an export is pending, unplug the webcam. The indicator matches the actual pipeline state and media writes.
- **Actual:** the indicator and Stop control pass with the fake device (`test_camera_panel_shows_actual_state_and_persistent_stop`, `test_unplug_during_capture_shows_error_and_turns_camera_off`). They have not been compared against a real webcam pipeline.
- **Owner:** EM-15 (Soyeon).

### R7 Review and deletion: PC app Pass (automated), tab prototype Not built

- **Expected:** delete during playback or export, repeat a deletion, disconnect during cleanup, restart the app. No stale response restores the media or its play link.
- **Actual (#27, #34):** `test_clip_deletion.py` (23 tests) covers:
  - the journal is written before any file is deleted;
  - deleting during playback stops and releases the clip; deleting during export cancels it and blocks its commit;
  - a repeated deletion is one operation;
  - pending until the server acknowledges, retried while offline;
  - a late result cannot restore media or a link;
  - a restart resumes an incomplete deletion;
  - files shared with a retained clip, or outside the clip folder, are kept;
  - a damaged or lost journal waits for repair without losing tombstones.

  The panel shows pending, then deleted.
- **Tab prototype:** still no delete action.
- **Owner:** EM-16 (Soyeon).

### R8 Microphone control: PC app Pass (automated)

- **Expected:** disable during an open event and during a queued inference. No new accepted audio event, microphone read or audio upload at the client after the control takes effect.
- **Actual:** the microphone-switch, audio-upload and server-session tests from the previous run still pass. `test_mic_off_splits_capture_and_prevents_more_audio` adds the recorder's side. No manual run with a real microphone is recorded.
- **Owner:** EM-17 (Soyeon).

### R9 Viewing interface: PC app Pass (automated)

- **Expected:** out-of-order and duplicated updates, an interrupted socket, a full analysis queue. Correct event count, visible connection gaps, responsive Stop controls.
- **Actual:**
  - order and duplicates: contract tests and run 4;
  - interrupted socket: reconnect tests (#23) and `test_missing_cursor_reconnects_and_superseded_events_are_applied_once`;
  - full queue: `test_bounded_incoming_messages_and_stop_while_sender_is_blocked` and `test_partial_drain_marks_unapplied_results_stale_before_queue_saturation` (#36).

  The window shows the timeline, the highlight list with clip status, connection state, microphone and camera state, and the echo-cancellation state.
- **Not done:** a manual run of the window.
- **Owner:** EM-18 (Soyeon).

### R10 Reaction clips: PC app Partial, tab prototype Partial

- **Expected:**
  - trigger near camera activation, buffer rollover and session end;
  - network delay, overlapping events, format changes, disk exhaustion;
  - decodable, synchronized output; actual coverage; missing parts labeled;
  - one trigger gives 25 s; each re-fire adds 5 s; no clip over 60 s.
- **PC app (#35, #37):** these pass with `videotestsrc`, raw and MJPEG at 5 fps (`test_desktop_recorder.py`, `test_desktop_media.py`):
  - re-fires extend the clip, the cap starts a linked part, and duplicate revisions are ignored;
  - footage from before camera activation is labeled as a gap;
  - pinned segments survive eviction;
  - going over the disk budget cancels safely;
  - after a crash, recovery removes the ring buffer and incomplete files;
  - microphone off splits the capture;
  - the exported WebM decodes with matching audio and video timestamps.

  Not run: a real webcam, and an injected signal delay longer than the buffer retention (35 s).
- **Tab prototype:** unchanged. The automated clip-plan, decode and browser recording cases pass; disk exhaustion and injected delay have not been run.
- **Owner:** EM-19 (Chul); EM-27 (Yujeong).

### R11 Viewing sessions: PC app Partial, tab prototype Partial

- **Expected:** repeat Start/End; reconnect after a committed but unacknowledged result; end while offline; restart during cleanup. One event per stable ID, and no unsaved media after cleanup.
- **Actual (PC app):** these pass:
  - reconnect after the cursor, replay of results lost in flight, controls resynced first, a lost server session not shown as recovered (`test_reconnect.py`, #23);
  - End waits for open reactions, gives up after the grace period, and ends at once while disconnected (`test_session_end.py`, #24);
  - a headless run fails when the server does not confirm the end (#29), and End waits for buffered results (#36);
  - after a crash, recovery removes unsaved media (R10).
- **Gap:** the app runs one session per launch. After End, **Start session** stays disabled (`session/panel.py`: Start is enabled only in `IDLE`). A second Start/End needs a restart, so "repeat Start/End" fails in the app.
- **Tab prototype:** stopping the share deletes unsaved segments. Reconnect has not been checked.
- **Owner:** EM-20 (unassigned).

## Deployment (R13, needed for the rerun)

- **Compose (EM-3, #31):** `deploy/compose.yaml` runs the server image with readiness probes; `deploy/compose.history.yaml` adds PostgreSQL history with a persistent outbox. The fixture works locally (run 7). CI's "Server deployment smoke" also:
  - builds it and runs the smoke and PC smoke;
  - stops the history database and recreates the server;
  - checks that the FINAL is read back.
- **Kubernetes (EM-22, #32):** `deploy/k8s` and `deploy/k8s-history` render in CI, and their startup, liveness and readiness probes are checked. No GPU cluster run has been recorded.
- **History (EM-29, #15, #28, #30):** FINALs are committed to a local SQLite outbox before delivery and drained to PostgreSQL with retries. The three outage problems from the #15 review are fixed (run 8).
- **Not run:** the real-model image (`ENCORE_BUILD_TARGET=inference` with the YAMNet and Face Landmarker bundle) and the EC2–RDS connection.

## Open items by owner

| Owner | Issue | Item | Blocks |
|---|---|---|---|
| Yujeong | EM-10 | Get `webrtcdsp` (or another canceller) and run the cancellation test and a recording comparison | R1, R12, R2 mixed-audio cases |
| Soyeon, Yujeong | EM-14, EM-15, EM-27 | Real webcam run: device, disk and network inspection, indicator vs pipeline | R5, R6 |
| Chul, Yujeong | EM-19, EM-27 | Real webcam clip run; signal delay beyond retention | R10 |
| Soyeon | EM-13, EM-18 | Manual run of the highlight and session windows | R4, R9 evidence |
| Unassigned | EM-20 | Allow a new session after End without restarting the app | R11 |
| Unassigned | EM-11, EM-26 | Labeled recordings and the evaluation report | R2 |
| Team | — | PANNs vs YAMNet: update the spec or the model | R2 |
| Chul | EM-22, EM-29 | GPU Kubernetes smoke; real-model image; EC2–RDS check | R13 |

## Before and after Nov 30 (proposal)

The development deadline is Dec 3. This split is a proposal for the team to confirm.

- **By Nov 30:**
  - the `webrtcdsp` decision and a cancellation run (EM-10);
  - a new session after End (EM-20);
  - one recorded manual run with a real webcam and microphone covering R4–R10.
- **Dec 1–2 slack:**
  - rerun this record on the release commit;
  - the R2 evaluation, once labeled recordings exist;
  - a manual tab-prototype run on `main`;
  - fixes the rerun turns up.

## How to rerun

```sh
# Python environment (reinstall after every pull)
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q -rs
node --test client/tab-demo/tests/*.test.mjs

# PostgreSQL history, against a dedicated test database
.venv/bin/python -m pip install -r server/requirements-history.txt
ENCORE_TEST_DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:5432/encore_test \
  .venv/bin/python -m pytest -q server/tests/test_event_history.py

# Contract demo
cd client && ../.venv/bin/python -m encore_client --demo --headless; cd ..

# PC to a separate server (loudness stand-in; use --model yamnet with TensorFlow installed)
PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --port 8799 &
cd client && ../.venv/bin/python -m encore_client --headless --no-player \
  --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5; cd ..

# Compose fixture
docker compose -f deploy/compose.yaml up --build --wait
docker compose -f deploy/compose.yaml exec -T server python deploy/smoke.py
python3 deploy/smoke.py --pc --server ws://127.0.0.1:8876
docker compose -f deploy/compose.yaml down

# Browser recording test
python3 -m http.server 8002 --bind 127.0.0.1 --directory client/tab-demo
# then open http://localhost:8002/tests/playback.html and press "Run recording test"
```
