# Run the session server with Compose

Use Docker Engine with Compose v2 or later on the server, and the Python environment from the [PC client setup](../client/README.md) on the PC. The examples run from the repository root. The container listens on host loopback port 8876 by default, leaving the usual prototype port 8765 available. Set `ENCORE_PORT` to another unused port if needed.

## First run with a fixture

```sh
docker compose -f deploy/compose.yaml up --build --wait
docker compose -f deploy/compose.yaml exec -T server python deploy/smoke.py
```

The lightweight image runs the existing loudness stand-in. The smoke client sends generated PCM twice with the original chunk keys, retries microphone control, and reconnects to check replay from two cursors. A successful run prints the session ID, FINAL ID and last cursor. On the PC, run the desktop entry point against the mapped port with a generated two-second WAV. Python’s standard library is sufficient for this headless fixture check. The command verifies four delivered audio chunks, one laughter FINAL displayed by the PC, and both event timecodes against the microphone clock.

```sh
python3 deploy/smoke.py --pc --server ws://127.0.0.1:8876
```

The final JSON line contains the session ID, event ID and capture-time mapping. Save it with the checkout commit for the first-run Jira record.

When the service runs on another host, first open the tunnel on the PC with `ssh -N -L 8876:127.0.0.1:8876 YOUR_ACCOUNT@SERVER_HOST`. End the trial with `docker compose -f deploy/compose.yaml down`. Sessions and replay cursors live in server memory. Clip files stay on the PC.

## Run the two real models

Prepare a trusted model bundle once with [the model installer](models/README.md). The release directory must include `models/tfhub/<id>/saved_model.pb`, the matching variables and assets, `models/face_landmarker.task`, and `reports/model-sha256.txt`. Record the output of `sha256sum reports/model-sha256.txt` on the trusted release host. Production startup verifies the pinned manifest and its listed files before opening the listening port. The asset mount is read-only.

```sh
export ENCORE_MODEL_BUNDLE=/absolute/path/to/encoremoment-models
export ENCORE_MANIFEST_SHA256='REPLACE_WITH_TRUSTED_MANIFEST_SHA256'
export ENCORE_IMAGE=encoremoment-server:inference ENCORE_BUILD_TARGET=inference ENCORE_MODEL=yamnet
docker compose -f deploy/compose.yaml up --build --wait
docker image inspect "$ENCORE_IMAGE" --format '{{.Id}}'
```

The inference image includes TensorFlow with CUDA libraries and a separate MediaPipe environment. Build on a machine with sufficient disk and memory, then load the built image on the target if needed. Compose uses CPU by default. The [Kubernetes trial](k8s/README.md) uses the same inference image with one GPU. Run the smoke client on the host with a labeled mono 16 kHz PCM16 WAV and the mapped server port.

```sh
.venv/bin/python deploy/smoke.py --server ws://127.0.0.1:8876 --wav "$REACTION_WAV" --expect laughter
```

The standalone smoke client needs `websockets==15.0.1` (in the server requirements). It checks for a FINAL of the expected class and the YAMNet model ID. Use a recording the team has permission to process. This command tests the audio stream with camera off. A PC camera-and-microphone acceptance run remains a separate check.

## Verify persistent event history

For an isolated test, the Compose override adds PostgreSQL 16 with a named volume and initializes a fresh database with the repository migration. Database connections stay on the Compose network. Set a local test password, keep the same value across restarts, and run both commands in the same shell.

```sh
export ENCORE_TEST_DB_PASSWORD="$(openssl rand -hex 24)"
docker compose -f deploy/compose.yaml -f deploy/compose.history.yaml up --build --wait
docker compose -f deploy/compose.yaml -f deploy/compose.history.yaml exec -T server python deploy/smoke.py
```

The deployment CI also stops the disposable database, generates another FINAL, recreates the server container, and checks that the recovered database contains the same event exactly once.

In fixture mode, note the printed session and event IDs. Save the history, restart the server, and compare the results. The expected event appears once in both responses.

```sh
curl -fsS "http://127.0.0.1:8876/v1/sessions/$SESSION_ID/events"
docker compose -f deploy/compose.yaml -f deploy/compose.history.yaml restart server
curl --retry 15 --retry-connrefused --retry-delay 1 -fsS "http://127.0.0.1:8876/v1/sessions/$SESSION_ID/events"
docker compose -f deploy/compose.yaml -f deploy/compose.history.yaml down
```

The PostgreSQL volume and the PC-event delivery outbox volume survive `down` and container recreation. In history mode the server writes queued FINAL events to `/var/lib/encore/history-outbox.sqlite3` as UID 10001. Keep the same Compose project name across restarts to reuse its volumes. For a separately administered PostgreSQL database, apply the migration as schema owner and use `ENCORE_HISTORY=1` with an injected `DATABASE_URL` and credentials for the runtime role. See the [RDS connection and CA settings](../server/README.md#optional-postgresql-event-history-em-29). Mount the CA file read-only at the DSN's `sslrootcert` path when using RDS. `GET /readyz` checks the enabled archive and the face subprocess before returning HTTP 200. The sensor WebSocket is reachable only after model loading finishes.
