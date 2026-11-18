# Encore Moment

CSE406 Software Engineering · Group Encore · Hanyang University

PC의 제어 가능한 영상 플레이어, 관객 반응 이벤트 수신, 로컬 클립 리뷰를 한 앱에서 개발하는 저장소입니다.
**현재는 계약 샘플 기반 통합 개발 버전입니다. 실제 관객 촬영·마이크 수집·AI 추론·클립 생성은 아직 연결되지 않았습니다.**
기준 설계는 Submission B의 PC-first 구조이며, Submission A는 이전 smart-TV 범위의 기록입니다.

## 빠른 실행

명령은 별도 표시가 없으면 저장소 루트에서 실행합니다. 전체 플레이어의 기준 환경은 **Ubuntu 24.04 / Python 3.12 / PySide6 6.8.3 / GStreamer 1.24**입니다.

```bash
sudo apt update
sudo apt install python3-venv python3-gi gir1.2-gstreamer-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav ffmpeg libegl1 libopengl0 libxcb-cursor0
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
cd client
../.venv/bin/python -m encore_client --demo
```

창에서 **Open video**로 원본 영상을 열고 **Start session**을 누릅니다.
`--demo`는 로컬 가짜 서버를 자동 시작하며 중복 전달 샘플을 재생합니다.
이벤트 표에는 `evt-0004` 하나가 남고 FINAL 상태가 되면 Highlights에도 나타납니다.
Saved clips 탭은 별도 샘플 인덱스를 읽습니다. 샘플 클립은 실시간 이벤트의 촬영 결과가 아닙니다.
End 후에는 현재 기록을 보존합니다. 새 세션은 앱을 다시 실행합니다(같은 창에서 새 세션 생성은 후속 작업).

테스트 영상을 만들고 함께 열려면 저장소 루트에서:

```bash
bash client/tools/make_test_video.sh client/samples/test-movie.mp4 30
cd client
../.venv/bin/python -m encore_client samples/test-movie.mp4 --demo
```

Windows에서는 계약/화면 개발 모드를 사용할 수 있습니다. GStreamer 전체 플레이어 검증은 Ubuntu에서 합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
cd client
..\.venv\Scripts\python -m encore_client --demo --no-player
```

화면 없이 계약 통신만 확인하려면 `client/`에서 `python -m encore_client --demo --headless`를 실행합니다(가상환경의 python 사용).
샘플 변경: `--scenario ../contracts/samples/05_camera_off.json`.
`--scenario`만 쓰면 외부 가짜 서버에 연결하고, `--demo`를 함께 쓰면 내장 가짜 서버를 시작합니다.
실제 서버 연결은 `--server wss://호스트`를 사용합니다. 현재 스트림 경로는 `/v1/sessions/<session_id>/stream` 제안이며 서버 구현과 합의가 필요합니다.
일반 모드는 영상의 첫 playback anchor가 준비돼야 Start가 활성화됩니다.
`--clip-index /경로/index.json`으로 저장된 클립 인덱스를 지정할 수 있습니다.
일반 모드 기본값은 `artifacts/clip_index.json`, 데모는 `samples/clip_index.json`입니다.

## 폴더와 역할

```text
contracts/                  PC·서버 공통 메시지 계약, 샘플, 참조 수신기, 테스트
client/
  encore_client/
    __main__.py             공통 실행 진입점, 데모 서버 생명주기
    player/                 GStreamer 원본 영상 재생, 재생 위치·epoch·anchor
    session/                WebSocket, 세션 상태, 수신·중복 제거, fake_server
    clips/                  저장된 클립 인덱스 모델, 목록·로컬 미리보기
    sensing/                카메라 상태 모델(실제 장치 어댑터는 후속 작업)
    ui/                     통합 화면, Highlights, 공통 표시 함수
  tests/                    단위·통신·UI·실제 미디어 테스트
  tools/                    테스트 영상 생성
  docs/                     기존 EM-23 전달 검증 기록
samples/                    클립 UI용 합성 영상과 예제 인덱스
scripts/                    샘플 재생성 도구
submission-a/, submission-b/ 원본 제출 문서(설계/요구사항 근거)
docs/                       현재 구조·통합 결정
.github/                    PR 템플릿과 Ubuntu CI
artifacts/                  로컬 실행 결과(버전 관리 제외)
```

