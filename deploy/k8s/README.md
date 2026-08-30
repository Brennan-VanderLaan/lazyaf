# LazyAF on Kubernetes — example manifests

## What this is

Three small YAML files that show **how LazyAF's shared secrets are wired**, and
nothing else. They exist because the secrets are the part that is easy to get
wrong and dangerous to get wrong: `LAZYAF_STEP_AUTH_SECRET` signs the JWTs step
containers present to `/api/steps/*`, and `LAZYAF_RUNNER_AUTH_SECRET` is what a
runner agent presents to enrol over `/ws/runner`. There is no default for either
— the constants LazyAF shipped before 12.7 were public, so anyone could mint
credentials the backend would trust. The backend now refuses to start without
real values.

Read these as **worked examples of the env wiring**, and copy the pattern into
whatever you actually deploy with.

## What this is NOT

Not a Helm chart. Not a production deployment. Specifically, it does **not**
give you:

- **Persistence.** `emptyDir` volumes are used deliberately. LazyAF's state is
  a SQLite database plus bare git repositories on one filesystem; on a restart
  this example loses all of it. A real deployment needs a PVC (and a single
  replica — see below).
- **A story for the docker socket.** The backend's local executor spawns step
  containers on a docker daemon. In Kubernetes there usually is not one, and
  mounting a node's socket is host-root-equivalent for every pod on that node.
  The honest arrangement is `replicas: 1` for the backend with the executor
  disabled, plus runner agents on machines that do own a daemon. This example
  does not solve that problem; it shows you where it lives.
- **Ingress, TLS, or auth in front of the API.** LazyAF has no user
  authentication. Anything you expose is exposed to whoever can reach it.
- **Horizontal scale.** The runner registry and the job queue are per-process.
  `replicas` above 1 routes step assignments to a worker that holds no socket
  for the target runner. Keep the backend at one replica.
- **Anything tested by CI.** Nothing in this directory is applied by LazyAF's
  own dogfood pipeline. The compose paths are.

If you need a supported path today, use `docker-compose.release.yml`
(see [QUICKSTART.md](../../QUICKSTART.md)).

## Files

| File | What it shows |
|---|---|
| `secret.example.yaml` | The Secret's **shape**, with generation instructions. No values. |
| `backend-deployment.yaml` | Backend env wiring, both the `valueFrom` and the mounted-file styles. |
| `runner-agent-deployment.yaml` | The agent reading the **same** secret. |

## Creating the Secret

**Do not commit a Secret with values in it.** `secret.example.yaml` deliberately
ships empty so a copy-paste cannot publish a key. Generate one directly instead:

```bash
kubectl create secret generic lazyaf-secrets \
  --namespace lazyaf \
  --from-literal=step-auth-secret="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  --from-literal=runner-auth-secret="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

Add your provider keys to the same Secret if you want agent steps to run:

```bash
kubectl create secret generic lazyaf-api-keys \
  --namespace lazyaf \
  --from-literal=anthropic-api-key=sk-ant-...
```

`kubectl create secret` writes to the API server without the value ever landing
in a file you might commit. If your workflow needs the manifest in git, encrypt
it (SOPS, sealed-secrets, External Secrets) — do not check in a plain one.

## How the backend reads a secret

Two supported forms, and the file form wins:

| Variable | Meaning | Precedence |
|---|---|---|
| `LAZYAF_STEP_AUTH_SECRET_FILE` | **Path** to a file whose contents are the value | highest |
| `LAZYAF_STEP_AUTH_SECRET` | The value, inline in the environment | fallback |

Same pair for `LAZYAF_RUNNER_AUTH_SECRET`.

A `_FILE` variable that is set but unreadable or empty is a **hard error**, never
a silent fallback to the inline value. Pointing at a path is a statement about
where the secret lives; a broken projected volume should stop the pod, not sign
tokens with a stale key.

Prefer the `_FILE` form with a projected Secret volume. `valueFrom.secretKeyRef`
puts the value in the pod spec's environment, where `kubectl describe pod` and
anything reading `/proc/<pid>/environ` can see it; a mounted file is scoped to
processes that open it and is re-projected when the Secret changes.

`backend-deployment.yaml` shows both, with the inline form commented out.

## How the runner agent reads it

The agent takes the same secret from the first of these that is set:

1. `LAZYAF_RUNNER_TOKEN_FILE` — path (mounted Secret)
2. `LAZYAF_RUNNER_TOKEN` — inline
3. `LAZYAF_RUNNER_AUTH_SECRET_FILE` — path, under the backend's own name
4. `LAZYAF_RUNNER_AUTH_SECRET` — inline, under the backend's own name

3 and 4 exist so one Secret can be projected into both workloads under the same
key. The value must **equal** the backend's `LAZYAF_RUNNER_AUTH_SECRET`; if it
does not, the `/ws/runner` upgrade is rejected before `accept()` and the host
simply never appears in the runner list.

An agent with no secret configured refuses to start and says so, rather than
dialling and being rejected. Also: the agent refuses plaintext `ws://` to a
non-loopback host, because the step-dispatch frame carries the step JWT and the
step's secret environment. In-cluster that means terminating TLS in front of the
backend, or accepting the risk explicitly with
`LAZYAF_RUNNER_ALLOW_INSECURE=1` when the traffic stays inside a trusted mesh.

## Rotating a secret

Rotation is not free, and nothing here automates it. See the "Rotation" note in
the deployment files: changing either value invalidates credentials that are
already in flight.
