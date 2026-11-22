# EM-20 (R11) session start, end and reconnection

Jira: [EM-20](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-20) · Contract: [contracts/README.md](../../contracts/README.md) · Server: [server/README.md](../../server/README.md) · Earlier: [EM-23 delivery](em-23-delivery.md)

## Acceptance criteria and verification

| Acceptance criterion | How it is met | Automated tests |
|---|---|---|
| At start, say what is sensed and processed on the server, and prepare the session with the player's title and time; the camera stays off until explicitly enabled | Start opens the result stream. The microphone-on control goes first, then the player's newest anchor (title, epoch, position, rate). With the player, Start stays disabled until a video is open. The sensing line says what is captured and where it is analysed. Nothing at Start turns the camera on: it is off on the PC and on the server until the user enables it. | `test_controls_before_open_go_first_and_seq_is_shared`, `test_player_anchor_is_sent_on_connect`, `test_live_cli_turns_microphone_on_at_start`, `test_start_sets_the_anchor_before_connecting`, `test_camera_is_off_by_default_and_nothing_is_opened`, `test_controls_are_synced_first_with_their_original_keys` (camera still off on the server after a reconnect) |
| On reconnect, sync control intents first, then retry with the original keys and the cursor, so no event is duplicated | After a drop the session waits 1, 2, 5, then every 10 s, and opens the stream again with `?after=<last applied cursor>`. On the new connection it first re-sends every sensor control it has made, unchanged (same `idempotency_key`, same `seq`): the server acknowledges the ones it has as `duplicate` and applies the ones that were lost in flight or made while disconnected. Then the newest anchor. Results the PC missed are replayed once; results it has are not sent again. | `client/tests/test_reconnect.py` (below) |
| On end, stop the devices, the transport and unsaved work, clean up temporary media and keep saved clips | End turns the camera off and closes the microphone device first, then tells the server the microphone is off, so the server closes every open reaction and sends its FINAL result. The stream closes once every control is acknowledged and nothing is open, or after 3 s, and the session shows "Ending…" meanwhile. A pending reconnect attempt is cancelled. The saved clip index is not touched. | `client/tests/test_session_end.py` (below), `test_session_end_stops_the_camera`, `test_end_closes_the_device`, `test_end_is_not_a_disconnect`, `test_end_while_waiting_to_reconnect_stops_the_attempts` |

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

Run:

```sh
python -m pytest -q client/tests/test_reconnect.py client/tests/test_session_end.py
```

## Not covered here

- **Deletion intents.** Clip deletion (R7) does not exist in the main app yet, and the contract has no deletion message. When it is added, a deletion request should be kept and re-sent like a sensor control: in order, unchanged, with its key, before anything else on a reconnect.
- **End while disconnected.** The session ends at once on the PC, but the server never hears it. It keeps the session and its open events until it restarts. This needs the session API or an expiry on the server.
- **Closing the window without End.** Shutdown closes the stream at once without waiting; the server still closes open reactions, but the PC does not receive them.
- **Temporary media.** The main app keeps no temporary media yet: clip buffering (EM-2) is not in it. The tab demo's segment storage is EM-19.
- **Recovery after a server restart.** Detected and reported, not recovered: sessions and results are in server memory until EM-29.
- **Anchors during a drop.** Only the newest anchor is sent on reconnect. No audio is analysed during the drop, so the anchors in between would map nothing new.
