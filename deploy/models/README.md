# EC2 model installation

For individual accounts, multiple laptops, and team development, see
[Team access](TEAM_ACCESS.md). Access provisioning is separate from model installation.

This directory installs and tests **YAMNet v1** and **MediaPipe Face Landmarker
float16 v1** on an Ubuntu 24.04 x86-64 CPU host. It does not implement the
session API, microphone/camera streaming, event fusion, or clip generation.
The existing client `--server` option expects the session WebSocket protocol;
these command-line model runners are not that server.

## Install and test

Copy this directory to the host, then run as a sudo-capable user:

```bash
cd ~/encoremoment-models
bash bootstrap.sh
```

The installer creates separate Python 3.12 environments, downloads model and
public test assets from Google, and runs both smoke tests. YAMNet's first run
downloads its weights through TensorFlow Hub; subsequent runs reuse the local
`models/tfhub` cache. Face Landmarker uses `models/face_landmarker.task`.
Both runners explicitly use CPU execution. No public application port is needed.

Generated `reports/` contains inference results, installed dependency versions,
and model SHA-256 hashes. Dependency checks must pass. The audio test requires
Cat or Meow in the sample's top five labels; the face test requires a detected
face with 478 landmarks and 52 blendshapes. This verifies model execution, not
accuracy on audience reactions or sustained multi-user performance.

## Run your own inputs

```bash
.venv-audio/bin/python yamnet.py /path/to/mono-16khz-pcm16.wav
.venv-face/bin/python face.py /path/to/image.jpg
```

The audio runner accepts 0.975–60 seconds of mono 16 kHz PCM16 WAV and returns
mean class scores and inference time. It rejects other formats instead of
silently interpreting them incorrectly. Face results are landmarks and
blendshape coefficients, not emotional-state labels or identities.
Inputs are processed locally on the EC2 host; the runners do not upload inputs
to a hosted inference provider.

## Deployment connection

Initial deployment on 2026-11-18:

- Region: `ap-northeast-2` (Seoul)
- Type: `c7i-flex.large`, 2 vCPU / 4 GiB memory
- Ubuntu 24.04, 30 GiB gp3 root volume
- Directory: `/home/ubuntu/encoremoment-models`
- SSH user: `ubuntu`; port 22 is restricted to the configured client IP

This repository is public, so the instance ID, address and key file names are
kept in the team's Slack access thread instead. The administrator connects with:

```text
ssh -i ADMIN_KEY.pem ubuntu@SERVER_HOST
```

Keep private keys outside the repository. If the client public IP changes,
update the SSH source rule. If the instance is stopped and restarted, check its
new public IP before connecting. Stopping compute preserves the EBS volume and
its storage charges. Models remain installed after reboot and are loaded when
the corresponding runner starts; there is no continuously running API service.

## Verified deployment results

Both smoke tests and both `pip check` commands passed on the initial EC2 host
on 2026-11-18. Model files occupy 22 MiB; the audio and face environments occupy
approximately 2.1 GiB and 970 MiB respectively. The root disk has 22 GiB free.

| Test | Observed result |
|---|---|
| Google `miaow_16k.wav`, 6.731 seconds | 521 classes, 13 frames; Cat and Meow in top 5; 0.352 seconds inference |
| Google `portrait.jpg` | 1 face, 478 landmarks, 52 blendshapes; 0.0122 seconds inference |

These timings exclude model loading and are single-sample CPU measurements.
Full JSON results, dependency snapshots and model hashes are saved on the host
in `reports/` (not committed to Git).

## Credit estimate

| Resource | Assumed cost |
|---|---:|
| EC2 Linux, console-displayed price | $0.09576/hour |
| One public IPv4 | $0.005/hour |
| 30 GiB gp3, project estimate of $0.0912/GiB-month | $2.736/month |

At 730 hours/month this is approximately **$0.10451/hour** or **$76.29/month**
of continuous operation; divide the remaining credit shown in Billing by the
hourly figure to estimate how long it lasts. This is a planning estimate,
not a billing guarantee. The EBS rate is from the project estimate, not a fresh
account price quote. It excludes other resources, outbound data, taxes, credits
earned later, credit expiration, and the free-plan expiration date. Verify those
in Billing. Stopping when idle extends elapsed calendar time, but EBS continues
to consume credit and expiration still applies.

## References

- https://www.tensorflow.org/hub/tutorials/yamnet
- https://www.tensorflow.org/install/pip
- https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python
- https://aws.amazon.com/vpc/pricing/