현재 데이터 흐름과 후속 작업 경계는 [architecture.md](docs/architecture.md)에 정리했습니다.
`server/`, `inference/`, `deploy/`는 설계상 필요한 후속 구성요소이며 **현재 구현된 서비스가 아닙니다**.
빈 서비스 뼈대나 임의의 API를 추가하지 않았습니다.

## 지금 되는 것 / 남은 것

| 구성요소 | 현재 동작 | 후속 작업 |
|---|---|---|
| 계약 | encore.v1 스키마, 6개 샘플, 수신 순서·중복·세대 처리 | 미디어 업로드 계약, 팀의 draft 결정 검토 |
| 원본 플레이어 | 열기, 재생/정지, 탐색, 속도, anchor | 실제 AEC/센서 파이프라인 통합, 출력 지연 보정 |
| 세션 | WebSocket 연결, 이벤트 수신, 단절 표시, 로그 | 실제 서버 세션 API, 재연결·재전송·영속화(EM-20) |
| UI | 실제 수신 타임라인, FINAL 이벤트 강도순 Highlights, 저장 클립 리뷰 | 이벤트↔내보낸 클립 연결, 삭제, 같은 창 새 세션 |
| 감지 상태 | 카메라·마이크 수집 미구현을 Off로 명시, 카메라 상태 모델 보존 | 장치 콜백과 제어 계약 연결 |
| 클립 | ready/partial 및 실제 비어 있지 않은 파일에만 재생 제공 | EM-2 버퍼/FFmpeg 생성·검증, SQLite 인덱스 |
| 서버·AI | fake_server는 샘플 재생 전용 | 실제 API/worker/Redis/PostgreSQL, 추론·배포 |

`ready` 인덱스는 내보내기 단계에서 검증됐다는 전제입니다. UI의 파일 존재/크기 검사만으로 디코딩 성공을 보장하지 않습니다.
GStreamer echo probe가 없으면 플레이어가 unavailable을 표시합니다. 현재 상태를 AEC 완료로 해석하면 안 됩니다.
설계 문서의 PANNs와 계약 README가 기록한 YAMNet 선택은 아직 불일치합니다. 이번 통합은 모델을 임의로 선택하지 않습니다.

## 팀 협업 규칙

