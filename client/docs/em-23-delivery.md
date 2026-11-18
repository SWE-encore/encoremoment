# EM-23 session and sample event delivery: procedure and reproduction records

Jira: [EM-23](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-23) · Contract: [contracts/README.md](../../contracts/README.md) · Related: EM-1 (contract), EM-3 (runtime environment), EM-20 (R11 reconnection recovery)

## Acceptance criteria and verification

| Acceptance criterion | Verification | Automated tests |
|---|---|---|
| Prepare the session and the player's title/time basis according to the EM-1 contract, then deliver sample results to the PC | Starting the session first sends the player's latest `playback_anchor` (title/time basis); results from the fake server appear in the Session panel table | `test_every_sample_reaches_its_expected_result`, `test_player_anchor_is_sent_on_connect` |
| Delivering the same sample result twice leaves one event in the UI | In `04_duplicate_delivery.json`, repeated deliveries vary the cursor/revision; nine messages are received, but the table retains one `evt-0004` row | `test_same_result_twice_leaves_one_event` |
| Indicate connection loss and record the delivery procedure and reproduction evidence | Disconnecting with `--drop-after` displays a red "Disconnected: reason" status and greys out the table. Every step is recorded in the `--log` file | `test_dropped_connection_is_reported`, `test_silent_server_counts_as_dropped`, `test_unreachable_server_is_reported` |

R11 recovery after reconnection (cursor replay and resending unsent messages) belongs to EM-20. This implementation only displays the disconnected state and retains undelivered messages in `unsent`.

## Delivery procedure

```
PC (encore_client.session)                       Server (currently fake_server)
  Click Start
  ── WebSocket connect ─────────────────────────▶ /v1/sessions/<session_id>/stream
  ◀──────────────────────────── Connection accepted (101)
  State: Connected
  ── playback_anchor (title/time basis) ────────▶
  ── control_update (sensor enable/disable) ────▶
  ◀──────────────────────────────── control_ack
  ◀──────────────────────────────── event_update (cursor 1, 2, 3 …)
  Contract receiver → one row per event_id; apply only newer revisions
  Update table
  (On connection loss) State: Disconnected + reason; grey out table
  Click End → close connection; state: Session ended
```

- Each message is one JSON object in one WebSocket text frame, as required by the contract.
- The result stream path `/v1/sessions/<session_id>/stream` is a proposal introduced by this work. The real server (EM-3) should use the same path, or `STREAM_PATH` in `session.py` must be updated if it changes.
- The client sends a ping every 5 seconds and treats 15 seconds without any server response as a dropped connection. This detects network loss as well as a terminated server process.

## Running the example

Follow Setup in [client/README.md](../README.md). The session code requires no additional packages.
The original command excerpts and logs below are preserved verbatim as reproduction evidence, including their Korean annotations.

```sh
cd client
tools/make_test_video.sh samples/test-movie.mp4 30

# 터미널 1: 가짜 서버
../.venv/bin/python -m encore_client.session.fake_server ../contracts/samples/04_duplicate_delivery.json

# 터미널 2: 플레이어 + Session 패널
../.venv/bin/python -m encore_client.session samples/test-movie.mp4 \
    --scenario ../contracts/samples/04_duplicate_delivery.json --log delivery-04.jsonl
```

Click **Start session** when the window opens. The Session panel on the right displays the connection state and event table.

To test connection loss, start the fake server with `--drop-after 2`, or press Ctrl+C in terminal 1 while it is running.

Add `--headless` to run without a window. Its output follows the format shown in the records below.

## Reproduction records

### 2026-11-17, headless (Linux, Python 3.13, no window)

**① Duplicate delivery: `04_duplicate_delivery.json`**

```
$ python -m encore_client.session.fake_server ../contracts/samples/04_duplicate_delivery.json --interval 0.05
$ python -m encore_client.session --scenario ../contracts/samples/04_duplicate_delivery.json --headless --log d04.jsonl
[session] connecting
[session] events: 0
[session] connected
[session] events: 1
           evt-0004  gasp     OPEN   rev 1
[session] events: 1
           evt-0004  gasp     FINAL  rev 3
[session] ignored {'duplicate_control': 1, 'duplicate_cursor': 1, 'duplicate_revision': 1, 'stale_revision': 1}  rejected {'wrong_session': 1}  received 9  sent 4
```

The server sent seven `event_update` messages, including a repeated cursor, out-of-order cursors, a replayed revision after reconnection, and a message from another session. The PC retained only `evt-0004`, ending at `FINAL rev 3`. The ignored/rejected counts match the fixture's `expected` result.

**② Connection loss: drop `01_single_event.json` after two messages**

```
$ python -m encore_client.session.fake_server ../contracts/samples/01_single_event.json --interval 0.3 --drop-after 2
[fake-server] dropping the connection after 2 messages (--drop-after)
[fake-server] connection closed (1006 dropped on purpose)

$ python -m encore_client.session --scenario ../contracts/samples/01_single_event.json --headless --log d01.jsonl
[session] connecting
[session] events: 0
[session] connected
[session] disconnected (connection lost)
[session] ignored {}  rejected {}  received 2  sent 3
```

Final lines of the delivery log (`d01.jsonl`):

```
{"t_ms": 371085, "what": "closed", "reason": "connection lost"}
{"t_ms": 371085, "what": "end", "unsent": 0}
```

**③ Automated tests**

```
$ cd client && python -m unittest tests.test_session_delivery -v
Ran 12 tests … OK        (5번 반복 실행, 모두 통과)
$ python -m unittest discover -s contracts/tests      # 계약 테스트도 그대로 통과
```

### GUI checks: to be completed by someone running the window

PySide6/GStreamer were unavailable in the environment where this record was created, so the GUI was not launched. Check the following in the Ubuntu 24.04 development environment and attach screenshots to the PR.

- [ ] Before Start: grey "Not started"; End disabled
- [ ] After Start: green "Connected"; fake server log contains `live anchor: '<영상 제목>'`
- [ ] After fixture 04 completes: one `evt-0004 gasp FINAL 3` row in the table
- [ ] After Ctrl+C on the fake server: red "Disconnected: connection lost. Results below may be out of date."; table greyed out
- [ ] After End: grey "Session ended"; fake server log contains `connection closed (1000 session ended)`
