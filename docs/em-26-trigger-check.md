# EM-26: the laughter trigger's 5 s score and its delay

Date: 2026-11-24. Machine: laptop, AMD Ryzen 7 8845HS, Ubuntu 24.04.3, CPU only, everything on one PC (no network). Server code: `main` at 9b5be45; this work changes nothing in `server/encore_server/`. Section 7 adds a live rerun on 2026-11-26 with `main` at 7ebd10f, after the app fixes in #46 and #49.

EM-26 asks four things about the laughter rule in `server/encore_server/fusion.py`. Does it open an event when it should, and not otherwise? Can repeated or late camera frames count the same audio twice? Where does the 5 s window end? How long is it from the laugh to OPEN on the PC?

## 1. The rule under test

| Setting (`TabRules`) | Value |
|---|---|
| Strong laughter: opens on sound alone | YAMNet laughter ≥ 0.25 |
| Assisted laughter: needs a smile in the same hop | ≥ 0.10 (still counts, at less weight, down to 0.05) |
| Smile (mean of MediaPipe `mouthSmileLeft` / `mouthSmileRight`) | ≥ 0.20 (counts down to 0.10) |
| Evidence window | 5000 ms |
| Evidence needed | 3 equivalent hops = 1.44 s. A hop with both signals at or above threshold adds 0.48 s; weaker hops add less. |
| Hop / analysis window | 480 ms / 975 ms (one YAMNet frame) |
| `source.config` in each laughter event | `fusion-5000ms-3h-a0.25-a0.1-s0.2-q3000` |

These are the `TabRules` defaults plus the server's `--smile-threshold` and `--demo-quiet-ms`. Nobody has tuned them on audience recordings yet.

## 2. How it was checked

