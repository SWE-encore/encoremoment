# EM-20 (R11) session start, end and reconnection

Jira: [EM-20](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-20) · Contract: [contracts/README.md](../../contracts/README.md) · Server: [server/README.md](../../server/README.md) · Earlier: [EM-23 delivery](em-23-delivery.md)

## Acceptance criteria and verification

| Acceptance criterion | How it is met | Automated tests |
|---|---|---|
| At start, say what is sensed and processed on the server, and prepare the session with the player's title and time; the camera stays off until explicitly enabled | Start opens the result stream. The microphone-on control goes first, then the player's newest anchor (title, epoch, position, rate). With the player, Start stays disabled until a video is open. The sensing line says what is captured and where it is analysed. Nothing at Start turns the camera on: it is off on the PC and on the server until the user enables it. | `test_controls_before_open_go_first_and_seq_is_shared`, `test_player_anchor_is_sent_on_connect`, `test_live_cli_turns_microphone_on_at_start`, `test_start_sets_the_anchor_before_connecting`, `test_camera_is_off_by_default_and_nothing_is_opened`, `test_controls_are_synced_first_with_their_original_keys` (camera still off on the server after a reconnect) |
| On reconnect, sync control intents first, then retry with the original keys and the cursor, so no event is duplicated | After a drop the session waits 1, 2, 5, then every 10 s, and opens the stream again with `?after=<last applied cursor>`. On the new connection it first re-sends every sensor control it has made, unchanged (same `idempotency_key`, same `seq`): the server acknowledges the ones it has as `duplicate` and applies the ones that were lost in flight or made while disconnected. Then the newest anchor. Results the PC missed are replayed once; results it has are not sent again. | `client/tests/test_reconnect.py` (below) |
| On end, stop the devices, the transport and unsaved work, clean up temporary media and keep saved clips | End turns the camera off and closes the microphone device first, then tells the server the microphone is off, so the server closes every open reaction and sends its FINAL result. The stream closes once every control is acknowledged and nothing is open, or after 3 s, and the session shows "Ending…" meanwhile. A pending reconnect attempt is cancelled. Turning the camera off removes the desktop recorder's capture directory (unsaved video and audience audio) and marks its pending clips unavailable. The saved clip index is not touched. | `client/tests/test_session_end.py` (below), `test_session_end_stops_the_camera`, `test_end_closes_the_device`, `test_end_is_not_a_disconnect`, `test_end_while_waiting_to_reconnect_stops_the_attempts`, `test_unsaved_capture_is_gone_between_sessions_and_saved_clips_stay` |
| Repeat Start/End in one run | After End, **Start session** is enabled again in a live run. It creates a new `Session` with a new ID (`new_session_id()`) and hands it to everything that held the old one: the session panel, Highlights, a new camera control (generation 0) and camera panel, a new desktop recorder (the old one is shut down), the microphone switch, the live or WAV microphone, and the player's anchor callback. The player's newest anchor goes to the new session, so Start does not wait for a play or seek. The old session's stream and log file are closed and it is never polled again. Between sessions the microphone and camera are off; Start turns the microphone on in generation 1. A demo or scenario run plays one session. Headless, `--sessions N` repeats Start/End N times. | `client/tests/test_session_restart.py` (below) |

## Reconnection tests

`client/tests/test_reconnect.py` runs the real PC `Session` against the real session server, through a TCP relay that the test can cut (no close frame either way, as in a network failure), stop forwarding in one direction (data lost in flight), or take down (server unreachable).

| Case | What is checked |
|---|---|
| Drop after one finished laugh, then a second laugh | Reconnects with `after` = the last applied cursor, the server keeps the same session, nothing is received twice (no ignored cursor, revision or control on either side), audio captured while disconnected is dropped and the server counts it as a gap, the second laugh arrives as `evt-0002`. The status line says "Reconnecting in …" during the drop and "reconnected after 1 drop(s)" afterwards. |
| Results lost in flight | The server's OPEN/FINAL never reach the PC. After the drop, the PC reconnects with `after=0` and applies every result exactly once. |
| Controls lost in flight or made while disconnected | Microphone off is sent but lost; microphone on is made while disconnected. On reconnect the three controls go first, unchanged and in order, before the anchor. Acks: `duplicate`, `applied`, `applied`. PC and server agree on microphone generation 3, and the camera is still off. |
| Server restarted (session lost) | A control the server had applied comes back `applied`, so the server no longer has this session. The PC stays disconnected with that reason and stops retrying, instead of showing the session as recovered. |
| Server unreachable | Attempts back off 100, 200, 400, 400 ms (test delays) and the session recovers when the server is back. |
| Server unreachable at Start | Retried the same way; when it connects, the status is "Connected", not "reconnected after a drop". |
| End while waiting to reconnect | No further attempt is made. |

Five deliberate faults were each caught before this was merged: reconnecting without the cursor, re-sending only unacknowledged controls, re-sending controls with a new `seq`, sending the anchor before the controls, and not checking for a lost session. The file passed 10 of 10 runs on one CPU core shared with three busy loops.

The headless app was also run end to end with `--mic-wav` against the real server through the relay, with the connection cut after 6 s. Result: one reconnect (`after=2`), nothing replayed twice, the second laugh found, 3 audio chunks dropped and 3 counted as a gap by the server, exit code 0.

