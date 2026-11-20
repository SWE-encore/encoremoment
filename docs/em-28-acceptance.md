# R1–R11 acceptance record (EM-28)

This record shows what `main` does today against requirements R1–R11 (`documents/submission-b/sections/requirements.tex`) and the check cases in the specification (`specifications.tex`). It is the first pass. Before submission, rerun it on the release commit and replace the results.

- **Commit:** `a82b6b5` (main, "Merge pull request #21 from SWE-encore/feat/tab-demo-ui")
- **Date run:** 2026-11-20
- **Open PRs that change results:** #22 (R3 records), #23 (R11 reconnect), #24 (R11 graceful End). None is counted below.

## Two products, kept apart

| | PC app | Tab prototype |
|---|---|---|
| Code | `client/encore_client` and `server/encore_server` | `client/tab-demo` and the same server with face fusion |
| Sensors | PC microphone (or a WAV stand-in); camera control with a fake device in tests | A shared Chrome tab: its audio and frames stand in for the microphone and the camera |
| Clips | Sample clip index only (`samples/clip_index.json`) | OPFS segment ring and WebM clips in the browser |

A result for one product is never counted for the other. For example, R10 passes its automated cases in the tab prototype, while the PC app has no clip recording at all.

## Status words

- **Pass:** every check case that applies was run and matched the expected result.
- **Partial:** some check cases pass; the others are not built or not run.
- **Unverified:** the feature is built, but the evidence the check case asks for does not exist yet.
- **Not built:** no implementation on `main`.
- **N/A:** the requirement does not apply to this product.
- **Not checked:** it may exist, but this run did not test it.

## Summary

