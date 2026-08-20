# ResumeForge — Infrastructure

> **Version**: 1.0.0 · **Updated**: 2026-08-20
> Covers Parts 16-20 of [06-build-plan.md](./06-build-plan.md).
> Status: Compose ✅ · CI ✅ · Jenkins ⬜ · Kubernetes ⬜ · AWS ⬜

This is the part of the project that exists to be *operated*, not just written.
Every choice below is documented with its reasoning, because the value is in
being able to explain each line rather than in having generated it.

---

## 1. Docker

### Image strategy

The AI service Dockerfile has **three stages**, and the split is the interesting
part:

| Stage | Contains | Size | Used by |
|---|---|---|---|
| `base` | Python 3.12 + pinned runtime deps | ~1.2 GB | nothing directly |
| `test` | `base` + pytest/ruff | ~1.26 GB | CI test job, local dev, hot reload |
| `runtime` | `base` + Playwright Chromium + TeX Live | ~2.5 GB | production |

**Why split.** Chromium and TeX Live together are well over a gigabyte, and unit
tests need neither. Building them into the test path would mean every CI test run
waits on a multi-gigabyte install for code that never touches a browser or
`pdflatex`. The layers are also cached independently, so editing application code
never re-triggers the apt install.

The trade-off, stated honestly: tests that genuinely need a browser or LaTeX must
target `runtime` and are marked as integration tests. Two code paths (Tier 2
scraping and PDF compilation) are therefore not exercised by the fast suite.

### Why Docker at all, for this project specifically

Not "for consistency" in the abstract — three concrete reasons:

1. **The host interpreter is wrong.** This machine runs Python 3.14;
   `pyahocorasick` and `yake` have no wheels for it. The container pins 3.12, so
   the build works regardless of what the developer has installed.
2. **Playwright needs a specific Chromium** plus a long list of shared
   libraries. `playwright install --with-deps` in a known base image is
   reproducible; on a developer laptop it is a support ticket.
3. **TeX Live is a 2GB install with OS-specific quirks.** LaTeX compilation must
   produce byte-identical output in dev and prod, or the PDF preview lies.

### Runtime hardening

| Measure | Reason |
|---|---|
| Non-root `appuser` (uid 10001) | The service compiles **untrusted user LaTeX**. That process must not own the filesystem it writes to. |
| `no-new-privileges: true` | Blocks privilege escalation via setuid binaries inside the container. |
| `HEALTHCHECK` in the image | So orchestrators other than Compose (Kubernetes, ECS) inherit a working probe. |
| Pinned dependency versions | An unpinned transitive bump is how a reproducible build stops being reproducible. |

---

## 2. Docker Compose

```bash
make up          # dev: hot reload, test image, fast startup
make up-prod     # as production runs it, no override file
make smoke       # exercise the running stack
make down        # stop, keep data
make clean       # stop, delete volumes
```

### Topology

```
        host
          │  :8000 (dev only)     :5432
          ▼                         ▼
    ┌───────────────────────────────────────────┐
    │ network: backend                          │
    │                                           │
    │   ai ──────▶ postgres     ai ──▶ redis    │
    │                                           │
    └───────────────────────────────────────────┘
    ┌───────────────────────────────────────────┐
    │ network: frontend   (web ──▶ api)  Part 15│
    └───────────────────────────────────────────┘
```

**Two networks, on purpose.** Once `web` exists it joins `frontend` only, so the
browser-facing container cannot address `postgres` or `ai` at all. Network
segmentation is cheap here and it is the same reasoning as a VPC private subnet.

`ai` is published only while `api` does not exist. In the finished topology it is
internal-only, reachable by the gateway and nothing else — which is why the
service authenticates callers with a shared key rather than user JWTs.

### Health, and why `depends_on` alone is not enough

Every service declares a healthcheck, and dependants wait on
`condition: service_healthy` rather than the default `service_started`:

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

A started Postgres container is not a ready Postgres — initdb runs after the
process accepts connections. Two details that matter:

- `pg_isready` **must** be given `-U` and `-d`. Without them it checks the
  default database and reports healthy while ours is still being created.
- The `ai` healthcheck uses a **40 second `start_period`**. Chromium and TeX Live
  make startup slow, and a short grace period restart-loops a service that was
  going to become healthy.

### Liveness vs readiness

The service exposes both, and they answer different questions:

