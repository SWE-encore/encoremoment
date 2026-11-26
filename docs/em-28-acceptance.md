# R1–R11 acceptance record (EM-28)

This record shows what `main` does today against requirements R1–R11 (`documents/submission-b/sections/requirements.tex`) and the check cases in the specification (`specifications.tex`). Before submission, rerun it on the release commit and replace the results.

- **Commit:** `f694299` (main, "Merge pull request #58 from SWE-encore/feat/em-11-scream-v3")
- **Date run:** 2026-11-27
- **Previous run:** 2026-11-23 on `0c5ce60`. Since then, PRs #38–#58 have been merged:
  - a new session after End and a server lease for dropped sessions (EM-20);
  - the linear echo canceller and its use in the player (EM-10), and the echo-cancellation switch (EM-21);
  - one wording for clips without footage (EM-27);
  - audio chunks sent as soon as they end, each with a camera frame, and a warning for a silent microphone (EM-26);
  - the reaction evaluation, versioned model names and `detector-v3` (EM-11).

## Two products, kept apart

| | PC app | Tab prototype |
|---|---|---|
| Code | `client/encore_client` and `server/encore_server` | `client/tab-demo` and the same server |
| Sensors | PC microphone (or a WAV stand-in) with the player's echo reference; GStreamer camera (`v4l2src`) behind explicit consent. Since #46, each audio chunk carries a camera frame from its own span, so the server's face fusion also runs for the PC app. | A shared Chrome tab: its audio and frames stand in for the microphone and the camera |
| Clips | Pinned rolling disk buffer and WebM export (`clips/recorder.py`) | OPFS segment ring and WebM clips in the browser |
| Role | The product | Shown as a labelled prototype (team decision, 2026-11-26) |

A result for one product is never counted for the other. `client/tab-demo` has not changed since `a82b6b5`, so its results carry over from the 2026-11-20 run.

## Team decisions used here (2026-11-26)

- **R2 target:** the level the demo meets today: laughter found ≥ 30%, scream ≥ 45%, gasp ≥ 50%; ≤ 1 false event per minute of speech or music; median delay ≤ 1.5 s. The evidence is EM-11's evaluation and EM-26's live runs (`docs/em-11-evaluation.md`, section 8).
- **Model:** YAMNet stays. The final report states the change from the specification's PANNs.
- **Tab prototype:** shown as a labelled prototype. The PC app is the product, so R7 does not apply to the tab prototype.
- **EM-19 cutoff:** Nov 30. If the false-gap bug is not fixed by then, someone applies the fix from the 11-23 review, with Chul's OK.
- **Accepted limits:**
  - R1: no `webrtcdsp`; the player uses the linear canceller instead;
  - R2: YAMNet scores heavy breathing like a gasp;
  - R2: no recordings of a real audience;
  - R13: no GPU Kubernetes run and no EC2–RDS check.

## Status words

- **Pass:** every check case that applies was run and matched the expected result. "(automated)" means automated tests only, with no recorded manual run on real devices. "(team target)" means it meets the target the team set, with the accepted limits named in its section.
- **Partial:** some check cases pass; the others are not built or not run.
- **Fail:** a check case was run and did not match the expected result.
- **Unverified:** the feature is built, but the evidence the check case asks for does not exist yet.
- **Not built:** no implementation on `main`.
- **N/A:** the requirement does not apply to this product.
- **Not checked:** it may exist, but this run did not test it.

## Summary

