# Shared development server

Every team member can receive an individual Linux account. Each laptop has its
own SSH key; multiple laptops can use the same person's account. Do not share
the administrator PEM or copy another person's private key.

## Onboard a device

Run in PowerShell, macOS Terminal, or a Linux shell (OpenSSH required):

```text
ssh-keygen -t ed25519 -f ~/.ssh/encoremoment -C "yourname-laptop1"
```

Choose a passphrase when prompted. Do not overwrite an existing key: use another
filename for a new device. Send only the content of `~/.ssh/encoremoment.pub`,
your requested Linux username (case-sensitive), and a device label to the project
administrator in the Slack access thread. Never send the file without `.pub`.
The administrator reviews the request and registers the public key:

```bash
sudo bash enroll-user.sh USERNAME /path/to/reviewed-device.pub
```

Registration is not automatic. Do not execute commands supplied inside a key
submission. This script accepts one Ed25519 public key and does not grant sudo.

## Connect and work

After your account and network access are configured:

```text
ssh -i ~/.ssh/encoremoment USERNAME@SERVER_HOST
```

`SERVER_HOST` is the server's Tailscale address. This repository is public, so
the address is shared in the Slack access thread rather than written here.

The selected team network is **Tailscale**, with regular OpenSSH public-key
authentication (not Tailscale SSH). Each member installs Tailscale and joins the
team's tailnet using their own identity. The administrator must first authenticate
the server and invite the team. The tailnet policy should permit project members
to access the server on TCP 22 without granting unrelated device access.

Until the VPN is in place, the AWS network rule only permits the administrator's
own public IP on port 22; do not widen it. The public address may change after
stop/start. Installing Tailscale alone does not establish VPN connectivity:
owner login, device enrollment, policy review and a client SSH test are required.

### VPN setup (administrator)

1. On the server, run `sudo tailscale up --hostname=encoremoment-dev
   --accept-routes=false` and complete registration in the team's tailnet.
   Never commit or post device-authentication links in the team channel.
2. Each member signs in to Tailscale on their device with their own identity
   and accepts the invitation to the same tailnet.
3. Verify `tailscale status` on both devices, the server's `tailscale ip -4`,
   and device approvals and the access policy in the admin console.
4. Enroll each device's Ed25519 public key with `enroll-user.sh` and confirm
   SSH and both model runners work through the VPN.

Track who is enrolled and the current VPN state in Slack or Jira, not in this
guide.

Use a separate clone and feature branch per person:

```bash
cd /srv/encoremoment/workspaces/USERNAME
git clone https://github.com/SWE-encore/encoremoment.git
cd encoremoment
git switch -c feat/YOUR-TASK
```

The repository is public, so cloning needs no credentials; pushing requires
your own GitHub authentication. Do not share a GitHub token or enable SSH agent forwarding by default. You can also develop
locally and copy your working files to your own server workspace with SCP, or
use VS Code Remote SSH with your individual account.

## Shared model runners

The installed baseline is read-only to team users at
`/home/ubuntu/encoremoment-models`. Run it using the installed Python executable:

```bash
/home/ubuntu/encoremoment-models/.venv-audio/bin/python /home/ubuntu/encoremoment-models/yamnet.py /path/to/input.wav
/home/ubuntu/encoremoment-models/.venv-face/bin/python /home/ubuntu/encoremoment-models/face.py /path/to/input.jpg
```

Model assets have already been downloaded. To experiment with dependencies or
modify the model runner, create your own virtual environment in your workspace.
Do not run the baseline bootstrap installer as a team user. Avoid simultaneously
installing many environments or running heavy jobs: this host has 2 vCPU and
4 GiB RAM shared by everyone. This is not a GPU host.

Commit code, configuration examples, and tests to Git. Keep private keys, real
environment secrets, user recordings, model binaries, virtual environments,
and logs out of Git. Submit a PR to main; shared baseline updates are deployed
by the administrator after review. Personal accounts have no sudo or AWS
console permissions. Coordinate server restart/stop and shared deployments
in Slack before acting.

## Remove a lost device

The administrator removes that device's key line from the person's
`authorized_keys`; other laptop keys can remain. Account removal, disabling
access, and deletion of workspaces are separate operations. Record the key
fingerprint and device label when enrolling; verify the specific key before
revocation.