| Probe | Question | Fails when |
|---|---|---|
| `/health` | Is the process alive? | The process is wedged. Deliberately checks **no** dependencies — Redis being down should not trigger a restart, because restarting fixes nothing. |
| `/ready` | Can it serve traffic? | Startup has not finished building the Aho-Corasick automaton. |

Conflating them is the classic mistake: a liveness probe that checks the database
turns a brief database blip into a cascading restart storm.

### Redis persistence is off

`--save "" --appendonly no`, with `allkeys-lru` and a 256MB cap. Everything in
Redis here is a scrape cache entry or a rate-limit counter. Losing it costs one
re-scrape; an AOF would be pure write overhead for data that is disposable by
design.

---

## 3. CI — GitHub Actions

`.github/workflows/ci.yml`

### Path filtering is the whole design

A `changes` job runs `dorny/paths-filter` and every downstream job is gated on
its output. Editing `services/web` must not run Python tests.

This is the concrete answer to the monorepo trade-off in
[ADR-001](./07-decision-log.md#adr-001--monorepo-not-3-polyrepos): polyrepo gives
you "don't rebuild everything" for free, and path filtering buys it back — while
keeping atomic commits across a contract change.

### Jobs

```
changes ──┬──▶ ai-lint  ──┐
          ├──▶ ai-test  ──┼──▶ ai-build ──┐
          └──▶ compose-validate ──────────┴──▶ ci-status
```

| Job | Does |
|---|---|
| `ai-lint` | `ruff check` + `ruff format --check` |
| `ai-test` | Full pytest suite against **real** Postgres and Redis service containers, plus the benchmarks printed to the log |
| `ai-build` | Builds the `runtime` image with GHA layer caching, then **smoke tests it** |
| `compose-validate` | `docker compose config` for both the production file alone and with the dev override |
| `ci-status` | One aggregate check for branch protection |

### Three decisions worth defending

**Real Postgres, not a mock.** The Part 10 checkpointing tests assert crash
recovery against actual serialisation. A mocked database would assert nothing
about whether a resumed session really works.

**No `GEMINI_API_KEY` in CI.** The pipeline is fully exercisable through
`MockProvider` ([ADR-003](./07-decision-log.md#adr-003--gemini-behind-a-provider-interface)),
and live-API tests mark themselves `integration` and skip. Two benefits: CI is
hermetic and fast, and **a rotated or rate-limited key can never turn the build
red**. A red build must mean the code is broken.

**The built image is smoke tested, not just built.** The job starts the
container, polls `/health`, reads `/ready`, and asserts that
`POST /internal/extract` **without** a key returns 401. An image that builds but
cannot serve, or that answers unauthenticated requests, is not a passing build.

**`ci-status` treats `skipped` as success.** Otherwise the aggregate check fails
every time a path filter correctly skips a job — and a single required check means
branch protection needs no edit when jobs are added.

### Benchmarks run but do not gate

`pytest tests/test_benchmark.py -s` runs so the Aho-Corasick numbers appear in CI
history, but it is not a threshold gate. Runner performance varies enough between
jobs that a wall-clock assertion would be flaky, and a flaky gate gets ignored,
which is worse than no gate. The *correctness* equivalence test between the
automaton and the naive baseline **is** a gate.

---

## 4. Release — `.github/workflows/release.yml`

Tag-triggered. Publishes to GHCR with three tags, each with a distinct purpose:

| Tag | Purpose |
|---|---|
| `v0.3.0`, `0.3` | What a human deploys |
| `sha-<full>` | What a rollback targets, unambiguously |
| `latest` | Local convenience only — never deploy this |

Authentication uses the automatic `GITHUB_TOKEN`, so there is no PAT to rotate.
Build provenance is attested and pushed to the registry, so a running digest is
traceable back to the commit and workflow that produced it.

---

## 5. Still to come

| Part | What | Note |
|---|---|---|
| 18 | Jenkins pipeline | Same stages, second tool. Enterprises (Salesforce included) run Jenkins, so the comparison is worth having first-hand. |
| 19 | Kubernetes on kind | Deployments, probes, HPA, StatefulSet + PVC. The demo — scale `ai` to 3 replicas and kill a pod mid-pipeline — is the concrete proof that state lives in Postgres and not in the process. |
| 20 | AWS EC2 + CD + observability | t3.micro, TLS, health-gated rollout with rollback, Prometheus + Grafana. |
