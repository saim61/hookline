# Deploying Hookline

Three processes from one image, differing only by their command:

| Process | Command | Scales on |
|---|---|---|
| API | `uvicorn hookline.main:app` (image default) | request rate |
| Worker | `hookline-worker` | how slow customer endpoints are |
| Migrations | `alembic upgrade head` | run once per deploy |

One image rather than three, because three would be three things to keep in step and three
chances for the code and the schema to skew apart.

---

## Docker Compose

The whole stack, with nothing installed locally except Docker:

```bash
docker compose up -d
docker compose exec api hookline-admin create-key --name "local"
curl localhost:8000/ready
open http://localhost:8000/dashboard
```

For day-to-day development, run only the infrastructure and keep the app on the host, where
reload works and a debugger can attach:

```bash
docker compose up -d db redis
uv run fastapi dev src/hookline/main.py
uv run hookline-worker
```

`migrate` is a one-shot service that the API and worker both wait on with
`service_completed_successfully`. Without that gate the API boots against a database missing
the tables it is about to query.

### The image

Multi-stage: the build stage has uv and the dev dependencies, the runtime stage has neither.
Roughly 370MB, most of which is the Python base image.

Three details that matter more than the size:

- **`uv sync --no-editable`** installs a real copy into `site-packages` rather than a link back
  to `/app/src`, so the runtime stage needs the virtualenv and nothing else. The Jinja2
  templates and the stylesheet travel with it.
- **`--frozen`** fails the build if `uv.lock` disagrees with `pyproject.toml`, rather than
  quietly resolving something the test suite never ran against.
- **Non-root with a numeric uid** (10001). A name would not satisfy Kubernetes `runAsUser`,
  which wants a number.

`README.md` is deliberately *not* in `.dockerignore`: `pyproject.toml` declares
`readme = "README.md"`, so the build backend reads it to produce the package metadata and the
build fails without it. That one cost a build.

---

## Kubernetes, with Helm

The chart declares no Postgres or Redis dependency. Bundling them as subcharts makes a chart
that is easy to demo and wrong to run — a database managed by the same release as the
application gets deleted along with it. Point the chart at instances that outlive it.

```bash
helm upgrade --install hookline deploy/helm/hookline -n hookline --create-namespace \
  --set image.repository=ghcr.io/you/hookline --set image.tag=v0.1.0 \
  --set secrets.existingSecret=hookline-credentials
```

The Secret needs two keys, `database-url` and `redis-url`. Create it out of band — with a
sealed-secret, external-secrets, or by hand. Values passed on the command line end up in the
Helm release object, in shell history, and in whatever CI logged the command.

### Migrations run as a pre-upgrade hook

This is the part worth reading carefully, because the obvious approaches are all wrong in a
specific way.

- **Not an init container.** Those run once per pod, so three replicas race three
  `alembic upgrade` processes on the version table.
- **Not inside the app.** The application should not be able to migrate the database it is
  querying.
- **Not a post-install hook.** With `--wait`, Helm waits for pods to be ready before running
  post-install hooks, and the pods are waiting for the schema. That deadlocks.
- **Not relying on readiness to hold traffic.** `/ready` runs `select 1`, which succeeds
  perfectly well against an empty schema, so it will happily mark a pod ready with no tables.

So: `pre-install,pre-upgrade`, at weight `-5`, with `before-hook-creation` deletion so the
previous Job survives long enough for `kubectl logs` to answer "what did that upgrade do". The
hook failing aborts the release, which is exactly what should happen — rolling out code that
expects a column the database does not have is worse than not rolling out.

**The trap:** Helm creates *all* pre-install hooks before *any* normal manifest, so at hook time
the chart's own ConfigMap and Secret do not exist yet. The first attempt failed with
`configmap "hookline-config" not found`. Hook-annotating them too fixes the ordering but makes
them hook-managed, and `helm uninstall` then leaves them orphaned. The Job instead takes only
what it needs: alembic reads one setting, the database URL, and everything else has a working
default.

### Probes

| | API | Worker |
|---|---|---|
| liveness | `/health` — never touches Postgres | `:9100/metrics` |
| readiness | `/ready` — 503 without Postgres | none |
| startup | `/health`, 30s budget | none |

Liveness restarts the pod, so if it checked the database a brief Postgres blip would restart
every replica at once — turning a recoverable outage into a much worse one. Readiness only pulls
the pod out of the load balancer, which is the correct response to "cannot reach the database".

The worker has no readiness probe because nothing routes traffic to it; adding one would only
give Kubernetes a reason to call a working pod unavailable. Its liveness probe hits the metrics
listener, which is bound exactly when the process is running — with the honest caveat that a
worker wedged on something would keep answering it. The real "is delivery healthy" signal is
`hookline_oldest_pending_delivery_age_seconds`, not a probe.