## End tests

`client/tests/test_session_end.py`, against the real server through the same relay (`client/tests/live_harness.py`):

| Case | What is checked |
|---|---|
| End during a laugh | The event is `OPEN` when End is pressed. Microphone off goes out, the FINAL comes back, and only then does the stream close. The PC's event equals the server's, and it is in the FINAL list Highlights shows. The server ends the session with nothing left to send. |
| Nothing open | End waits only for the microphone-off acknowledgement (well under 1 s). |
| Microphone already off | No second off control. |
| Server does not answer | End gives up after the grace period (300 ms in the test) and the status says the server did not confirm. |
| Connection drops while ending | The session ends at once; it does not reconnect. |
| Disconnected at End | Ends at once, no reconnect. |
| Scenario run | Ends at once and sends no control of its own. |
| End button (Qt, offscreen) | The microphone device closes before the off control is sent; the panel shows "Ending…" and End is disabled until the server confirms. |

Five deliberate faults were each caught: End without turning the microphone off, ending at once, waiting only for acknowledgements and not for open events, treating a drop while ending as a reconnectable drop, and the End button not closing the microphone device first. Both test files passed 15 of 15 runs on one CPU core shared with three busy loops.

The headless app was run against the real server with a laugh still in progress when the run ended (the server's idle close disabled). The output went from `evt-0001 OPEN` to `evt-0001 FINAL`, then the server logged "session ended". Exit code 0.

If the live server disconnects during End or the three-second wait expires before all controls and reactions are confirmed, the headless client warns on standard error that final results may be incomplete and exits with code 1.

## Repeat Start/End tests

`client/tests/test_session_restart.py`, offscreen Qt. The first two cases use the real server through the relay, the window and headless cases the real server directly; the others use a scripted stream so that a late frame from the old stream can be checked.

| Case | What is checked |
|---|---|
| Start → End → Start → End, a laugh in each | Two sessions with different IDs. Each has exactly the events its own server session sent (`evt-0001` FINAL in each, with its own capture times), its microphone controls start at generation 1 and `seq` 0, and its audio starts at generation 1, chunk 0. The second session leaves the first one's events unchanged. The panel and Highlights show only the current session. |
| Microphone and camera between sessions | The first session ends with microphone generation 4 and the camera on. After End both devices are off. The second session's camera control is new and off at generation 0, its only control is `ctl-microphone-1`, and the server agrees (camera off, generation 0; microphone generation 1). Enabling the camera again opens the new recorder in generation 1. |
| Nothing carries over | A frame the old stream still delivers never reaches either session. The new session has an empty view, cursor 1 and its own `seq`. The live microphone's buffered PCM, recording and AEC status start again. The old recorder is shut down and audio goes to the new one. The old log file is closed, and both sessions' `start` entries are in the one `--log` file. |
| Real desktop recorder | End removes the capture directory. The next session gets a new recorder and directory. A saved clip and its index row are unchanged after both sessions. |
| The app's window (`--no-player --mic-wav`, through `main()`) | Start, End, Start, End with the buttons. Each session gets the whole WAV file from chunk 0 of generation 1 and its own microphone-clock anchor; the WAV microphone and the real desktop recorder follow the new session. Set `ENCORE_RESTART_SCREENSHOTS=<dir>` to save the window after the first End and during the second session. |
| `--headless --sessions 2` | Two live sessions with new IDs against the real server, each with microphone generations 1 and 2 and no audio gap, and both in the one log file. |
| Anchor handover | The player's newest anchor goes to the new session; anchors after that go only to it. |

Ten deliberate faults were each caught: Start disabled after End; the microphone switch, Highlights, the recorder or the camera control not replaced; the live microphone keeping its recording; the WAV file not played again; the anchor not handed to the new session; the old log left open; and the headless loop reusing the ended session.

Run:

```sh
python -m pytest -q client/tests/test_reconnect.py client/tests/test_session_end.py client/tests/test_session_restart.py
```

## Not covered here

- **Deletion intents.** Clip deletion (R7) does not exist in the main app yet, and the contract has no deletion message. When it is added, a deletion request should be kept and re-sent like a sensor control: in order, unchanged, with its key, before anything else on a reconnect.
- **End while disconnected.** The session ends at once on the PC, but the server never hears it. It keeps the session and its open events until it restarts. This needs the session API or an expiry on the server.
- **Closing the window without End.** Shutdown closes the stream at once without waiting; the server still closes open reactions, but the PC does not receive them.
- **Remote cleanup.** End removes the desktop recorder's capture directory on the PC. The contract has no End request (the PC sends anchors, controls and audio), so there is no persisted End retry or server cleanup acknowledgement as R11 describes; see "End while disconnected".
- **Restart during cleanup.** A new recorder reaps capture directories that no running recorder holds (`clips/storage.recover`, tested in `test_interrupted_capture_cleanup_and_active_owner`), but quitting the app in the middle of End and starting it again has no end-to-end test.
- **Recovery after a server restart.** Detected and reported, not recovered: sessions and results are in server memory until EM-29.
- **Anchors during a drop.** Only the newest anchor is sent on reconnect. No audio is analysed during the drop, so the anchors in between would map nothing new.