**공유 기준은 main, 작업은 짧은 기능 브랜치, PR의 base는 main**으로 통일합니다.
파일 수정마다 브랜치를 만들지 않습니다. 하나의 검토 가능한 작업에서 여러 커밋을 만들고 리뷰 수정도 같은 브랜치에 올립니다.

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/EM-번호-짧은설명
# 수정 후 저장소 루트에서 테스트
python -m pytest -q
git add <변경한-파일>
git commit -m "EM-번호: 변경 목적"
git push -u origin feat/EM-번호-짧은설명
# GitHub에서 base: main으로 PR 생성
```

- Jira EM 티켓/Issue에 담당자, R번호, 수용 조건, 리뷰어를 남깁니다. 한 명당 활성 구현 작업 하나를 기본으로 합니다.
- 기존 `player/minimal-player`, `em-1/event-contract`에 새 작업을 이어 붙이지 않습니다. 통합 PR이 병합되면 최신 main에서 새 브랜치를 만듭니다.
- 선행 계약/인터페이스가 필요하면 작게 먼저 main에 합칩니다. 의존 PR이 불가피하면 base와 병합 순서를 명시하고, 선행 PR 병합 뒤 **base를 main으로 직접 확인/변경**합니다.
- 작업 중 main이 바뀌면 `git fetch origin` 후 작업 브랜치에서 `git merge origin/main`하고 충돌 해결·테스트를 합니다. 공유 브랜치의 force push는 피합니다.
- PR에는 무엇이 바뀌었는지, 실행법, 테스트 결과, 아직 없는 기능을 적습니다. 팀원 1명의 리뷰와 `Client integration` 성공 후 병합합니다. 장치/GPU 기능은 실제 장치 증거도 필요합니다.
- 일반 작업 PR은 Squash and merge 후 브랜치를 삭제합니다. **이번 초기 통합 PR은 원래 브랜치 이력을 보존하도록 Create a merge commit을 권장합니다.** 기존 브랜치는 통합 확인 후 삭제합니다.
- main 보호 규칙(리뷰 1명, CI 필수, 강제 push 제한)과 병합 후 자동 삭제는 저장소 관리자가 설정합니다. 이 문서가 GitHub 설정을 자동 변경하지는 않습니다.
- 기능 코드는 폴더에 추가하고, 새로운 독립 실행 앱/복제된 Session 상태를 만들지 않습니다. 계약은 schema·sample·receiver·소비 코드를 같은 PR에서 변경합니다.
- 샘플/가짜 서버와 실제 기능을 분리합니다. 모델 테스트가 통과했다고 실제 촬영·추론이 완료됐다고 기록하지 않습니다.

설계 문서상 역할을 기준으로 다음 경계를 권장합니다(변경은 팀 합의):

| 담당 | 주 작업 경계 | 함께 리뷰할 부분 |
|---|---|---|
| 설소연 | UI, Highlights, 클립 리뷰 | 세션 상태 표시, 실제 장치 상태 표시 |
| 김파벨 | 서버 세션·기록·결과 스트림 | contracts, session 연동 |
| 허유정 | AEC·음성 추론·강도 평가 | player의 오디오 참조, 입력/결과 계약 |
| 박철 | 미디어 버퍼·전송·배포·통합 | clips, sensing, 공통 실행/CI |

얼굴 분석 담당은 문서에서도 미정입니다. 구현 전에 티켓에서 지정합니다.

## 검증

`python -m pytest -q` 하나로 unittest 기반 계약/세션 테스트와 pytest 기반 클립 테스트를 모두 수집합니다.
Windows에서는 GStreamer 관련 테스트 8개가 skip될 수 있습니다. Ubuntu CI는 먼저 실제 미디어 의존성을 확인하고 플레이어 테스트까지 실행합니다.
GUI 테스트는 offscreen이며 실제 스피커·카메라·마이크 장치 검증을 대신하지 않습니다.
샘플 영상 재생성은 루트에서 `python scripts/make_samples.py`(FFmpeg 필요)로 실행합니다.

## 제출 문서

## Submission A

The revised proposal and requirements document is preserved separately in IEEE LaTeX format (4 pages). It retains the original smart-TV scope, role table, 22 requirements/objectives, and 27 references; Submission B contains the later PC-first specification.

- [Read the Submission A PDF](encore_moment_submission_a.pdf)
- [Browse the Submission A LaTeX source](submission-a/)
- [Download the Submission A LaTeX package](encore-moment-submission-a-latex.zip)

Build with `cd submission-a` followed by `latexmk -pdf main.tex`. The source README records the original document used for this transcription.

## Submission B

Updated 2026-11-06. The current PDF is 9 pages in IEEE conference format.

- [Read the PDF](encore_moment_submission_b_.pdf)
- [Browse the LaTeX source](submission-b/)
- [Download the complete LaTeX package](encore-moment-submission-b-latex.zip)

The Introduction contains Motivation, User and Client Needs, Related Software, and Team Responsibilities and Roles, including the original role-assignment table adapted to the PC client. Sections II–IV preserve the supplied revised B: Requirements, Development Environment, and Specifications. Requirements 1–11 are Must-Have, and 12–13 are Good-to-Have.

## Build the document

With a LaTeX distribution and `latexmk` installed:

```sh
cd submission-b
latexmk -pdf main.tex
```

Alternatively, upload the complete ZIP to Overleaf and select `submission-b/main.tex` as the main document. The package includes the IEEEtran class/style, bibliography, and all three PDF figures/tables.

GitHub publication is separate from the course LMS submission and printed copy.
