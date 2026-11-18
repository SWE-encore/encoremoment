# EM-23 세션과 샘플 이벤트 전달: 절차와 재현 기록

Jira: [EM-23](https://hanyang-team-ldzgset2.atlassian.net/browse/EM-23) · 계약: [contracts/README.md](../../contracts/README.md) · 관련: EM-1(계약), EM-3(실행 환경), EM-20(R11 재연결 복구)

## 수용 조건과 확인 방법

| 수용 조건 | 어떻게 확인하나 | 자동 테스트 |
|---|---|---|
| EM-1 계약에 따라 세션, 플레이어 제목·시점 기준을 준비하고 PC에 샘플 결과를 전달한다 | 세션을 시작하면 플레이어의 최신 `playback_anchor`(제목·시점)를 먼저 보내고, 가짜 서버가 보낸 샘플 결과가 Session 패널 표에 뜬다 | `test_every_sample_reaches_its_expected_result`, `test_player_anchor_is_sent_on_connect` |
| 같은 샘플 결과를 두 번 보내도 UI에 이벤트가 하나만 남는다 | `04_duplicate_delivery.json`: 같은 이벤트가 cursor·revision을 바꿔 가며 9번 오지만 표에는 `evt-0004` 한 줄만 있다 | `test_same_result_twice_leaves_one_event` |
| 연결 단절을 표시하고, 전달 절차와 재현 기록을 남긴다 | `--drop-after`로 연결을 끊으면 빨간 "Disconnected: 이유"가 뜨고 표가 흐려진다. 모든 단계는 `--log` 파일에 남는다 | `test_dropped_connection_is_reported`, `test_silent_server_counts_as_dropped`, `test_unreachable_server_is_reported` |

R11의 재연결 후 복구(cursor 재전송, 보내지 못한 메시지 재전송)는 EM-20에서 한다. 여기서는 끊긴 상태를 보여 주고, 보내지 못한 메시지를 `unsent`에 보관하는 데까지만 한다.

## 전달 절차

```
PC (encore_client.session)                       서버 (지금은 fake_server)
  Start 클릭
  ── WebSocket 연결 ─────────────────────────────▶  /v1/sessions/<session_id>/stream
  ◀──────────────────────────────── 연결 수락 (101)
  상태: Connected
  ── playback_anchor (제목·시점 기준) ───────────▶
  ── control_update (센서 켬/끔) ─────────────────▶
  ◀──────────────────────────────── control_ack
  ◀──────────────────────────────── event_update (cursor 1, 2, 3 …)
  계약 수신기로 처리 → 같은 event_id는 한 줄, 더 새로운 revision만 반영
  표 갱신
  (연결이 끊기면) 상태: Disconnected + 이유, 표 흐리게
  End 클릭 → 연결 닫기, 상태: Session ended
```

- 메시지 하나는 WebSocket 텍스트 프레임 하나에 JSON 객체 하나로 보낸다 (계약 규칙).
- 결과 스트림 주소 `/v1/sessions/<session_id>/stream`은 이 작업에서 정한 제안이다. 실제 서버(EM-3)를 만들 때 같은 경로를 쓰거나, 바뀌면 `session.py`의 `STREAM_PATH`를 고친다.
- 5초마다 ping을 보내고, 15초 동안 서버에서 아무것도 오지 않으면 끊긴 것으로 본다. 그래서 프로세스가 죽은 경우뿐 아니라 네트워크가 끊긴 경우도 잡는다.

## 실행 방법

준비는 [client/README.md](../README.md)의 Setup과 같다. 세션 코드에는 새로 설치할 것이 없다.

```sh
cd client
tools/make_test_video.sh samples/test-movie.mp4 30

# 터미널 1: 가짜 서버
../.venv/bin/python -m encore_client.session.fake_server ../contracts/samples/04_duplicate_delivery.json

# 터미널 2: 플레이어 + Session 패널
../.venv/bin/python -m encore_client.session samples/test-movie.mp4 \
    --scenario ../contracts/samples/04_duplicate_delivery.json --log delivery-04.jsonl
```

창이 뜨면 **Start session**을 누른다. 오른쪽 Session 패널에 상태와 이벤트 표가 나온다.

연결 끊김을 보려면 가짜 서버를 `--drop-after 2`로 띄우거나, 실행 중에 터미널 1에서 Ctrl+C를 누른다.

창 없이 확인할 때는 `--headless`를 붙인다. 출력은 아래 기록과 같은 형태로 나온다.

## 재현 기록

### 2026-11-17, headless (Linux, Python 3.13, 창 없이)

**① 중복 전달: `04_duplicate_delivery.json`**

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

서버는 `event_update`를 7번 보냈다(같은 cursor 두 번, 순서 바뀐 cursor, 재연결 뒤 같은 revision 재전송, 다른 세션 메시지 포함). PC에 남은 이벤트는 `evt-0004` 하나이고, 마지막 상태는 `FINAL rev 3`이다. 무시·거부 횟수는 샘플의 `expected`와 같다.

**② 연결 끊김: `01_single_event.json`을 메시지 2개 뒤에 끊기**

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

전달 로그(`d01.jsonl`)의 마지막 줄:

```
{"t_ms": 371085, "what": "closed", "reason": "connection lost"}
{"t_ms": 371085, "what": "end", "unsent": 0}
```

**③ 자동 테스트**

```
$ cd client && python -m unittest tests.test_session_delivery -v
Ran 12 tests … OK        (5번 반복 실행, 모두 통과)
$ python -m unittest discover -s contracts/tests      # 계약 테스트도 그대로 통과
```

### 창(GUI) 확인: 직접 실행한 사람이 채운다

이 기록을 만든 환경에는 PySide6/GStreamer가 없어서 창은 띄워 보지 못했다. Ubuntu 24.04 개발 환경에서 아래를 확인하고 스크린샷을 PR에 붙인다.

- [ ] Start 전: 회색 "Not started", End 비활성
- [ ] Start 후: 초록 "Connected", 가짜 서버 로그에 `live anchor: '<영상 제목>'`
- [ ] 04 샘플이 끝난 뒤: 표에 `evt-0004 gasp FINAL 3` 한 줄
- [ ] 가짜 서버 Ctrl+C: 빨간 "Disconnected: connection lost. Results below may be out of date.", 표가 흐려짐
- [ ] End: 회색 "Session ended", 가짜 서버 로그에 `connection closed (1000 session ended)`