`server/tools/em26_trigger_check.py` feeds the real `ServerSession` the packets a PC sends: 0.5 s audio chunks, each paired with one camera frame stamped 20 ms before the chunk ends (the PC's newest frame). Everything after the socket is production code: audio admission, analysis windows, face pairing by capture time, the fusion and the detector.

- **Scripted** (the default, run in CI by `server/tests/test_em26_trigger_check.py`): the audio model reads a laughter score written into the audio, and the face model reads a smile written into the image. Each case states exactly what the models report, so the run tests only the server's timing and fusion.
- **Real models**: `--audio-model yamnet --wav LABEL=FILE` sends recorded audio through YAMNet. Each file plays after 2 s of silence and is followed by 3 s of silence. It runs three times: camera off, a neutral face, and a smile (0.20) while the file plays. `--face-python/--face-task` swaps the scripted face for the MediaPipe worker, which sees blank frames (camera on, no face found).
- **Clock**: simulated by default. A chunk reaches the server when its last sample is captured, plus the measured time `on_audio` took. `--realtime` sends each chunk at its real capture time on the monotonic clock, and the run times OPEN when `on_audio` returns it.

A mutation check confirmed the cases catch regressions. Each of these broken versions fails at least one case: a face bin widened to ±0.96 s, the window at 4.5 s (fails `edge-inside`) or 5.5 s (fails `edge-outside`), frames accepted at any capture time, and duplicate audio chunks processed.

## 3. Scripted cases: expected vs actual

Hop *n* is the analysis window starting *n* × 480 ms after the first sample. Delays come from the simulated clock, 3 runs each, and all 3 runs gave the same numbers.

| Case | Input | Expected | Actual | Trigger hop (exp/act) | Trigger → OPEN | Reaction start → OPEN |
|---|---|---|---|---|---|---|
| strong-alone | laughter 0.30, neutral face | OPEN | OPEN | 2 / 2 | 554 ms | 794 ms |
| weak-with-smile | 0.10 and smile 0.20 together | OPEN | OPEN | 4 / 4 | 594 ms | 1794 ms |
| weak-alone | 0.10, neutral face | none | none | | | |
| smile-alone | 0.02, smile 1.0 | none | none | | | |
| no-reaction | 0.01, neutral face | none | none | | | |
| camera-off-strong | 0.30, camera off | OPEN | OPEN | 2 / 2 | 554 ms | 794 ms |
| camera-off-weak | 0.10, camera off | none | none | | | |
| no-face-strong | 0.30, camera on, no face found | OPEN | OPEN | 2 / 2 | 554 ms | 794 ms |
| no-face-weak | 0.10, camera on, no face found | none | none | | | |
| two-hops | 0.10 + smile for 2 hops (0.96 of 1.44 s) | none | none | | | |
| resent-packets | weak-with-smile, every packet sent twice | OPEN | OPEN | 4 / 4 | 594 ms | 1794 ms |
| same-frame-twice | each frame stamped 100 ms after its chunk and sent with 2 chunks | OPEN | OPEN | 6 / 6 | 634 ms | 2794 ms |
| smile-after-sound | 0.10 for hops 2–8, smile from hop 8, frames stamped 399 ms after their audio | none | none | | | |
| frames-one-chunk-late | weak-with-smile, each frame arrives with the next chunk | none | none | | | |
| edge-inside | paired hops 2, 3, 11: oldest hop starts 4.8 s before the trigger hop ends | OPEN | OPEN | 11 / 11 | 734 ms | 5294 ms |
| edge-outside | paired hops 2, 3, 12: 5.28 s | none | none | | | |

All 16 match. No audio hop was credited twice in any case:

- A resent packet is dropped as a duplicate chunk.
- A frame sent with two chunks lands twice in the same hop's bin and still counts once. Only every other frame was sent, so OPEN comes at hop 6, not hop 4.
- A frame stamped after its audio counts for the hop of its own capture time. That is the 399 ms case from EM-26 comment 10011.
- A frame that arrives one chunk late is older than its chunk and is dropped.

At the edge, the oldest hop counts only for the part inside the window: 0.2 s of its 0.48 s at hop 12. The total then stays at 1.16 s.

`--realtime` (1 run, real clock) gives the same 16 OPEN results, with trigger → OPEN of 555–735 ms: 1–2 ms above the simulated values.

## 4. Real models

- **Audio**: YAMNet `google/yamnet/1` from TF-Hub, TensorFlow 2.18.1, tensorflow-hub 0.16.1, tf-keras 2.18.0, numpy 1.26.4, as pinned in `server/requirements-yamnet.txt`. Laughter is the maximum over Laughter, Baby laughter, Giggle, Snicker, Belly laugh and Chuckle/chortle (`server/encore_server/models.py`).
- **Face**: MediaPipe 0.10.21, `face_landmarker.task` (sha256 `64184e229b26…`, 3 758 596 bytes), with the environment from `deploy/models/requirements-face.txt`.
- **Inputs**: 8 clips from ESC-50 (Piczak, ACM MM 2015, CC BY-NC 3.0), fetched one by one from the dataset's GitHub repository. They were resampled to 16 kHz mono 16-bit with `ffmpeg -ac 1 -ar 16000 -sample_fmt s16`. They are not committed. Like the EM-10 evaluation, this is a small public set, not audience recordings. It shows how the rule behaves with real scores; it is not an accuracy figure.

Time is from the start of the clip to OPEN on the simulated clock; 3 runs each gave the same values. "Strong / weak hops" counts hops ≥ 0.25 and hops from 0.05 up to 0.25.

| Clip | ESC-50 class | Peak laughter | Strong / weak hops | Camera off | Neutral face | Smile |
|---|---|---|---|---|---|---|
| 1-1791-A-26 | laughing | 0.291 | 1 / 1 | OPEN 1.51 s | OPEN 1.51 s | OPEN 1.51 s |
| 1-30039-A-26 | laughing | 0.222 | 0 / 6 | **miss** | **miss** | OPEN 4.01 s |
| 1-30043-A-26 | laughing | 0.092 | 0 / 5 | **miss** | **miss** | OPEN 2.00 s |
| 1-33658-A-26 | laughing | 0.719 | 3 / 3 | OPEN 1.00 s | OPEN 1.00 s | OPEN 1.00 s |
| 1-36164-A-26 | laughing | 0.414 | 1 / 2 | OPEN 1.00 s | OPEN 1.00 s | OPEN 1.00 s |
| 1-19111-A-24 | coughing | 0.072 | 0 / 1 | none | none | none (evidence 0.41 s) |
| 1-104089-A-22 | clapping | 0.042 | 0 / 0 | none | none | none |
| 1-187207-A-20 | crying_baby | 0.165 | 0 / 3 | none | none | none (evidence **1.43 s of 1.44**) |

- **Misses**: 2 of 5 laughing clips with the camera off or a neutral face (4 of 10 runs), 0 of 5 with a smile.
- **False positives**: 0 of 9 non-laughter runs.
- **Real face model**: MediaPipe on blank frames finds no face, and every clip then behaves exactly as with the camera off: 3 of 5 laughs open, 0 of 3 others.

The two missed laughs never reach 0.25. With a smile, their weak hops add up: 0.054, 0.064, 0.090, then 0.222 at 4.0 s for one clip, and five hops of 0.06–0.09 for the other. The crying baby scores 0.135, 0.165 and 0.096 in three hops. With a smile that adds 0.48 + 0.48 + 0.47 = 1.43 s, 0.01 s short of an OPEN, because the third hop is just under 0.10.

## 5. Delay

Trigger → OPEN has three parts:

1. **Half a window, 488 ms.** The trigger time is the analysis window's centre, and the window ends 488 ms later.
2. **Waiting for the chunk, 0–499 ms.** The window is complete only when the 0.5 s chunk that holds its end arrives. With 480 ms hops and 500 ms chunks this wait cycles. It was 65 ms at hop 2 (554 ms in total), 105 ms at hop 4 and 245 ms at hop 11.
3. **Server processing**, measured on this PC:

| | Simulated runs | Realtime run |
|---|---|---|
| YAMNet, per hop | 3.6–4.7 ms (median per case) | 5.8–7.0 ms |
| MediaPipe, per frame, no face in the frame | 2.1–2.3 ms | 3.1–3.3 ms |
| Trigger → OPEN, ESC-50 laughs | 598–618 ms | 602–624 ms |
| Clip start → OPEN, ESC-50 laughs, sound alone | 1.00–1.51 s | 1.01–1.51 s |

The realtime run is slower per call because the CPU idles between chunks.

So on the server, trigger → OPEN is 488–987 ms plus a few milliseconds, about 0.74 s on average. From the start of a reaction, strong laughter opens about 0.8 s later. Assisted laughter needs two more paired hops, so about 1.8 s. With the real app in the loop it was 943 ms for a hop that the server alone would answer in about 0.5 s (section 6), and 648–663 ms once the app sent each chunk as soon as it ended (section 7). Not measured: the network to the EC2 server. MediaPipe on a real face took 11–21 ms on this PC (sections 6 and 7); the EC2 smoke test in `deploy/models/README.md` gives 12 ms for one portrait image.

## 6. Live run on this PC

Two sessions on 2026-11-24 with the app (`encore_client --mic`), this PC's microphone and camera, and a local server running YAMNet and MediaPipe. There was no network: app and server were on the same PC. The server kept only the numbers from each scored window (scores, face found, model times). The app's clip and both logs were deleted right after each run. A cue script with timed steps (quiet, talking, loud laugh, silent smile, smile with a chuckle) was not started in either run. The results are therefore per session, not per step, and the smile scores are not checked against what the face was doing.

| | Run 1 | Run 2 |
|---|---|---|
| Microphone | muted at the system level (noticed only afterwards) | on |
| Length | 298 scored windows (about 2.4 min) | 118 s |
| Camera frames paired with audio | 297 of 297 windows found a face | none |
| Laughter events | 0; laughter 0.000 in every window | 1, on sound alone (confidence 0.32), during a loud laugh |
| Other OPENs | 0 | 0, including while talking |

**Model times with a real face** (run 1, per call):

| | Median | p95 | Max |
|---|---|---|---|
| MediaPipe, per frame | 21.0 ms | 28.9 ms | 31.7 ms |
| YAMNet, per window | 9.5 ms | 14.7 ms | 16.6 ms |
| Server `on_audio`, per chunk with a window | 12 ms | 19 ms | 30 ms |

**Smile scores** (run 1): 97 windows above 0.20, with a median of 0.76 and the lowest at 0.25. 189 windows below 0.20, with a 95th percentile of 0.019.

**Trigger → OPEN, run 2**: 943 ms, measured on the PC's monotonic clock from the trigger time in the event to the moment the app logged the OPEN.

| Part | Time |
|---|---|
| Half a window | 488 ms |
| Waiting for the chunk | about 5 ms |
| From the chunk's last sample to the server | about 351 ms |
| Server processing | 19 ms |
| From the server's reply to the app's log | about 80 ms |

Three problems showed up in the app, not in the server's rule:

1. **The app sends each audio chunk about 350 ms after it ends.** All 235 chunks in run 2 left the app 349–360 ms after their last sample. A likely cause: the app's microphone buffer is 0.5 s (`client/encore_client/sensing/live_mic.py`), so audio arrives in reads of about 0.5 s. The app also drops the first 150 ms (`SETTLE_MS`), so each chunk ends 350 ms before the read that completes it. This adds about 350 ms to every OPEN.
2. **No camera frames reached the server in run 2.** The app sends a chunk with its newest camera frame only if that frame was captured during the chunk (`Session.send_audio`). If chunks leave 350 ms after they end, the newest frame is always too new. Frames did pair in run 1, so this cause is not proven. Run 2 therefore behaved like a run with the camera off, and the smile path was not tested live.
3. **A silent microphone gives no warning.** In run 1 the microphone was muted at the system level. Every laughter score was 0.000 for the whole session, and nothing in the app showed it.

## 7. Live rerun after the app fixes (2026-11-26)

Two more sessions with the same setup as section 6: the app (`encore_client --mic --no-player`), this PC's microphone and camera, and a local server running YAMNet and MediaPipe, all on one PC. `main` was at 7ebd10f. It has the fixes for section 6's problems. #46 sends each audio chunk as soon as it ends, with a camera frame from that chunk. #49 warns when the microphone sends only zeros. As before, the server kept only the numbers from each scored window, and the app's clips and logs were deleted right after each run. One person, one room, a laptop camera.

Rule: the section 1 defaults, `fusion-5000ms-3h-a0.25-a0.1-s0.2-q3000`. Laughter ≥ 0.25 opens on sound alone. Otherwise laughter with a smile in the same hop adds evidence, and 1.44 s of it within 5 s opens. 3 s of quiet closes the event. Models in every event's `source.model`: `google/yamnet/1@sha256:c59c5744e1b92942+mediapipe-face@sha256:64184e229b263107`.

**Run 4 (cued).** A cue script showed 12 timed steps and logged when each started, on the same monotonic clock as the app and the server. A window counts in the step that holds its centre, and an OPEN counts in the step that holds its trigger.

| # | Step | Expected | Windows (face found) | Laughter max | Smile median / max | Paired hops* | Actual |
|---|---|---|---|---|---|---|---|
| 1 | Quiet, neutral face, 20 s | no OPEN | 41 (41) | 0.013 | 0.00 / 0.00 | 0 | no OPEN |
| 2 | Talking, neutral face, 20 s | no OPEN | 42 (42) | 0.002 | 0.00 / 0.00 | 0 | no OPEN |
| 3 | Smile, no sound, 15 s | no OPEN | 31 (31) | 0.008 | 0.65 / 0.77 | 0 | no OPEN |
| 4 | Quiet, 10 s | no OPEN | 21 (21) | 0.009 | 0.72 / 0.80 | 0 | no OPEN |
| 5 | Smile with a soft chuckle, 10 s | OPEN | 21 (21) | 0.216 | 0.84 / 0.94 | 3 | OPEN, confidence 0.132 (smile path) |
| 6 | Quiet, 15 s | no OPEN | 31 (31) | 0.051 | 0.00 / 0.90 | 0 | no OPEN |
| 7 | Loud laugh, 8 s | OPEN | 17 (17) | 0.456 | 0.77 / 0.96 | 3 | OPEN, confidence 0.456 (≥ 0.25, so sound alone) |
| 8 | Quiet, 15 s | no OPEN | 31 (31) | 0.007 | 0.00 / 0.51 | 0 | no OPEN |
| 9 | Face turned away, soft chuckle, 10 s | no OPEN | 21 (4) | 0.034 | 0.00 / 0.00 | 0 | no OPEN |
| 10 | Quiet, 10 s | no OPEN | 21 (21) | 0.013 | 0.00 / 0.05 | 0 | no OPEN |
| 11 | Smile with a soft chuckle, 10 s | OPEN | 21 (21) | 0.022 | 0.32 / 0.51 | 0 | **no OPEN (missed)** |
| 12 | Quiet, 15 s | no OPEN | 31 (31) | 0.003 | 0.00 / 0.19 | 0 | no OPEN |

\* Windows with laughter ≥ 0.10 and smile ≥ 0.20, which add the full 0.48 s each. Weaker windows add less (section 1).

- 11 of 12 steps matched. There was no OPEN in any step without laughter, including 15 s of silent smiling (step 3) and 20 s of talking (step 2).
- Step 5 opened only through the smile path: no window reached 0.25, and the event's confidence is 0.132.
- Step 11 missed because YAMNet did not score the chuckle as laughter (0.022 at most, below the 0.10 the smile path needs). The smile was there (median 0.32).
- Step 9 cannot tell "no face" from "too quiet". The chuckle scored 0.034 at most, so it would not have opened with a face either.

**Run 3 (no step times).** About 100 s with the same kinds of steps, done before the cue script was started, so the steps have no times. Two OPENs:

- Five windows with laughter 0.05–0.21 and smile 0.44–0.91 opened an event with confidence 0.206, through the smile path.
- One window with laughter 0.332 opened an event. That is above 0.25, so sound alone was enough.
- Two stretches of smiling with laughter under 0.04 (about 8 s at smile 0.42–0.88 and about 5 s at 0.21–0.55) opened nothing. A single window with laughter 0.119 and smile 0.92 added one full hop, short of the 1.44 s needed, and opened nothing.

**Trigger → OPEN** (the app's log of the OPEN minus `capture.triggers_ms[0]`, both on the PC's monotonic clock):

| | Run 3 | Run 4 | 2026-11-24 run 2 |
|---|---|---|---|
| Sound alone | 648 ms | 663 ms | 943 ms |
| Smile path | 849 ms | 803 ms | not measured |

| Part (run 4) | Time |
|---|---|
| Trigger → the server commits the OPEN | 574 ms (sound alone), 713 ms (smile path) |
| The server commits → the app logs it | about 90 ms |
| A chunk's last sample → the server | median 32 ms, p95 33 ms (429 chunks); 351 ms on 2026-11-24 |
| Server `on_audio`, per chunk | median 8.1 ms, p95 11.5 ms |
| MediaPipe, per frame | median 13.9 ms, p95 15.3 ms |

**Camera frames.** 418 of 429 chunks in run 4 and 193 of 201 in run 3 carried a camera frame, and MediaPipe found the face in 402 of 418 and 191 of 193. In run 2 on 2026-11-24, no frame reached the server.

## 8. Findings

1. **The rule does what it says.** Every scripted case matches. Camera-off and no-face fall back to sound alone, repeated or late frames never credit a hop twice, and evidence older than 5 s drops out. No server change is needed for EM-26's rule checks.
2. **Sound alone misses quieter laughs.** On this small public set, 2 of 5 laughing clips never reach 0.25. The smile path recovers both.
3. **The smile path has little margin against non-laughter.** A crying baby with a smiling face came within 0.01 s of an OPEN. A labeled set of audience recordings (EM-11) is needed before changing 0.10 / 0.20 / 1.44 s.
4. **One hop in 25 gets no face.** The PC sends one frame per 0.5 s chunk and hops are 0.48 s. When a hop's ±240 ms bin holds no frame, that hop earns no paired credit (hop 13 here). Sustained laughter loses about 4% of its evidence, and the delay barely changes.
5. **Events say which face model was used.** On 2026-11-24, `source.model` got `+mediapipe-face` with no version or file hash. Since #52 it carries the SHA-256 prefix of both models' files, e.g. `google/yamnet/1@sha256:c59c5744e1b92942+mediapipe-face@sha256:64184e229b263107`. `source.config` records the thresholds.
6. **The models are not the delay.** Waiting for the 0.5 s chunk that completes the window adds 0–499 ms (about 250 ms on average). The models take a few milliseconds, and MediaPipe takes 21 ms on a real face. Smaller chunks would shorten the wait; the 488 ms half window stays.
7. **Live delay is now 0.65–0.85 s.** On 2026-11-24 trigger → OPEN was 943 ms, about 350 ms of it from the app holding each chunk (section 6). With #46 a chunk reaches the server about 32 ms after it ends, and trigger → OPEN was 648–663 ms on sound alone and 803–849 ms through the smile path (section 7).
8. **The smile path works live.** On 2026-11-24 no camera frames reached the server with the microphone on (section 6). With #46, 97% of chunks carry a frame, a smile with weak laughter opened twice, and smiling without laughter never opened (section 7).
9. **A soft chuckle is not always laughter to YAMNet.** In one of two cued "smile with a soft chuckle" steps the chuckle scored 0.022 at most, so the smile could not help and nothing opened. Lowering 0.10 needs EM-11's labelled audience recordings first.

**Not done:**

- A larger live sample: section 7 is one person in one room, with two cued "smile with a soft chuckle" steps.
- Delay over the network to the EC2 server. EM-26 asks for the delay on the PC's clock, which section 7 gives.
- Scream and gasp. EM-26 is about the laughter rule.

## 9. Reproduce

Scripted cases (no downloads):

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q server/tests/test_em26_trigger_check.py
.venv/bin/python server/tools/em26_trigger_check.py --runs 3
.venv/bin/python server/tools/em26_trigger_check.py --realtime
```

Real models need a Python 3.12 environment with `server/requirements-yamnet.txt`. For the face, add one with `deploy/models/requirements-face.txt` and `face_landmarker.task`. Get the clips from the ESC-50 repository (`meta/esc50.csv`, `audio/<file>.wav`), then convert each with `ffmpeg -i in.wav -ac 1 -ar 16000 -sample_fmt s16 out.wav`:

```bash
TFHUB_CACHE_DIR=/path/to/tfhub /path/to/yamnet-venv/bin/python server/tools/em26_trigger_check.py \
  --audio-model yamnet --runs 3 \
  --wav laughter=1-1791-A-26.wav --wav laughter=1-30039-A-26.wav --wav laughter=1-30043-A-26.wav \
  --wav laughter=1-33658-A-26.wav --wav laughter=1-36164-A-26.wav \
  --wav coughing=1-19111-A-24.wav --wav clapping=1-104089-A-22.wav --wav crying_baby=1-187207-A-20.wav \
  [--realtime] [--face-python /path/to/face-venv/bin/python --face-task /path/to/face_landmarker.task]
```

`--json` prints every case with its per-hop trace: laughter score, smile and evidence.

Live run: start a local server with both models, then the app with a log. The app asks for the microphone and camera, and with the camera on it saves clips next to `--clip-index`. Delete that folder afterwards.

```bash
cd server && TFHUB_CACHE_DIR=/path/to/tfhub /path/to/yamnet-venv/bin/python -m encore_server \
  --model yamnet --host 127.0.0.1 --port 8799 \
  --face-python /path/to/face-venv/bin/python --face-task /path/to/face_landmarker.task
cd client && ../.venv/bin/python -m encore_client --server ws://127.0.0.1:8799 --mic --no-player \
  --log /tmp/em26-live/client.jsonl --clip-index /tmp/em26-live/clips/clip_index.json
```

Trigger → OPEN is the `t_ms` of the OPEN line in the `--log` file minus the event's `capture.triggers_ms[0]`. Both are on the PC's monotonic clock. The per-window numbers in sections 6 and 7 came from a small wrapper around `ServerSession` that logs only scores, face results, chunk times and events. The step times in section 7 came from a cue script that waits for the first camera frame and then shows timed steps. Neither is in the repository.