| R | Requirement | Jira (owner) | PC app | Tab prototype | Open work |
|---|---|---|---|---|---|
| R1 | Echo cancellation | EM-10 (Yujeong) | **Not built** | N/A (analyses the tab's own audio) | AEC in the microphone path |
| R2 | Reaction recognition | EM-11, EM-26 (unassigned) | **Unverified** | **Unverified** (one manual run) | Labeled evaluation; PANNs vs YAMNet in the spec |
| R3 | Event records | EM-12 (unassigned) | **Partial** | Not checked | Late-delivery case in PR #22 |
| R4 | Highlights | EM-13 (Soyeon) | **Partial** | Not built | Clip links, clip status, deletion state |
| R5 | Camera activation | EM-14 (Soyeon) | **Partial** (fake device) | Not checked (Chrome share prompt) | Real camera backend; device, disk and network inspection |
| R6 | Camera indication | EM-15 (Soyeon) | **Partial** (fake device) | Not checked (Chrome sharing bar) | Indicator vs a real pipeline |
| R7 | Review and deletion | EM-16 (Soyeon) | **Not built** | **Not built** | Delete action and pending/completed state |
| R8 | Microphone control | EM-17 (Soyeon) | **Pass** (automated) | Not checked | Manual check with a real microphone |
| R9 | Viewing interface | EM-18 (Soyeon) | **Partial** | Not checked | AEC status, clip progress, queue-full check |
| R10 | Reaction clips | EM-19 (Chul), EM-27 (Yujeong) | **Not built** | **Partial** (automated cases pass) | PC clip recording; disk-full and delay cases |
| R11 | Viewing sessions | EM-20 (unassigned) | **Partial** | **Partial** | PRs #23 and #24; restart during cleanup |

PC app on `main`: 1 pass, 6 partial, 1 unverified, 3 not built.

## Environment

| | Local (developer PC) | CI ("Client integration", run 44697363899) |
|---|---|---|
| OS | Ubuntu 24.04.3 LTS, kernel 7.0.0-43-generic | GitHub `ubuntu-24.04` image 20261111.370.1 |
| Python | 3.12.3, PySide6 6.8.3, websockets 15.0.1, numpy 2.5.5, soxr 1.1.0 | Same requirements files |
| GStreamer | 1.24.2 through PyGObject. CLI tools are not installed. `webrtcechoprobe` and `webrtcdsp` are missing; `splitmuxsink`, `vp8enc` and `opusenc` are present. | 1.24.2 with tools, plugins-base/good/bad and libav |
| Node | v22.23.3 | `actions/setup-node@v4` |
| Browser | Chromium 156 (embedded browser) | none |
| YAMNet | TensorFlow 2.18.1 and tensorflow_hub 0.16.1, in a separate environment built from `server/requirements-yamnet.txt` | not installed |

## Runs

| # | What | Command (from the repository root unless noted) | Result |
|---|---|---|---|
| 1 | Python suite | `.venv/bin/python -m pytest -q -rs` | **197 passed, 1 skipped** locally and in CI. The skip is `server/tests/test_yamnet_model.py`, because `tensorflow_hub` is not installed. |
| 2 | Tab prototype Node tests | `node --test client/tab-demo/tests/*.test.mjs` | CI: **8/8 pass**. Local: 7 pass, 1 skipped (the VP8/Opus decode test needs the GStreamer CLI). |
| 3 | Contract demo, headless | `cd client && ../.venv/bin/python -m encore_client --demo --headless` | Exit 0. `evt-0004` gasp OPEN → FINAL rev 3. Duplicates ignored: `duplicate_control`, `duplicate_cursor`, `duplicate_revision`, `stale_revision` (1 each). Rejected: `wrong_session` 1. Received 9, sent 4. |
| 4 | PC → server, loudness stand-in | Server: `PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --port 8799`. PC: `cd client && ../.venv/bin/python -m encore_client --headless --no-player --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5` | Exit 0. `evt-0001` laughter OPEN → UPDATE → FINAL. 18 chunks sent, 0 dropped, nothing ignored or rejected. Server: confidence 1.00, intensity 0.93, "session ended" on End. |
| 5 | PC → server, YAMNet | Same as run 4 with `--model yamnet`, from `main`'s code | Exit 0. 18 chunks, **no event**: YAMNet does not call a 440 Hz tone a reaction. No positive YAMNet case on the PC path, because the repository has no labeled reaction recordings. |
| 6 | Browser recording | Serve `client/tab-demo` locally and open `/tests/playback.html`, then **Run recording test** | **PASS**: 1 file, 470,925 bytes, VP8 + Opus, OPEN → UPDATE → FINAL. With **Resize video during recording**: **PASS**, 2 files, 468,872 bytes. |
| 7 | Tab prototype, manual (user) | `client/tab-demo/README.md` steps, a YouTube tab, the team's YAMNet + face server | Build `70b1738`, which is older than `main`. Result: FINAL `evt-0001` scream, audio confidence 0.543, first trigger latency 573 ms. A recording error appeared; #16 fixed it, but the run has not been repeated on `main`. |

`tone.wav` is 2 s of silence, 3 s of a 440 Hz tone at half scale, then 4 s of silence (mono, 16 kHz, 16-bit). The loudness stand-in calls any loud sound laughter and marks its events `source.model: loudness-standin`, so run 4 checks the pipeline, not recognition.

**Local setup note:** run 1 first failed 8 microphone tests with `No module named 'numpy'`. `a7e16a1` added `numpy` and `soxr` to `client/requirements.txt`, and the local `.venv` had not been updated. After `.venv/bin/python -m pip install -r requirements-dev.txt`, the local result matched CI. Reinstall the requirements after pulling.

## Per requirement

### R1 Echo cancellation: PC app Not built

- **Expected:** compare AEC on and off on soundtrack-only, audience-only and overlapping recordings, including seeks and output-device changes. Residual playback is suppressed, and laughter, scream and gasp stay recognizable.
- **Actual:** the player inserts `webrtcechoprobe` when the plugin is installed (`client/encore_client/player/pipeline.py`). On this PC it is not installed, and the player shows "Echo reference: unavailable". No microphone path uses the reference. Both microphone sources send every chunk with `aec: "off"` (`sensing/wav_mic.py`, `sensing/live_mic.py`).
- **Tab prototype:** N/A. It analyses the shared tab's own audio, so no playback is mixed into a microphone.
- **Owner:** EM-10 (Yujeong). R12 (EM-21) depends on it.

### R2 Reaction recognition: Unverified

- **Expected:** labeled laughter, scream, gasp, speech, soundtrack-only and mixed recordings. Camera-off operation, missing or occluded faces, one strong and several weak responses. Per-class errors and measured detection delay.
- **Actual (PC app):** YAMNet classes are combined into laughter, scream and gasp (`server/encore_server/models.py`). The detector has 11 unit tests. Runs 4 and 5 show the path works end to end, and that a tone is not a reaction for YAMNet. There is no labeled set, so there are no per-class errors and no delay measurement.
- **Actual (tab prototype):** audio and smile fusion has 4 rolling-fusion and 8 paired-demo tests. One manual run produced a scream event with a 573 ms first-trigger latency (run 7). That is one sample, not an evaluation.
- **Spec mismatch:** the specification names PANNs at 32 kHz. The code runs YAMNet on 16 kHz windows of 0.975 s every 0.48 s. Change one or the other before submission.
- **Owner:** EM-11 (recognition) and EM-26 (5 s laughter score and signal delay), both unassigned.

### R3 Event records: PC app Partial

- **Expected:** replaying the same input or result twice gives one event. Timecodes stay right before and after pause, seek, rate change and title change, including delayed network delivery.
- **Actual:** these pass:
  - `test_same_result_twice_leaves_one_event`
  - run 3's duplicate handling
  - `test_anchors.py` (7 tests: epochs on seek and new content, pause, rate)
  - `test_events_map_to_content_timecode_through_anchors`, `test_a_seek_during_the_event_leaves_the_end_unmapped` and `test_paused_anchor_pins_the_timecode`

  Intensity is loudness above the room's quiet level (`audio.py`), an observable quantity. Delayed delivery of anchors is not covered on `main`. PR #22 adds it.
- **Observation:** in run 4 the event starts at 1.4 s, but the tone starts at 2.0 s. The recorded start is the first analysis window that contains the sound, so it can be up to one window early. The specification already calls framewise predictions approximate.
- **Owner:** EM-12. PR #22 is waiting for review.

### R4 Highlights: PC app Partial

- **Expected:** mixed intensities, equal scores, shared clips, camera-off events, failed exports and pending deletions. Check the ordering and every displayed link.
- **Actual:** the "Tonight's Highlights" tab lists FINAL events by intensity, highest first. Ties go by trigger time, then event ID (`ui/workspace.py`). Every row says "no exported clip linked". Clip review works only for the sample clip index (`test_full_entrypoint_player_session_and_clip_review`). Not built: clip links from real events, clip status, deletion state.
- **Tab prototype:** saved clips are listed and playable, but nothing is ranked.
- **Owner:** EM-13 (Soyeon). It needs PC clips from R10 first.

### R5 Camera activation: PC app Partial

- **Expected:** inspect device access, disk writes and network packets before enable, after deny and after disable. Race disable against queued upload and clip completion.
- **Actual:** 14 tests in `test_camera_control.py` pass with a fake device. They cover:
  - off by default, nothing opened;
  - enable needs an active session;
  - denial keeps the camera off;
  - "on" only after the first real frame;
  - stop closes the gate first;
  - a late frame after stop is ignored;
  - session end stops the camera.

  The PC app has no real camera backend yet, so the device, disk and network inspection cannot be done.
- **Tab prototype:** capture starts only from Chrome's share prompt. Not checked against this requirement.
- **Owner:** EM-14 (Soyeon).

### R6 Camera indication: PC app Partial

- **Expected:** switch views during capture, stop while an export is pending, unplug the webcam. The indicator matches the actual pipeline state and media writes.
- **Actual:** `test_camera_panel_shows_actual_state_and_persistent_stop` and `test_unplug_during_capture_shows_error_and_turns_camera_off` pass with the fake device. There is no real pipeline or export to compare against.
- **Owner:** EM-15 (Soyeon).

### R7 Review and deletion: Not built

- **Expected:** delete during playback or export, repeat a deletion, disconnect during cleanup, restart the app. No stale response restores the media or its play link.
- **Actual:** the clip model has a `deletion_pending` status ("next sprint" in `clips/model.py`), but neither product has a delete action. Tab prototype clips stay in OPFS until the browser's site data is cleared.
- **Owner:** EM-16 (Soyeon).

### R8 Microphone control: PC app Pass (automated)

- **Expected:** disable during an open event and during a queued inference. No new accepted audio event, microphone read or audio upload at the client after the control takes effect.
- **Actual:** these pass:
  - `test_start_opens_the_device_and_off_closes_it_before_telling_the_server`
  - `test_off_stops_the_upload`
  - `test_old_generation_events_are_refused_after_off_and_after_on_again`
  - `test_server_closes_the_open_event_ignores_old_audio_and_starts_fresh`
  - `test_server_turning_off_while_a_face_inference_is_pending_adds_nothing`
  - `test_audio_flows_only_while_the_microphone_is_on`
  - `test_microphone_off_closes_events_and_old_audio_is_ignored` (server)

  These use WAV and fake Qt devices. No manual run with a real microphone has been recorded.
- **Owner:** EM-17 (Soyeon).

### R9 Viewing interface: PC app Partial

- **Expected:** out-of-order and duplicated updates, an interrupted socket, a full analysis queue. Correct event count, visible connection gaps, responsive Stop controls.
- **Actual:** the session panel shows the live timeline (time, reaction, phase, revision, intensity, confidence) and the connection state. "Disconnected: reason. Results below may be out of date." greys out the table. These pass: `test_workspace_receives_deduplicates_and_shows_actual_disconnect`, `test_dropped_connection_is_reported`, `test_silent_server_counts_as_dropped`, and the contract tests for order and duplicates. Not built: AEC status, clip progress, a full-queue check.
- **Owner:** EM-18 (Soyeon).

### R10 Reaction clips: PC app Not built, tab prototype Partial

- **Expected:**
  - trigger near camera activation, buffer rollover and session end;
  - network delay, overlapping events, format changes, disk exhaustion;
  - decodable, synchronized output; actual coverage; missing parts labeled;
  - one trigger gives 25 s; each re-fire adds 5 s; no clip over 60 s.
- **Tab prototype:** these pass:
  - the five clip-plan tests (15 s before and 10 s after, +5 s per re-fire, 60 s cap then a linked next clip, activation and interior gaps recorded, OPEN pins pre-context, a duplicate FINAL or stop cannot resurrect a job);
  - VP8 and Opus decoding across track renumbering and resolution changes (CI);
  - the browser recording test with and without a mid-recording resize (run 6).

  Not run: disk exhaustion, injected network delay on a real capture. A clip-stitching problem is open with Chul and was not reproduced here.
- **PC app:** no camera buffering or clip export. Only the sample clip index (EM-2) is reviewable.
- **Owner:** EM-19 (Chul) for clip rules and recording; EM-27 (Yujeong) for PC events with audience-camera clips.

### R11 Viewing sessions: Partial

- **Expected:** repeat Start/End; reconnect after a committed but unacknowledged result; end while offline; restart during cleanup. One event per stable ID, and no unsaved media after cleanup.
- **Actual (PC app):** these pass:
  - Start/End and `test_end_is_not_a_disconnect`;
  - interruptions are reported (`test_dropped_connection_is_reported`, `test_unreachable_server_is_reported`);
  - the server keeps a dropped session and replays after a cursor (`test_results_replay_from_a_cursor`, `test_a_reconnecting_pc_is_sent_the_results_after_its_cursor`).

  The PC does not reconnect on `main`: PR #23 adds it, with 7 tests. End does not wait for the server to close open reactions: PR #24 adds that, with 8 tests. The PC app has no temporary media to clear yet. Check this again when R10 lands in the PC app.
- **Actual (tab prototype):** stopping the share deletes unsaved segments (`ClipBuffer.stop()` in `clips/buffer.js`; README step 5). Reconnect and restart during cleanup were not checked.
- **Owner:** EM-20. PRs #23 and #24 are waiting for review.

## Not in R1–R11 but needed for the run: deployment

There is **no Docker Compose file** in the repository. The specification (R13) makes Compose the initial AWS runtime, with separate API and worker services, Redis and PostgreSQL. Today the server is one Python process (`python -m encore_server`) that keeps sessions in memory. `deploy/models/` only installs YAMNet and Face Landmarker for command-line inference on the team's server. The related issues:

- EM-3: minimal client–server integration environment (Chul)
- EM-22: Kubernetes deployability (Chul)
- EM-29: PostgreSQL (RDS) for confirmed events, in progress

Until one of them lands, every run above uses a server started by hand.

## Open items by owner

| Owner | Issue | Item | Blocks |
|---|---|---|---|
| Yujeong | EM-10 | AEC in the microphone path | R1, R12, R2 mixed-audio cases |
| Yujeong | EM-27 | PC events with audience-camera clips | R10 (PC), R4 links |
| Soyeon | EM-13 | Clip links and status in highlights | R4 |
| Soyeon | EM-14, EM-15 | Real camera backend and indicator | R5, R6 |
| Soyeon | EM-16 | Delete action and pending/completed state | R7 |
| Soyeon | EM-18 | AEC status, clip progress | R9 |
| Chul | EM-19 | PC clip recording; clip-stitching problem | R10 |
| Chul | EM-3 | Compose (or another repeatable server start) | rerun of this record |
| Pavel | EM-12, EM-20 | Merge PRs #22, #23, #24 after review | R3, R11 |
| Unassigned | EM-11, EM-26 | Labeled recordings and the evaluation report | R2 |
| Team | — | PANNs vs YAMNet: update the spec or the model | R2 |

## Before and after Nov 30 (proposal)

The development deadline is Dec 3. This split is a proposal for the team to confirm, not an agreed plan.

- **By Nov 30:**
  - merge #22–#24;
  - AEC in the microphone path (EM-10);
  - PC clip recording and links (EM-19, EM-27, EM-13);
  - a real camera backend (EM-14, EM-15);
  - deletion (EM-16).

  These change what the acceptance run can test, so they have to land first.
- **Dec 1–2 slack:**
  - rerun this record on the release commit;
  - the R2 evaluation report, once labeled recordings exist;
  - a second manual tab-prototype run on `main`;
  - fixes the rerun turns up.

  The Compose start (EM-3) belongs here if it has not landed by then.

## How to rerun

```sh
# Python environment (reinstall after every pull)
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q -rs
node --test client/tab-demo/tests/*.test.mjs

# Contract demo
cd client && ../.venv/bin/python -m encore_client --demo --headless; cd ..

# PC to a separate server (loudness stand-in; use --model yamnet with TensorFlow installed)
PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --port 8799 &
cd client && ../.venv/bin/python -m encore_client --headless --no-player \
  --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5; cd ..

# Browser recording test
python3 -m http.server 8002 --bind 127.0.0.1 --directory client/tab-demo
# then open http://localhost:8002/tests/playback.html and press "Run recording test"
```
