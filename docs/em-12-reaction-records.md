# EM-12 (R3) reaction records: verification

Jira: [EM-12](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-12) · Contract: [contracts/README.md](../contracts/README.md) · Server: [server/README.md](../server/README.md) · Related: EM-11 (R2 recognition), EM-20 (R11 reconnection), EM-29 (PostgreSQL archive)

## Acceptance criteria and verification

| Acceptance criterion | How it is met | Automated tests |
|---|---|---|
| Record the reaction type, observed intensity, title, content timecode and model/config version, with confidence and intensity kept apart | Every `event_update` carries `reaction.type`, `reaction.confidence` (model confidence for the class), `intensity` (`audio` from loudness against the room baseline, `visual` from the strongest face, `combined`), `playback` (`title`, `content_id`, `epoch`, start/end timecode) and `source` (`model` such as `google/yamnet/1`, `config` such as `detector-v1` or the fusion thresholds, AEC status). The server keeps each session's results in order and replays them from a cursor; the PC keeps one row per `event_id`. Durable storage of finished events is EM-29 ([PR #15](https://github.com/SWE-encore/encoremoment/pull/15), open). | `test_a_laugh_in_the_audio_comes_back_as_a_mapped_event`, `test_fusion_and_replay` (visual intensity 0.20 with confidence 0.10) |
| Retransmitting the same input or result does not create a duplicate event | A repeated audio `chunk` is not analysed twice, a retried control with the same idempotency key is acknowledged once, a reconnecting PC is sent only results after its cursor, and the PC receiver keeps one event per `event_id` and ignores equal or lower revisions. | `test_retried_and_missing_chunks`, `test_control_acks_applied_duplicate_rejected`, `test_results_replay_from_a_cursor`, `test_a_reconnecting_pc_is_sent_the_results_after_its_cursor`, `test_same_result_twice_leaves_one_event` |
| Timecodes come from capture time through pause, seek, speed change, title change and late reception | The PC's player sends an anchor (title, epoch, position, rate, play state, capture time) whenever playback changes. The server maps each event's capture start and end through the anchor in effect at those times. An end in another epoch (after a seek or another title) is `null`, not guessed. The PC shows the event's own mapping, never the player's position when the result arrives. | `server/tests/test_playback_timecode.py` (below), `test_events_map_to_content_timecode_through_anchors`, `test_a_seek_during_the_event_leaves_the_end_unmapped`, `test_paused_anchor_pins_the_timecode`, `test_timecode_while_playing_paused_and_at_other_rates`, `test_seek_and_new_content_change_epoch_pause_and_rate_do_not` |

## Playback changes during a reaction

`server/tests/test_playback_timecode.py` runs the real PC `Session` and the player's `AnchorTracker` against the real session server over a local WebSocket. The film is at 60.0 s at capture time 9.5 s. The viewer laughs from 12 s to 14 s of capture time, and at 13 s, in the middle of the laugh, playback changes:

| Change at 13 s | Start timecode | End timecode |
|---|---|---|
| None | 60 000 + (start − 9 500) | 60 000 + (end − 9 500) |
| Pause | same | 63 500 (where it paused) |
| 2× speed | same | 63 500 + 2 × (end − 13 000) |
| Seek to 10:00 | same | `null` (another epoch) |
| Another title | same, title stays "Film A" | `null` (another epoch) |
| Late reception: another title and a seek before the PC reads the result | same, the PC's row shows "Film A" | 60 000 + (end − 9 500) |

Each case also checks that the server's mapping equals what the PC's own anchor history says the player was showing at the event's start and end. Two deliberate faults in the server's mapping were each caught before this was merged: using the newest anchor instead of the one in effect at capture time (5 of 6 cases fail), and ignoring the playback rate (the 2× case fails).

Run:

```sh
python -m pytest -q server/tests/test_playback_timecode.py
```

## Not covered here

- **Recognition quality** of the three reaction types: EM-11 and EM-26.
- **Durable storage** across server restarts: EM-29.
- **A real GStreamer seek.** The anchor follows when the seek lands, so a reaction in the few hundred milliseconds between the seek request and the new anchor is mapped on the old timeline. Measuring that window needs the Ubuntu player.
- **An event that starts before the first anchor** (the microphone is on before playback starts) stays `pending` by design: content time before playback started is not guessed.