| R | Requirement | Jira (owner, status) | PC app | Tab prototype | Open work |
|---|---|---|---|---|---|
| R1 | Echo cancellation | EM-10 (Yujeong, in progress) | **Partial** | N/A (analyses the tab's own audio) | Real room recording with speakers |
| R2 | Reaction recognition | EM-11, EM-26 (done) | **Pass** (team target) | **Unverified** (one manual run) | — (accepted limits) |
| R3 | Event records | EM-12 (Pavel, done) | **Pass** (automated) | Not checked | — |
| R4 | Highlights | EM-13 (Soyeon, done) | **Pass** (automated) | Not built | Ordering checked by hand |
| R5 | Camera activation | EM-14 (Soyeon, done) | **Partial** | Not checked (Chrome share prompt) | Device and network inspection with a real webcam |
| R6 | Camera indication | EM-15 (Soyeon, done) | **Partial** | Not checked (Chrome sharing bar) | Indicator vs pipeline and media writes; unplug a real webcam |
| R7 | Review and deletion | EM-16 (Soyeon, done) | **Pass** (automated) | N/A (labelled prototype) | — |
| R8 | Microphone control | EM-17 (Soyeon, done) | **Pass** (automated) | Not checked | Microphone off during a reaction, by hand |
| R9 | Viewing interface | EM-18 (Soyeon, done) | **Pass** (automated) | Not checked | A dropped connection, by hand |
| R10 | Reaction clips | EM-19 (Chul, in progress), EM-27 (Yujeong, in progress) | **Fail** (real webcam) | **Partial** | EM-19 false gaps, then a real webcam retest |
| R11 | Viewing sessions | EM-20 (Pavel, done) | **Pass** (automated) | **Partial** | Tab prototype reconnect |

PC app on `main`: 7 pass, 3 partial, 1 fail (on 2026-11-23: 5 pass, 4 partial, 2 unverified).

## Environment

| | Local (developer PC) | CI (run 45844722001 "Client integration", run 45844722252 "Server deployment smoke", both on `f694299`) |
|---|---|---|
| OS | Ubuntu 24.04.3 LTS, kernel 7.0.0-43-generic | GitHub `ubuntu-24.04` image |
| Python | 3.12.3, PySide6 6.8.3, websockets 15.0.1, numpy 2.5.5, soxr 1.1.0 | Same requirements files |
| GStreamer | 1.24.2 through PyGObject. CLI tools are not installed. `webrtcechoprobe` and `webrtcdsp` are missing. | 1.24.2 with tools, plugins-base/good/bad and libav. `webrtcdsp` is missing there too. |
| Node | v22.23.3 | `actions/setup-node@v4` |
| Browser | Chromium 156 (embedded browser), 2026-11-20 run | none |
| Containers | Not used in this run (see run 9) | `postgres:16` service; Compose and `kubectl kustomize` |
| YAMNet | TensorFlow 2.18.1 and tensorflow_hub 0.16.1, in a separate environment built from `server/requirements-yamnet.txt`. Weights `google/yamnet/1@sha256:c59c5744e1b92942`. | not installed |

## Runs

All on `f694299`, 2026-11-27.

| # | What | Command (from the repository root unless noted) | Result |
|---|---|---|---|
| 1 | Python suite | `.venv/bin/python -m pytest -q -rs` | **439 passed, 3 skipped**, locally and in CI. Skips: `test_yamnet_model.py` (no tensorflow_hub); `test_echo_capture.py::test_synthetic_echo_mixture_and_seek` (no `webrtcdsp`, an accepted limit); the PostgreSQL integration test (needs a database, see run 2). The linear canceller's playback test (`LinearEchoPlaybackTest`) runs and passes in both. |
| 2 | PostgreSQL history | `ENCORE_TEST_DATABASE_URL=… python -m pytest -q server/tests/test_event_history.py` | CI (`postgres-history`, `postgres:16`): **14 passed**. The new one since 11-23 is the expired-lease test (#41). Not run locally: the local environment has no `psycopg`, and installing it needs a download. |
| 3 | Tab prototype Node tests | `node --test client/tab-demo/tests/*.test.mjs` | CI: **8/8 pass**. Local: 7 pass, 1 skipped (the VP8/Opus decode test needs the GStreamer CLI). |
| 4 | Contract demo, headless | `cd client && ../.venv/bin/python -m encore_client --demo --headless` | Exit 0. `evt-0004` gasp OPEN → FINAL rev 3. A duplicate control, cursor and revision and a stale revision are ignored, and one `wrong_session` is rejected, as before. |
| 5 | PC → server, loudness stand-in | Server: `PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --port 8799`. PC: `cd client && ../.venv/bin/python -m encore_client --headless --no-player --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5` | Exit 0. `evt-0001` laughter OPEN → UPDATE → FINAL. 18 chunks sent, 0 dropped. The server applied microphone generation 2, then "session ended". |
| 6 | Two sessions in one run | Run 5 with `--sessions 2` | Exit 0. Two session IDs. Each got one laughter FINAL, with 18 chunks sent and 0 dropped. The server logged "session ended" for the first before the second connected. |
| 7 | PC → server, YAMNet, tone | Run 5 with `--model yamnet` (port 8798) | Exit 0. 18 chunks, **no event**. |
| 8 | PC → server, YAMNet, three reactions | Run 7 with `--mic-wav reactions.wav --timeout 6` | Exit 0. 55 chunks, 0 dropped. Three FINALs: laughter (confidence 0.87) at 1.4 s, scream (0.61) at 10.6 s, gasp (0.98) at 18.2 s. The clips start at 2.0, 10.9 and 18.3 s. |
| 9 | Compose fixture | CI job `compose-history` (`.github/workflows/deployment-smoke.yml`) | CI: **pass**. It builds the server with PostgreSQL history and runs the smoke and the PC smoke. Then it stops the history database and runs the smoke again, so that session's FINAL waits in the outbox. Finally it restarts the database, recreates the server, and reads every session's FINAL back. It also renders both Kubernetes configurations and checks their probes. Not run locally: the build needs the `python:3.12-slim-bookworm` image, which is not cached on this PC. |

**Inputs:**

- `tone.wav`: 2 s of silence, 3 s of a 440 Hz tone at half scale, then 4 s of silence (mono, 16 kHz, 16-bit). The loudness stand-in calls any loud sound laughter, so runs 5 and 6 check the pipeline, not recognition.
- `reactions.wav`: 27 s, same format. It has 2 s of silence, then three FSD50K evaluation clips, each followed by 5 s of silence:
  - laughter `62263` (3.9 s);
  - scream `426195` (2.4 s);
  - gasp `169931` (3.7 s).

  EM-11's evaluation already found these three, so run 8 checks that the PC path delivers them, not how often recognition works.

## Earlier runs used here

| Date | Commit | What | Result | Where |
|---|---|---|---|---|
| 2026-11-20 | `a82b6b5` | Browser recording test (`/tests/playback.html`) | **PASS**: 1 file, 470,925 bytes, VP8 + Opus. With resize: **PASS**, 2 files. | Previous version of this record |
| 2026-11-20 | build `70b1738` | Tab prototype, manual (user) | FINAL scream, audio confidence 0.543, first trigger latency 573 ms | Previous version of this record |
| 2026-11-23 | `0c5ce60` | History during outages (three cases from the #15 review) | Database down at End: the FINAL is archived 1.6 s after the database returns. Down during a live session: the microphone-off ack and the FINAL still arrive, and the FINAL is archived after recovery. Server stopped mid-reaction: the reaction is closed and archived. `history.py` has changed since only to forget a session whose lease ran out (#41). | Previous version of this record |
| 2026-11-23 | `0c5ce60` | PC app in a real window, laptop webcam (MJPEG 1080p), loudness server, WAV microphone | See R5, R6, R10 | Jira EM-2, EM-4 |
| 2026-11-24 | #44 | Echo cancellation evaluation with YAMNet (MacBook) | See R1 | `docs/em-10-aec-evaluation.md` |
| 2026-11-26 | `7ebd10f` | PC app live, real microphone and laptop camera, YAMNet and MediaPipe, cued steps | See R2 | `docs/em-26-trigger-check.md`, section 7 |
| 2026-11-26 | #58 | `detector-v3` on the FSD50K evaluation clips | See R2 | `docs/em-11-evaluation.md`, sections 6 and 8 |

## Per requirement

### R1 Echo cancellation: PC app Partial

- **Expected:** compare AEC on and off on soundtrack-only, audience-only and overlapping recordings, including seeks and output-device changes. Residual playback is suppressed, and laughter, scream and gasp stay recognizable.
- **Built:**
  - #33: the live microphone is cancelled against the player's reference, and each chunk carries its effective AEC state.
  - #44, #47, #48, #54: where `webrtcdsp` and `webrtcechoprobe` are missing (Ubuntu 24.04, macOS), the player copies its own output just before the audio sink, and a linear canceller (`sensing/linear_aec.py`, `player/linear_echo.py`) removes it from the microphone. The echo's bulk delay is measured and aligned.
  - #53 (R12, EM-21): the user can turn echo cancellation off and on, and the window shows the requested and the effective mode.
- **Passing tests:**
  - `test_linear_aec.py` and `test_linear_echo.py`: convergence, reference timing, seeks, jittered timestamps, bulk delay, out-of-range echo reported `uncancelled`;
  - `LinearEchoPlaybackTest`: the WebRTC test's scene through the real player, in CI on Ubuntu;
  - `test_aec_mode.py` and the plumbing tests in `test_echo_capture.py`.
- **Measured (`docs/em-10-aec-evaluation.md`):** 40 laughter clips with spoken playback through a synthetic room, the echo from 6 dB quieter to 6 dB louder than the laughter. YAMNet still finds laughter in this share of the windows:

  | | Linear canceller | No AEC | WebRTC AEC3 |
  |---|---|---|---|
  | Laughter windows kept | 51–59% | 3–13% | 0–3% |

  The echo alone never triggered laughter.
- **Accepted limit:** no `webrtcdsp`. `test_synthetic_echo_mixture_and_seek` stays skipped locally and in CI.
- **Missing:**
  - a room recording with real speakers (soundtrack-only, audience-only, overlapping);
  - output-device changes;
  - music as playback.

  The canceller assumes one clock for the speakers and the microphone, so the demonstration should use one device for both.
- **Owner:** EM-10 (Yujeong). `client/tools/aec/room_recording.py` exists for the room recording.

### R2 Reaction recognition: PC app Pass (team target)

- **Expected:** labeled laughter, scream, gasp, speech, soundtrack-only and mixed recordings. Camera-off operation, missing or occluded faces, one strong and several weak responses. Per-class errors and measured detection delay.
- **Per-class errors and delay** (`docs/em-11-evaluation.md`): 40 FSD50K evaluation clips per class, never used for tuning, audio only, with `detector-v3`:

  | | Found | Median delay |
  |---|---|---|
  | Laughter | 13/40 (32%) | 1.43 s |
  | Scream | 26/40 (65%) | 0.78 s |
  | Gasp | 20/40 (50%) | 0.70 s |

  False events per minute: clean speech 0.8, clean music 0.3, playback alone 0.2. The reactions were also mixed with speech and music playback at +10 and 0 dB.
- **Camera and faces:**
  - camera off and no face: EM-26's scripted cases (#45);
  - live (`docs/em-26-trigger-check.md`, section 7): 11 of 12 cued steps matched. Talking and silent smiling opened nothing. One of two soft chuckles with a smile opened through the smile path; YAMNet scored the other too low as laughter. Trigger to OPEN took 648–663 ms on sound alone and 803–849 ms through the smile path;
  - several faces: the face worker keeps the largest smile of up to 4 faces, so weak faces never add up. No test feeds it several real faces.
- **PC path:** run 8 delivers a laughter, a scream and a gasp event; run 7 gives no event for a tone.
- **Versioning:** each event's `source.model` names the weights by SHA-256 prefix (#52), and `source.config` names `detector-v3`.
- **Team target:** met on every line (EM-11, section 8).
- **Accepted limits:**
  - laughter recall is weak: about a third of the laughs; crowd laughter, laughter inside speech and soft chuckles are mostly missed;
  - heavy breathing, sighs and sneezes can open a gasp (15 of 40 breathing and cough clips);
  - no recordings of a real audience.
- **Spec mismatch:** the specification names PANNs at 32 kHz; the code runs YAMNet. The team keeps YAMNet, and the final report states the change.
- **Tab prototype:** unchanged, one manual run.

### R3 Event records: PC app Pass (automated)

- **Expected:** replaying the same input or result twice gives one event. Timecodes stay right before and after pause, seek, rate change and title change, including delayed network delivery.
- **Actual:**
  - duplicate handling: `test_same_result_twice_leaves_one_event` and run 4;
  - pause, seek, rate and title: `test_anchors.py` and the server's anchor tests;
  - delayed delivery: `test_playback_timecode.py` (#22), including a late anchor read after the server applied it.

  With `--record-history`, FINAL records also survive a restart (run 2), which R3 does not require.
- **Observation:** a recorded start can be up to one analysis window early. In run 8 the events start 0.6, 0.3 and 0.1 s before their clips.

### R4 Highlights: PC app Pass (automated)

- **Expected:** mixed intensities, equal scores, shared clips, camera-off events, failed exports and pending deletions. Check the ordering and every displayed link.
- **Actual:** `test_highlights.py` (#26, 17 tests) covers:
  - ordering by intensity, ties by trigger time then event ID, missing intensity last, and the same order whatever the arrival order;
  - only FINAL events listed;
  - events without footage listed without a link;
  - one link only for a ready or partial clip whose file exists and is not empty;
  - no link while pending, failed or being deleted;
  - a shared clip linked from both events;
  - parts of a capped clip named in order;
  - the panel rereads the index when a pending clip finishes.

  Since #42, `test_clip_reasons.py` (17 tests) checks that every view gives the same reason for a clip without footage (camera off, export failed, and so on).
- **By hand (2026-11-23):** an event row played its own clip in the real window.
- **Not done:** the ordering has not been checked by hand.

### R5 Camera activation: PC app Partial

- **Expected:** inspect device access, disk writes and network packets before enable, after deny and after disable. Race disable against queued upload and clip completion.
- **Automated:** these pass with fake devices and `videotestsrc`:
  - the 14 camera-control tests;
  - `test_privacy_cancel_and_camera_off_metadata`;
  - `test_offline_camera_audio_stays_local_and_cannot_leak_into_reconnect`;
  - `test_camera_image_belongs_to_one_audio_interval_and_one_connection`.
- **Real webcam (2026-11-23):**
  - a session with the camera off wrote no WebM and no ring folder;
  - with the camera on, the span before activation was not in the file.
- **Not done:** device access and network packets have not been inspected with a real webcam.
- **Owner:** EM-14 (Soyeon). EM-27 (Yujeong) covers the desktop camera wiring.

### R6 Camera indication: PC app Partial

- **Expected:** switch views during capture, stop while an export is pending, unplug the webcam. The indicator matches the actual pipeline state and media writes.
- **Automated:** the indicator and Stop control pass with the fake device (`test_camera_panel_shows_actual_state_and_persistent_stop`, `test_unplug_during_capture_shows_error_and_turns_camera_off`).
- **Real webcam (2026-11-23):** the panel showed "Camera off", then "Camera on (recording)" with Stop enabled. Stop showed "Camera off" at once.
- **Not done:**
  - the indicator has not been compared against the pipeline state and media writes;
  - a real webcam has not been unplugged during capture.
- **Owner:** EM-15 (Soyeon).

### R7 Review and deletion: PC app Pass (automated), tab prototype N/A

- **Expected:** delete during playback or export, repeat a deletion, disconnect during cleanup, restart the app. No stale response restores the media or its play link.
- **Actual:** `test_clip_deletion.py` (#27, #34, 23 tests) covers:
  - the journal is written before any file is deleted;
  - deleting during playback stops and releases the clip; deleting during export cancels it and blocks its commit;
  - a repeated deletion is one operation;
  - a late result cannot restore media or a link;
  - a restart resumes an incomplete deletion;
  - files shared with a retained clip, or outside the clip folder, are kept;
  - a damaged or lost journal waits for repair without losing tombstones.

  The panel shows pending, then deleted.
- **No server cleanup (#56):** the server keeps no clip media and no clip reference, so a deletion is local only and a reconnect has nothing to sync (`client/docs/em-20-reconnect.md`).
- **Tab prototype:** N/A by the team decision: it is shown as a labelled prototype, and it still has no delete action.
- **Owner:** EM-16 (Soyeon).

### R8 Microphone control: PC app Pass (automated)

- **Expected:** disable during an open event and during a queued inference. No new accepted audio event, microphone read or audio upload at the client after the control takes effect.
- **Actual:** the microphone-switch, audio-upload and server-session tests still pass, with `test_mic_off_splits_capture_and_prevents_more_audio` for the recorder's side. Since #49 the window warns when the microphone sends only exact zeros.
- **Not done:** the EM-26 live runs used a real microphone, but nobody turned it off during a reaction by hand.
- **Owner:** EM-17 (Soyeon).

### R9 Viewing interface: PC app Pass (automated)

- **Expected:** out-of-order and duplicated updates, an interrupted socket, a full analysis queue. Correct event count, visible connection gaps, responsive Stop controls.
- **Actual:**
  - order and duplicates: contract tests and run 4;
  - interrupted socket: reconnect tests (#23) and `test_missing_cursor_reconnects_and_superseded_events_are_applied_once`;
  - full queue: `test_bounded_incoming_messages_and_stop_while_sender_is_blocked` and `test_partial_drain_marks_unapplied_results_stale_before_queue_saturation` (#36).

  The window shows:
  - the timeline;
  - the highlight list with clip status;
  - connection state;
  - microphone and camera state;
  - the requested and effective echo cancellation.

  The 2026-11-23 run used the real window.
- **Not done:** a dropped connection has not been tried by hand.
- **Owner:** EM-18 (Soyeon).

### R10 Reaction clips: PC app Fail (real webcam), tab prototype Partial

- **Expected:**
  - trigger near camera activation, buffer rollover and session end;
  - network delay, overlapping events, format changes, disk exhaustion;
  - decodable, synchronized output; actual coverage; missing parts labeled;
  - one trigger gives 25 s; each re-fire adds 5 s; no clip over 60 s.
- **Automated (#35, #37):** these pass with `videotestsrc`, raw and MJPEG at 5 fps (`test_desktop_recorder.py`, `test_desktop_media.py`):
  - re-fires extend the clip, the cap starts a linked part, and duplicate revisions are ignored;
  - footage from before camera activation is labeled as a gap;
  - pinned segments survive eviction;
  - going over the disk budget cancels safely;
  - after a crash, recovery removes the ring buffer and incomplete files;
  - microphone off splits the capture;
  - the exported WebM decodes with matching audio and video timestamps.
- **Real webcam (2026-11-23, Jira EM-2):**
  - **Passed:**
    - the clip was saved 17.5 s after the FINAL;
    - the real gap before activation (0–11.0 s) was labelled;
    - in the #35 review's webcam run, a 35 s file held the 25 s clip and its handles.
  - **Failed:** the clip was marked `partial` with 52 false gaps, and its label in the clip list was 876 characters long. Coverage is computed from the webcam's frame timestamps, which jitter. The cause and a fix are in the #35 review and EM-19's comment.
  - The clips from the 2026-11-26 EM-26 live runs were also `partial`. `clips/export.py` has not changed since `0c5ce60`.
- **Not run:** an injected signal delay longer than the buffer retention (35 s).
- **Tab prototype:** unchanged. The automated clip-plan, decode and browser recording cases pass; disk exhaustion and injected delay have not been run.
- **Plan:** EM-19 cutoff on Nov 30 (team decision), then a real webcam retest.
- **Owner:** EM-19 (Chul); EM-27 (Yujeong).

### R11 Viewing sessions: PC app Pass (automated), tab prototype Partial

- **Expected:** repeat Start/End; reconnect after a committed but unacknowledged result; end while offline; restart during cleanup. One event per stable ID, and no unsaved media after cleanup.
- **Actual (PC app):**
  - **repeat Start/End (#39):** after End, Start opens a new session with a new ID, and everything that held the old one moves to the new one (`test_session_restart.py`, 8 tests). Run 6 does it twice in one run;
  - **reconnect (#23):** after the cursor, results lost in flight are replayed and controls are resynced first. A lost server session is not shown as recovered (`test_reconnect.py`);
  - **End (#24, #29, #36):** End waits for open reactions and gives up after the grace period. It ends at once while disconnected, and a headless run fails when the server does not confirm the end;
  - **end while offline (#41):** the server keeps a dropped session for 60 s (`LEASE_S`), then ends it as End would. Open reactions get their FINAL, which is archived when history is on (run 2);
  - **deletion (#56):** a reconnect has no deletion intents to sync, because the server holds no clip media;
  - **cleanup:** after a crash, recovery removes unsaved media (R10).
- **Limits** (`client/docs/em-20-reconnect.md`, "Not covered here"):
  - restart during cleanup is tested at the recorder level only (`test_interrupted_capture_cleanup_and_active_owner`);
  - the contract has no End request, so the server's lease takes the place of a persisted End retry.
- **Tab prototype:** stopping the share deletes unsaved segments. Reconnect has not been checked.
- **Owner:** EM-20 (Pavel).

## Deployment (R13, needed for the rerun)

- **Compose (EM-3, #31):** `deploy/compose.yaml` runs the server image with readiness probes. `deploy/compose.history.yaml` adds PostgreSQL history with a persistent outbox. CI checks it on every server change (run 9).
- **Kubernetes (EM-22, #32):** `deploy/k8s` and `deploy/k8s-history` render in CI, and their startup, liveness and readiness probes are checked.
- **History (EM-29):** FINALs are committed to a local SQLite outbox before delivery and drained to PostgreSQL with retries. A session whose lease runs out is archived like an ended one (#41).
- **Accepted limits:** no GPU Kubernetes run and no EC2–RDS check.
- **Not run:** the real-model image (`ENCORE_BUILD_TARGET=inference` with the YAMNet and Face Landmarker bundle).

## Open items by owner

| Owner | Issue | Item | Blocks |
|---|---|---|---|
| Chul (after Nov 30, someone else with Chul's OK) | EM-19 | Fix the false gaps on a real webcam | R10 |
| Pavel | EM-28 | Retest R10 with a real webcam after the EM-19 fix | R10 |
| Pavel | EM-28 | Live run with a real webcam and microphone, which adds the hand checks to the automated passes:<ul><li>R4: Highlights ordering;</li><li>R5: device access and network packets;</li><li>R6: indicator vs pipeline;</li><li>R8: microphone off during a reaction;</li><li>R9: a dropped connection.</li></ul> | R5, R6 |
| Yujeong | EM-10 | Room recording with real speakers | R1 |
| Chul | EM-22 | Real-model image | R13 |
| Team | — | Final report: YAMNet instead of PANNs, and the accepted limits | R1, R2, R13 |
| Pavel | EM-28 | Rerun this record on the release commit | all |

## Before and after Nov 30

The development deadline is Dec 3.

- **By Nov 30:**
  - the EM-19 fix (Chul);
  - the EM-28 live run with a real webcam and microphone;
  - the EM-10 room recording, if there is time.
- **Dec 1–2:**
  - if EM-19 is not fixed, someone applies the 11-23 review's fix with Chul's OK;
  - retest R10 with a real webcam;
  - rerun this record on the release commit;
  - fix what the rerun turns up.

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

# PC to a separate server: loudness stand-in, one session and then two
PYTHONPATH=server .venv/bin/python -m encore_server --model loudness --port 8799 &
cd client && ../.venv/bin/python -m encore_client --headless --no-player \
  --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5
../.venv/bin/python -m encore_client --headless --no-player \
  --mic-wav tone.wav --server ws://127.0.0.1:8799 --timeout 5 --sessions 2; cd ..

# PC to a YAMNet server (TensorFlow environment from server/requirements-yamnet.txt)
PYTHONPATH=server yamnet-env/bin/python -m encore_server --model yamnet --port 8798 &
cd client && ../.venv/bin/python -m encore_client --headless --no-player \
  --mic-wav reactions.wav --server ws://127.0.0.1:8798 --timeout 6; cd ..

# Compose fixture
docker compose -f deploy/compose.yaml up --build --wait
docker compose -f deploy/compose.yaml exec -T server python deploy/smoke.py
python3 deploy/smoke.py --pc --server ws://127.0.0.1:8876
docker compose -f deploy/compose.yaml down

# Browser recording test
python3 -m http.server 8002 --bind 127.0.0.1 --directory client/tab-demo
# then open http://localhost:8002/tests/playback.html and press "Run recording test"
```

To build `reactions.wav`, take the three FSD50K evaluation clips named under "Inputs" at 16 kHz (`server/tools/em11_eval.py` prepares them). Put 2 s of silence before them and 5 s after each.
