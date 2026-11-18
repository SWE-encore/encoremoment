# Event and control message contract (`encore.v1`)

Jira: [EM-1](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-1) · Spec: [Submission B §IV](../documents/submission-b/sections/specifications.tex) · Status: **draft, needs review from PC, AI and server owners**

> **Summary** — This contract defines reaction event and sensor control messages exchanged between the PC and server. The schema is in `schema/`, six executable fixtures are in `samples/`, and the reference implementation of the PC receiver rules is in `reference/`. EM-23 (session/event delivery), EM-2 (clip storage), and EM-24 (UI) can use these fixtures for development and testing. Please review the "Decisions that need review" section below.

## What is in this folder

| Path | What it is | Who uses it |
|---|---|---|
| `schema/messages.schema.json` | JSON Schema (draft 2020-12) for every message | Everyone: validate before sending and after receiving |
| `samples/*.json` | Six scenarios: a message sequence plus the expected result | Server: emit these. PC/UI: feed these in instead of a live server |
| `reference/encore_contract.py` | Stdlib-only PC receiver: dedup, ordering, generations, R10 clip windows | PC client (EM-23), clip pipeline (EM-2) |
| `tests/test_contract.py` | Checks samples against the schema and the reference receiver | CI |

Run the checks from the repository root (Python 3.11+):

```sh
python -m pip install -r contracts/requirements-dev.txt
python -m unittest discover -s contracts/tests -v
```

## Messages

Every message is one JSON object in one WSS text frame, with `"schema": "encore.v1"`, a `kind` and a `session_id`. Unknown fields are rejected, so adding a field means changing the schema here first.

| `kind` | Direction | Purpose | Requirement |
|---|---|---|---|
| `playback_anchor` | PC → server | Title, epoch, position, rate and play state at a capture time, used to map capture time to content timecode | R3, R11 |
| `control_update` | PC → server | Camera, microphone or AEC turned on or off, with its new generation | R5, R8, R12 |
| `control_ack` | server → PC | Server applied, de-duplicated or rejected a control update | R5, R8 |
| `event_update` | server → PC | One committed change to one reaction event | R2, R3, R9, R10, R11 |

Media packets (audio and video frames) are not part of this contract yet. They reuse the same `session_id`, generation and `capture_ms` fields when they are added.

### `event_update` fields

| Field | Meaning |
|---|---|
| `cursor` | Per-session result cursor. Starts at 1, no gaps. The PC applies changes in cursor order. |
| `event.event_id` | Stable ID for one reaction. The same across updates, retries and reconnects. |
| `event.revision` | Increments with every change to the event. `OPEN` is always revision 1. |
| `event.phase` | `OPEN` (first threshold crossing) → `UPDATE`* → `FINAL` (interval closed, `end_ms` set). |
| `event.reaction.type` | `laughter`, `scream` or `gasp`. Never changes for an event. |
| `event.reaction.confidence` | Model confidence for that class, 0–1. **Not** intensity. |
| `event.intensity` | Observable strength, 0–1: `audio` (RMS against room baseline), `visual` (strongest valid face), `combined = max(audio, visual)`. `null` = modality unavailable. |
| `event.capture.triggers_ms` | Every time the threshold was crossed, ascending. `[0]` is the trigger t0; the rest are re-fires. Revisions may only append. |
| `event.capture.start_ms` / `end_ms` | Reaction interval. `end_ms` is `null` until `FINAL`. |
| `event.generations` | Microphone and camera generation in effect at capture. `camera: null` = camera was off. |
| `event.playback` | `{"status": "mapped", epoch, content_id, title, timecode_start_ms, timecode_end_ms}` or `{"status": "pending"}` while no anchor exists. Never guessed. |
| `event.source` | Model ID, config version and AEC status (`active`, `warming_up`, `reference_unavailable`, `uncancelled`, `off`). |

All `*_ms` capture times are milliseconds on the **PC monotonic clock**. Network arrival time and wall-clock time are never used as capture time. Content timecode is `position_ms + rate × (t − anchor.capture_ms)` within one playback epoch.

