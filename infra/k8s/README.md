# Kubernetes (kind)

The application on a three-node cluster, with the demonstration that matters:
**a pipeline in flight survives every pod that was running it being destroyed.**
That is not a Kubernetes feature — it is what falls out of checkpointing session
state to Postgres after every node (Part 10), and the cluster is where the claim
becomes checkable.

```bash
make k8s-up        # create the cluster, install ingress, load images, apply, migrate
make k8s-status    # pods, services, ingress, HPA
make k8s-demo      # the durability demonstration, spends nothing
make k8s-down      # delete the cluster
```

Then open **http://localhost:8081**.

## Layout

```
kind-cluster.yaml               control plane + 2 workers, host ports 8081/8443
ingress-nginx-kind-patch.yaml   pins the controller to the node with the ports
base/                           the application, one file per concern
overlays/dev                    smaller CPU requests, DEBUG logging
overlays/prod                   2 replicas each, HPA 2-10, AUTH_MODE=strict,
                                and the committed dev Secret removed
```

`overlays/prod` deliberately **deletes** the dev Secret rather than overriding
it: production credentials are expected to already exist in the cluster, put
there by External Secrets, SOPS, or a human running `kubectl create secret`. The
application is identical either way; only where the values come from changes.

## The demonstration

```bash
# 1. Start a pipeline run through the ingress. Extraction spends no LLM tokens,
#    and it pauses at the keyword gate.
# 2. Delete *every* AI pod.
# 3. Ask for the session again.
```

Observed:

```
pods before          ai-...-62ttn Running
                     ai-...-t4fd2 Running
delete every AI pod  deployment "ai" successfully rolled out
pods after           ai-...-4p2tx Running 5s
                     ai-...-wjzpg Running 5s
the session          step: EXTRACTING | paused_at: keyword_review | keywords: 35
resume it            → matching → refactor → evaluate → paused at human_review
```

The second line is the interesting one: the session was not merely *readable*
after its pods died, it **continued** — on pods that did not exist when it
started. Nothing was replayed and nothing was lost, because no pod ever held the
session. The cluster has no `GEMINI_API_KEY`, so the resume ran against the
deterministic mock provider and cost nothing.

## Probes: three, doing three different jobs

Getting these wrong is the most common way a healthy service looks broken, so
each one is deliberate:

* **startup** — the AI service builds a 492-pattern automaton and runs the
  checkpointer's migrations at boot. A startup probe tolerates that without also
  making a genuinely hung process take a minute to be noticed later, which is
  what a long `initialDelaySeconds` on liveness would do.
* **readiness** → `/ready`, which checks dependencies. It decides whether this
  pod gets traffic.
* **liveness** → `/health`, which checks nothing but the process. Restarting a
  container does not fix Postgres, and a liveness probe that fails when a
  dependency is down converts one outage into a crash loop.

Postgres follows the same logic in the other direction: readiness runs a real
`select 1` (the image runs initdb against a temporary server, so `pg_isready`
answers "accepting" during a window when the database is about to restart),
while liveness is only a TCP check — restarting a database because one query was
slow is a self-inflicted outage.

## Four things that went wrong, and what they teach

### `AI_PORT` was not mine

Kubernetes injects Docker-links compatibility variables for every Service in the
namespace, so a Service named `ai` puts `AI_PORT=tcp://10.96.59.213:8000` into
*every* pod. The AI service has its own `AI_PORT` setting, which arrived as that
string and failed integer validation at startup — a crash loop whose cause is
invisible unless you print the environment. Fixed with
`enableServiceLinks: false`, which is the right answer rather than a workaround:
nothing here reads those variables, and they are a relic of Docker links.

### kindnet enforces NetworkPolicy

The comment in `networkpolicy.yaml` originally said kind's CNI ignores them. It
does not, as of kind v0.30: the migration Job sat retrying "waiting for
postgres..." forever while the gateway — which the policy allowed — connected
fine. A policy that is only enforced in production is a policy that fails in
production. The Job now carries a label of its own (`app: db-migrate`, not
`app: api`, which would make the gateway's Service route user traffic to a
migration pod) and the policy names it.

### The ingress controller floated off the node with the ports

kind's `extraPortMappings` are per-node, and this cluster puts 80/443 on the
control plane. The upstream ingress-nginx kind manifest used to select
`ingress-ready=true`; as of v1.13 it selects only `kubernetes.io/os: linux`, so
the controller scheduled onto a worker where it was reachable from inside the
cluster and from nowhere else. The symptom is a connection reset on
localhost:8081 with a perfectly healthy controller in its own logs.
`ingress-nginx-kind-patch.yaml` pins it explicitly — a dependency's default is
not something a deployment should silently depend on.

### The seed could not read its own data

`prisma/seed.ts` reads the AI service's test fixtures by relative path, so the
seeded profile and the pipeline's tests cannot drift. That path exists in a
checkout and not in the API image, whose build context is `services/api` alone.
The seed now honours `SEED_DATA_DIR`, and the cluster injects the same fixture
files as a ConfigMap — one source of truth, two delivery mechanisms.

## Two honest limitations

**Images are side-loaded, so every node that can run a workload needs a copy.**
`kind load` puts an image on the nodes you name; a pod scheduled to a node
without it fails `ImagePullBackOff`, because there is no registry to pull from.
The AI image is 3GB (Chromium for scraping tier 2, TeX Live for compilation), so
three copies is 9GB — more than this machine had spare, and the third replica
failed exactly that way. The demonstration above therefore runs with the workers
where the image is present. Part 20 pushes images to GHCR, at which point this
disappears entirely and is worth remembering as the reason a registry is not
optional.

**The HPA reports `FailedGetResourceMetric` on a bare kind cluster**, because
metrics-server is not installed and nothing serves `pods.metrics.k8s.io`. The
autoscaler is correct and does nothing; installing metrics-server
(`--kubelet-insecure-tls` on kind) makes it live. Left out because an
autoscaler that scales on CPU is the wrong instrument for this workload anyway —
the expensive part of a pipeline run is waiting on a model, which costs latency,
not CPU. The target is set to 60% so that CPU rising *at all* is treated as real
local work (extraction, LaTeX compilation) queuing up.
