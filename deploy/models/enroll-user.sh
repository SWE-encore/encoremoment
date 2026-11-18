#!/usr/bin/env bash
# Run as administrator with a reviewed, device-specific PUBLIC key.
set -euo pipefail
if [[ $EUID -ne 0 || $# -ne 2 ]]; then
  echo 'Usage: sudo bash enroll-user.sh USERNAME PUBLIC_KEY_FILE' >&2
  exit 2
fi
account=$1
key_file=$2
[[ $account =~ ^[a-zA-Z][a-zA-Z0-9_-]{1,30}$ ]] || exit 2
[[ $account != ubuntu && $account != root ]] || exit 2
[[ -f $key_file ]] || exit 2
[[ $(grep -cve '^$' "$key_file") -eq 1 ]] || exit 2
grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$' "$key_file" || exit 2
ssh-keygen -lf "$key_file" >/dev/null
getent group encore-dev >/dev/null || groupadd encore-dev
if id "$account" >/dev/null 2>&1; then
  id -nG "$account" | tr ' ' '\n' | grep -qx encore-dev || {
    echo 'Refusing to modify an existing non-team account' >&2; exit 2;
  }
else
  adduser --allow-bad-names --disabled-password --gecos '' "$account"
  usermod -aG encore-dev "$account"
fi
account_home=$(getent passwd "$account" | cut -d: -f6)
[[ $account_home == "/home/$account" ]] || exit 2
install -d -m 700 -o "$account" -g "$account" "$account_home/.ssh"
touch "$account_home/.ssh/authorized_keys"
key=$(cat "$key_file")
grep -qxF "$key" "$account_home/.ssh/authorized_keys" || printf '%s\n' "$key" >> "$account_home/.ssh/authorized_keys"
chown "$account:$account" "$account_home/.ssh/authorized_keys"
chmod 600 "$account_home/.ssh/authorized_keys"
install -d -m 755 /srv/encoremoment /srv/encoremoment/workspaces
install -d -m 700 -o "$account" -g "$account" "/srv/encoremoment/workspaces/$account"
echo "Enrolled $account; workspace: /srv/encoremoment/workspaces/$account"
echo 'No sudo or AWS account permissions were granted.'
