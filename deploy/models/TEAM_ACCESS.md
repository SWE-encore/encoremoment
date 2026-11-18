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
ssh -i ~/.ssh/encoremoment USERNAME@15.165.31.24
```

The selected team network is **Tailscale**, with regular OpenSSH public-key
authentication (not Tailscale SSH). Each member installs Tailscale and joins the
team's tailnet using their own identity. The administrator must first authenticate
the server and invite the team. The tailnet policy should permit project members
to access the server on TCP 22 without granting unrelated device access.
After setup, replace the public IP above with the server's Tailscale address.

The public address may change after stop/start. The current AWS network rule
only permits the administrator's original public IP; it has NOT been widened.
Tailscale installation alone does not establish VPN connectivity: owner login,
device enrollment, policy review and a client SSH test are still required.

Current provisioning status:

- `chul`: this Windows PC's key registered; SSH and both models tested.
- `FeGigabyte`: requested for the second Ubuntu laptop; awaiting its public key.
- Other members: awaiting public keys and Tailscale invitations.

### Handoff: 2026-11-19 KST

The initial school-account login selected an unfamiliar tailnet. The server was
explicitly logged out of that network. Do not request approval there. The owner
has since signed into the Tailscale admin console with a personal account.
At this checkpoint the server still reports `Logged out` / `NeedsLogin`.
No working team VPN address has been verified; the address assigned during
the earlier enrollment must not be used as the project endpoint.

The second Ubuntu laptop has Tailscale installed (reported by the owner), but
its login and server connectivity have not been verified. `FeGigabyte` has not
yet been created because that laptop's public key has not been received.

Resume in this order:

1. Administrator: run `sudo tailscale up --hostname=encoremoment-dev
   --accept-routes=false` on EC2 and complete registration in the personal
   account's intended tailnet. Generate a fresh login link if needed; never
   commit or post device-authentication links in the team channel.
2. Ubuntu laptop: run `sudo tailscale up --hostname=fegigabyte-laptop`, open
   its own login link, and select the same personal account/tailnet.
3. Verify `tailscale status` on both devices, the server's `tailscale ip -4`,
   and any device approvals/access policy in the owner's admin console.
4. Receive the Ubuntu laptop's Ed25519 public key and enroll `FeGigabyte`.
   Test SSH and both model runners from that laptop through the VPN.
5. Invite teammates individually, review the tailnet access policy, enroll
   each device's public key, and verify their individual SSH connections.
6. Update this guide with the verified VPN endpoint. Proceed to media contracts,
   receiver API, reaction rules and PC client integration as separate work.

The EC2 instance remains running at this checkpoint; no stop, termination, or
public SSH firewall expansion was performed. Model installation and normal
administrator SSH remain available. The previous credit estimate is a snapshot,
not a refreshed balance.

On the Ubuntu laptop, generate its own key with comment `FeGigabyte-ubuntu`.
After enrollment, use `ssh -i ~/.ssh/encoremoment FeGigabyte@TAILSCALE_IP`.

Use a separate clone and feature branch per person:

```bash
cd /srv/encoremoment/workspaces/USERNAME
git clone https://github.com/SWE-encore/encoremoment.git
cd encoremoment
git switch -c feat/YOUR-TASK
```

Private repository access requires your own GitHub authentication. Do not share
a GitHub token or enable SSH agent forwarding by default. You can also develop
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
