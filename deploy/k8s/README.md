# Single-GPU deployment trial

Use the [Compose inference image and trusted release bundle](../README.md#run-the-two-real-models) on an existing GPU-enabled test cluster. Prepare a dedicated namespace, a default StorageClass, a compatible NVIDIA driver, container runtime and device plugin. Load the same image into the node’s container runtime or deploy its registry digest via a Kustomize overlay. A local tag must resolve to the same image ID that passed Compose. The manifests request one GPU and run one server replica because live sessions and cursors are process-local.

## Stage assets and start

Run from the repository root against the intended Kubernetes context. Set `ENCORE_NS` to the existing test namespace, and use the same bundle and trusted manifest hash as in Compose. On a single-node cluster, release any previous GPU reservation before starting the trial.

```sh
kubectl config current-context
kubectl get nodes -o 'custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu'
kubectl -n "$ENCORE_NS" apply -f deploy/k8s/models-pvc.yaml -f deploy/k8s/stage-models.yaml
kubectl -n "$ENCORE_NS" wait --for=condition=Ready pod/encore-stage-models --timeout=120s
tar -C "$ENCORE_MODEL_BUNDLE" -cf - models reports/model-sha256.txt | \
  kubectl -n "$ENCORE_NS" exec -i encore-stage-models -- tar -xf - -C /bundle
kubectl -n "$ENCORE_NS" delete pod encore-stage-models --wait
kubectl -n "$ENCORE_NS" create configmap encore-model-release \
  --from-literal="manifest-sha256=$ENCORE_MANIFEST_SHA256" --dry-run=client -o yaml | \
  kubectl -n "$ENCORE_NS" apply -f -
kubectl -n "$ENCORE_NS" apply -k deploy/k8s
kubectl -n "$ENCORE_NS" rollout status deployment/encore-server --timeout=4m
kubectl -n "$ENCORE_NS" port-forward svc/encore-server 8876:8765
```

Only the staging pod writes to the model PVC. The server uses its read-only mount, a capped ephemeral `/tmp`, an internal Service, and a NetworkPolicy denying pod ingress for this port-forward trial. Run the following checks in a separate terminal.

```sh
curl -fsS http://127.0.0.1:8876/readyz
.venv/bin/python deploy/smoke.py --server ws://127.0.0.1:8876 --wav "$REACTION_WAV" --expect laughter
kubectl -n "$ENCORE_NS" get pods -l app=encore-server -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
```

## Record the outcome

| Check | Observation to record |
|---|---|
| Ready and infer | Cluster/client versions, image digest, manifest hash, GPU compute startup log, session ID, expected and actual FINAL |
| Retry | The smoke fixture retries controls and audio and replays after the final cursor. Capture its JSON or exception. |
| Dependencies | A missing GPU leaves the Pod pending or fails the GPU compute check. An incorrect model checksum exits before readiness. A missing PVC prevents startup. |
| Restart | In history mode, read the FINAL, restart the Deployment, reestablish port-forward, and verify the same event ID appears once. Live replay cursors remain in memory. |
| Stop | Scale the dedicated Deployment to zero and verify the Pod and its temporary volume are removed. The asset PVC and external PostgreSQL records survive. |

To enable the archive, supply a pre-migrated PostgreSQL database, inject its DSN through Secret `encore-history` key `database-url`, and set `record-history` to `1` in `encore-model-release`. A verified TLS connection also needs the CA mounted at the DSN path. Restart the Deployment after ConfigMap/Secret changes. The deployed server runs inference in its process and launches a local face worker. The distributed Redis queue, separate worker Deployment and worker-failover trial remain open in R13.
