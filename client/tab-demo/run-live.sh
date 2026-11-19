#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
remote=${ENCORE_SSH_TARGET:-chul@100.83.137.124}
quiet_ms=${ENCORE_QUIET_MS:-3000}
[[ "$quiet_ms" =~ ^[1-9][0-9]*$ ]] || { echo 'ENCORE_QUIET_MS must be positive milliseconds' >&2; exit 2; }
opts=(-o ConnectTimeout=8 -o ServerAliveInterval=15)
setup_ssh=false
case "${1:-}" in
  --setup-ssh)
    [[ "$remote" == 'chul@100.83.137.124' ]] || { echo 'SSH setup is for the project EC2 chul account only.' >&2; exit 2; }
    setup_ssh=true
    ENCORE_SSH_KEY=${ENCORE_SSH_KEY:-"$HOME/.ssh/id_ed25519"}
    pub="$ENCORE_SSH_KEY.pub"
    [[ -r "$ENCORE_SSH_KEY" && -r "$pub" ]] || { echo "SSH key pair missing: $ENCORE_SSH_KEY and $pub" >&2; exit 2; }
    ssh-keygen -lf "$pub"
    read -r key_type key_data _ < "$pub"
    echo 'AWS CloudShell에서 다음 한 줄을 실행하세요. EC2의 chul 계정에 이 노트북 공개키를 등록하는 절차입니다.'
    printf "aws ec2-instance-connect send-ssh-public-key --region ap-northeast-2 --instance-id i-02162e7c5540b1c21 --instance-os-user chul --ssh-public-key '%s %s' --query Success --output text\n" "$key_type" "$key_data"
    read -r -p 'CloudShell에 True가 나오면 지금 이 노트북 터미널에서 Enter를 누르세요: ' _
    ;;
  '') ;;
  *) echo 'Usage: bash client/tab-demo/run-live.sh [--setup-ssh]' >&2; exit 2 ;;
esac
if [[ -n ${ENCORE_SSH_KEY:-} ]]; then opts+=(-o IdentitiesOnly=yes -i "$ENCORE_SSH_KEY"); fi
# Authenticate once, including when EC2 Instance Connect grants a short login window.
control_dir=$(mktemp -d)
socket=$control_dir/ssh
web_pid=
cleanup() {
  if [[ -n $web_pid ]]; then kill "$web_pid" 2>/dev/null || true; fi
  if [[ -S $socket ]]; then ssh -S "$socket" -O exit "$remote" >/dev/null 2>&1 || true; fi
  rmdir "$control_dir" 2>/dev/null || true
}
trap cleanup EXIT
if ! ssh "${opts[@]}" -M -S "$socket" -fN "$remote"; then
  if ! "$setup_ssh" && [[ "$remote" == 'chul@100.83.137.124' ]]; then
    echo 'Permission denied (publickey)인 경우 한 번 실행: bash client/tab-demo/run-live.sh --setup-ssh' >&2
  fi
  exit 1
fi
if "$setup_ssh"; then
  # Run inside the authenticated instance account. Keep all existing keys and restrictions.
  printf '%s %s\n' "$key_type" "$key_data" | ssh "${opts[@]}" -S "$socket" "$remote" '
    set -eu
    [ "$(id -un)" = chul ]
    umask 077
    read -r key_type key_data
    cd "$HOME"
    mkdir -p .ssh
    chmod 700 .ssh
    touch .ssh/authorized_keys
    chmod 600 .ssh/authorized_keys
    : "${key_data:?missing key}"
    fingerprint=$(printf "%s %s\n" "$key_type" "$key_data" | ssh-keygen -lf -)
    fingerprint=${fingerprint#* }
    fingerprint=${fingerprint%% *}
    if ! ssh-keygen -lf .ssh/authorized_keys 2>/dev/null | grep -Fq -- " $fingerprint "; then
      printf "\n%s %s encore-laptop\n" "$key_type" "$key_data" >> .ssh/authorized_keys
    fi
  '
  echo 'EC2 계정의 공개키 등록을 확인했습니다. 임시 키가 만료되도록 65초 기다린 후 독립된 SSH 접속을 검사합니다.'
  sleep 65
  if ! ssh "${opts[@]}" -o ControlMaster=no -S none "$remote" 'test "$(id -un)" = chul'; then
    echo '새 SSH 로그인이 실패했습니다. 위 오류를 확인하세요. 시연은 시작하지 않았습니다.' >&2
    exit 1
  fi
  echo '새 SSH 로그인 성공. 시연 서버를 시작합니다.'
fi
opts+=(-S "$socket")
dir=$(ssh "${opts[@]}" "$remote" 'umask 077; mktemp -d /tmp/encore-live.XXXXXXXX')
[[ "$dir" =~ ^/tmp/encore-live\.[a-zA-Z0-9]+$ ]] || exit 1
printf 'Connected to %s. Demo directory: %s\n' "$remote" "$dir"
tar -czf - server/encore_server/*.py server/requirements.txt contracts/schema/messages.schema.json contracts/reference/encore_contract.py |
  ssh "${opts[@]}" "$remote" "tar -xzf - -C '$dir'"
python3 -m http.server 8000 --bind 127.0.0.1 --directory client/tab-demo &
web_pid=$!
sleep 0.3
kill -0 "$web_pid"
ssh "${opts[@]}" "$remote" "cat > '$dir/start.sh'" <<'REMOTE'
set -euo pipefail
models=/home/ubuntu/encoremoment-models
py="$models/.venv-audio/bin/python"
face="$models/.venv-face/bin/python"
task="$models/models/face_landmarker.task"
test -x "$py" && test -x "$face" && test -r "$task" || { echo "Model environment missing or unreadable under $models"; exit 1; }
export PYTHONPATH="$PWD/server:$PWD/deps"
export TFHUB_CACHE_DIR="$models/models/tfhub"
if ! "$py" -c 'from websockets.asyncio.server import serve; from jsonschema import Draft202012Validator' 2>/dev/null; then
  "$py" -m pip install --target "$PWD/deps" -r server/requirements.txt
fi
echo 'Loading EC2 models. Once the server is listening, open http://localhost:8000 on the laptop.'
exec "$py" -u -m encore_server --unix-socket "$PWD/stream.sock" --model yamnet --face-python "$face" --face-task "$task" --demo-quiet-ms "$1"
REMOTE
# A new directory isolates each server. Its foreground PTY closes with the SSH session.
ssh "${opts[@]}" -tt -o ExitOnForwardFailure=yes -L "127.0.0.1:8765:$dir/stream.sock" "$remote" "cd '$dir' && exec bash start.sh '$quiet_ms'"
