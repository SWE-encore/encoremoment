# EM-20 (R11) session start, end and reconnection

Jira: [EM-20](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-20) · Contract: [contracts/README.md](../../contracts/README.md) · Server: [server/README.md](../../server/README.md) · Earlier: [EM-23 delivery](em-23-delivery.md)

## Acceptance criteria and verification

| Acceptance criterion | How it is met | Automated tests |
|---|---|---|
| At start, say what is sensed and processed on the server, and prepare the session with the player's title and time; the camera stays off until explicitly enabled | Start opens the result stream. The microphone-on control goes first, then the player's newest anchor (title, epoch, position, rate). With the player, Start stays disabled until a video is open. The sensing line says what is captured and where it is analysed. Nothing at Start turns the camera on: it is off on the PC and on the server until the user enables it. | `test_controls_before_open_go_first_and_seq_is_shared`, `test_player_anchor_is_sent_on_connect`, `test_live_cli_turns_microphone_on_at_start`, `test_start_sets_the_anchor_before_connecting`, `test_camera_is_off_by_default_and_nothing_is_opened`, `test_controls_are_synced_first_with_their_original_keys` (camera still off on the server after a reconnect) |
| On reconnect, sync control intents first, then retry with the original keys and the cursor, so no event is duplicated | After a drop the session waits 1, 2, 5, then every 10 s, and opens the stream again with `?after=<last applied cursor>`. On the new connection it first re-sends every sensor control it has made, unchanged (same `idempotency_key`, same `seq`): the server acknowledges the ones it has as `duplicate` and applies the ones that were lost in flight or made while disconnected. Then the newest anchor. Results the PC missed are replayed once; results it has are not sent again. | `client/tests/test_reconnect.py` (below) |
| On end, stop the devices, the transport and unsaved work, clean up temporary media and keep saved clips | End turns the camera off first (its control goes out before the stream closes), closes the microphone device, closes the stream normally so the server ends the session, and cancels any pending reconnect attempt. The saved clip index is not touched. | `test_session_end_stops_the_camera`, `test_end_closes_the_device`, `test_end_is_not_a_disconnect`, `test_end_while_waiting_to_reconnect_stops_the_attempts` |

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

Run:

```sh
python -m pytest -q client/tests/test_reconnect.py
```

## Not covered here

- **Deletion intents.** Clip deletion (R7) does not exist in the main app yet, and the contract has no deletion message. When it is added, a deletion request should be kept and re-sent like a sensor control: in order, unchanged, with its key, before anything else on a reconnect.
- **Events still open at End.** The server finalizes them when the stream closes, but the PC has closed the stream by then, so their rows stay `OPEN` or `UPDATE` on the PC. A graceful End (microphone off, wait for the FINAL results, then close) is a follow-up.
- **End while disconnected.** The server never hears that the session ended. It keeps the session and its open events until it restarts. This needs the session API or an expiry on the server.
- **Temporary media.** The main app keeps no temporary media yet: clip buffering (EM-2) is not in it. The tab demo's segment storage is EM-19.
- **Recovery after a server restart.** Detected and reported, not recovered: sessions and results are in server memory until EM-29.
- **Anchors during a drop.** Only the newest anchor is sent on reconnect. No audio is analysed during the drop, so the anchors in between would map nothing new.