### Scaling

Workers coordinate through Postgres with `SELECT ... FOR UPDATE SKIP LOCKED`, so adding
replicas needs no configuration and no leader election:

```bash
kubectl scale -n hookline deploy/hookline-worker --replicas=6
```

Scale on queue age, not CPU:

```promql
max(hookline_oldest_pending_delivery_age_seconds)
```

The chart ships CPU-based HPAs, off by default, and the `autoscaling.yaml` comment is honest
about why CPU is a poor proxy for the worker: a worker waiting on a slow endpoint uses almost no
CPU, so the metric that matters barely moves it. Use a custom-metrics adapter or KEDA for the
real signal.

`worker.terminationGracePeriodSeconds` defaults to 60. It must exceed the worst case for one
batch — `workerBatchSize × deliveryTimeoutSeconds` — or SIGKILL arrives mid-batch and leaves
rows `in_flight` until the reaper reclaims them. Nothing is lost either way; it is just slower
than it needs to be on every rolling deploy.

### Config changes need the checksum annotation

Both Deployments carry `checksum/config`, a hash of the rendered ConfigMap. Kubernetes restarts
nothing when a ConfigMap is updated, and environment variables are read once at startup, so
without it a config change appears to apply and quietly does nothing.

---

## Minikube

Verified end to end on Minikube — this is the exact sequence, not an approximation:

```bash
minikube start --driver=docker --memory=4096 --cpus=2
docker build -t hookline:local .
minikube image load hookline:local

kubectl create namespace hookline

helm repo add bitnami https://charts.bitnami.com/bitnami
helm install hookline-db bitnami/postgresql -n hookline \
  --set auth.username=hookline --set auth.password=hookline --set auth.database=hookline \
  --set primary.persistence.enabled=false
helm install hookline-cache bitnami/redis -n hookline \
  --set auth.enabled=false --set architecture=standalone \
  --set master.persistence.enabled=false

helm upgrade --install hookline deploy/helm/hookline -n hookline \
  --set image.repository=hookline --set image.tag=local --set image.pullPolicy=Never \
  --set secrets.databaseUrl="postgresql+asyncpg://hookline:hookline@hookline-db-postgresql:5432/hookline" \
  --set secrets.redisUrl="redis://hookline-cache-redis-master:6379/0" \
  --wait
```

`pullPolicy=Never` is required: the image was side-loaded into the node, so any attempt to pull
it fails.

Then:

```bash
kubectl exec -n hookline -it deploy/hookline-api -- hookline-admin create-key --name bootstrap
kubectl port-forward -n hookline svc/hookline-api 8080:80
open http://localhost:8080/dashboard
```

What the run confirmed, beyond "the pods started":

- All four migrations applied in order, from an empty database, by the hook Job.
- A `helm upgrade` re-ran the hook as a no-op, rolled the API pods via the checksum annotation,
  and the changed setting was present in the container's environment afterwards.
- **Two worker pods claimed disjoint batches** — six events split 1 and 5 between them, with no
  duplicate delivery. That is `SKIP LOCKED` doing its job across separate processes on separate
  pods, which no single-process test can demonstrate.

Tear down:

```bash
helm uninstall hookline hookline-db hookline-cache -n hookline
kubectl delete namespace hookline
minikube stop
```

---

## CI

`.github/workflows/ci.yml` runs four jobs on every push and pull request:

| Job | What it proves |
|---|---|
| `lint` | ruff, ruff format, mypy --strict |
| `test` | the full suite against Postgres and Redis service containers |
| `image` | the Dockerfile builds **and the image boots** |
| `helm` | the chart lints, renders with defaults and with every feature on, and the output validates against real Kubernetes schemas |

`lint` is split from `test` so a formatting mistake fails in twenty seconds rather than after
Postgres has finished starting.

The `image` job does not stop at a successful build. It runs the container and waits for
`/health`, which catches a missing template directory or an entrypoint that is not on `PATH` —
things a build cannot detect. It then asserts `/ready` returns **503** with no database
reachable, which is a regression test for the probe split: if `/health` ever starts touching
Postgres, every replica will restart together during the next database blip, and this is the
check that notices.

The `helm` job renders twice, with defaults and with everything enabled, because the ingress,
HPA and ServiceMonitor templates are never parsed while their feature is off. It then runs
`kubeconform` over the result — `helm template` only proves the Go templates render, not that
the output is a valid Kubernetes object.

`.github/workflows/release.yml` runs on `v*` tags only, and publishes a multi-arch image to
GHCR with a build-provenance attestation. Tags rather than branches: `latest` moving on every
merge to main makes a rollback impossible to describe.