## Receiver rules (PC side)

These are implemented in `reference/encore_contract.py` and exercised by the samples.

1. **Cursor order.** A cursor below the next expected one is a duplicate and is ignored. A cursor above it is held until the gap is filled. Reconnect resumes after the last applied cursor.
2. **One event per `event_id`.** Higher revision replaces the stored one. Equal revision is a duplicate. Lower revision is stale. Both are ignored.
3. **`FINAL` is final.** A higher revision after `FINAL` is a protocol error.
4. **Immutable fields.** `reaction.type`, `generations` and `capture.start_ms` never change. `triggers_ms` is only appended to.
5. **Generations.** Each sensor generation increments on every enable and every disable. A control with a generation that is not newer is rejected. A retried control with the same `idempotency_key` is ignored.
6. **Microphone off (R8).** A **new** event whose microphone generation is not the current enabled one is refused, so late results are never shown as new detections. An event that was already open may still be updated and closed.
7. **Camera off (R5, R10).** An event with `camera: null` is kept, but it has no clip. Footage from before the camera was enabled is never assumed: the missing part of the window is reported. If the camera stops before a clip's window ends, that clip is cancelled. Disabling the camera clears unsaved video.
8. **Clip windows (R10).** From `triggers_ms`: the first trigger gives `[t0 − 15 s, t0 + 10 s]`. A re-fire at or before the current end extends the end by 5 s, up to 60 s in total. A re-fire after the end starts a new clip. A re-fire while the clip is already 60 s long starts a new clip linked to the previous one.

`expected_status` in the samples is the status the clip should reach once EM-2 cuts and verifies the file: `pending` (event not `FINAL`), `ready`, `partial` or `unavailable`. The UI must not show a play link until the real file is finalized and checked.

## Samples

| File | Case | EM-1 checklist |
|---|---|---|
| `01_single_event.json` | Laughter, OPEN → UPDATE → FINAL, one 25 s clip | single event |
| `02_repeated_firing.json` | Scream re-fires extend the clip by 5 s each; a later re-fire starts a new clip | repeated firing |
| `03_refire_cap.json` | Continuous laughter hits the 60 s cap, then a linked clip | repeated firing |
| `04_duplicate_delivery.json` | Same cursor twice, out-of-order cursors, replayed revisions after reconnect, retried control, wrong session | duplicate delivery |
| `05_camera_off.json` | Event with camera off; event 3 s after enabling (partial clip); camera stopped mid-clip | camera off |
| `06_microphone_off.json` | Microphone disabled while an event is open; a late new detection is refused | extra (R8) |

Each sample has `messages` (`{"dir": "pc->server" | "server->pc", "msg": {...}}` in the order the PC sees them) and `expected` (the receiver's summary after replay). To use one as fake server output, send the `server->pc` messages in order.

## Decisions that need review

These choices are not spelled out in the specification. They are in the contract so work can start, and each one can change after review.

1. **Linked clip shape (R10, Chul).** When a re-fire arrives and the clip is already 60 s long, the new linked clip is a normal `[t − 15 s, t + 10 s]` window, so it overlaps the previous clip. The alternative is to start exactly where the previous clip ended.
2. **Camera stop cancels the whole clip (R5, Chul and Soyeon).** The spec says disabling the camera cancels clip jobs and clears unsaved video, so a clip whose window had not ended is `unavailable` and not trimmed to `partial`.
3. **Open events may close after microphone off (R8, Yujeong).** Only new events from the old microphone generation are refused.
4. **Normalized 0–1 scores (R3, Yujeong).** Confidence and all intensities are normalized to 0–1 by the server, so the UI can rank and display them without knowing the model.
5. **Model naming.** `source.model` is a free string. The team chose YAMNet over PANNs on 2026-11-05 in Slack, but Submission B §III–IV still names PANNs. Nothing in this contract depends on which model is used.

## Changing the contract

Change the schema, a sample and the reference receiver in the same pull request, and keep `python -m unittest discover -s contracts/tests` green. A change that breaks existing samples needs a new `schema` value (`encore.v2`).
