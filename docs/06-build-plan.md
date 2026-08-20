# ResumeForge — Build Plan (20 Parts)

> **Version**: 1.0.0
> **Created**: 2026-08-20
> **Status legend**: ✅ done · 🔄 in progress · ⬜ not started
> **Complexity**: S (≤1h) · M (2-4h) · L (5-8h) · XL (full day+)

This is the working plan. Each part is independently completable, has explicit
deliverables and acceptance criteria, and states what it depends on. Parts are
ordered so that **something demoable exists as early as possible** — the
vertical slice (Parts 2-11) comes before breadth (Parts 12-15), and infra
(Parts 16-20) comes last so it deploys a system that actually works.

---

## Progress Overview

| # | Part | Complexity | Depends on | Status |
|---|---|---|---|---|
| 1 | Repo scaffold + deterministic extraction | L | — | ✅ |
| 2 | LangGraph state schema + graph skeleton | M | 1 | ✅ |
| 3 | Node 1 — Scraper & Keyword agent | S | 1, 2 | ⬜ |
| 4 | Node 2 — Data Retriever agent | L | 2 | ⬜ |
| 5 | Node 3 — Resume Refactorer agent | L | 2, 4 | ⬜ |
| 6 | Node 4 — Evaluator agent + guardrails | L | 5 | ⬜ |
| 7 | Self-correction loop + routing | M | 5, 6 | ⬜ |
| 8 | Node 5 — Human review interrupt | M | 7 | ⬜ |
| 9 | Node 6 — LaTeX → PDF compilation | M | 2 | ⬜ |
| 10 | PostgresSaver checkpointing | M | 2 | ⬜ |
| 11 | FastAPI surface + SSE streaming | L | 3-10 | ⬜ |
| 12 | Postgres schema + Prisma + seed profile | M | — | ⬜ |
| 13 | Express API gateway | L | 11, 12 | ⬜ |
| 14 | GitHub profile sync (PAT) | M | 12 | ⬜ |
| 15 | Next.js frontend flow | L | 13 | ⬜ |
| 16 | Docker Compose full-stack wiring | M | 11, 13, 15 | ⬜ |
| 17 | GitHub Actions CI | M | 16 | ⬜ |
| 18 | Jenkins pipeline | M | 17 | ⬜ |
| 19 | Kubernetes on kind | L | 16 | ⬜ |
| 20 | AWS EC2 deploy + CD + observability | L | 17, 19 | ⬜ |

**Interview-critical parts**: 1 (bullet 3), 7 + 10 (bullet 2 — retry and failure
recovery), 16-20 (Docker, K8s, Jenkins, CI/CD, AWS).

---
---

# Phase A — The AI Pipeline (Parts 2-11)

## Part 1 — Repo scaffold + deterministic extraction ✅ · L

**Delivered.** Monorepo scaffold, 148-skill taxonomy, Aho-Corasick matcher,
YAKE+RAKE ensemble, section weighting, 4-layer fusion pipeline, 3-tier scraper,
LLM provider seam with deterministic mock, containerised test target.

Files: `services/ai/app/extraction/*`, `services/ai/app/clients/*`,
`services/ai/data/skill_taxonomy.json`, `services/ai/tests/*`.

**Acceptance**: 39 tests green; benchmark shows flat automaton query time as the
pattern set grows. See [05-keyword-extraction.md](./05-keyword-extraction.md).

---

## Part 2 — LangGraph state schema + graph skeleton ✅ · M

Stand up the graph shape before any node does real work, so routing and
checkpointing can be tested against stub nodes.

**Deliverables**
- `app/graph/state.py` — `ResumeForgeState` TypedDict (trimmed from
  [02-agent-architecture.md](./02-agent-architecture.md) §3.1 to MVP fields).
- `app/graph/builder.py` — `StateGraph` with all 6 nodes wired, conditional
  edges registered, `interrupt_before=["human_review"]`.
- `app/graph/steps.py` — the `current_step` lifecycle enum
  (`INIT → SCRAPING → … → COMPLETE`), single source of truth for UI progress.
- Stub node functions that only advance `current_step`.

**Acceptance**: ✅ all met. 49 tests cover the topology, the routing rules as
pure functions, both durable interrupts, the bounded self-correction loop, and
checkpoint survival across a rebuilt graph object.

**Delivered beyond plan**: `app/graph/events.py` (one event shape serving SSE,
the checkpointed audit trail, and logs) and a second interrupt at
`keyword_review` for extraction Layer 4. `max_iterations` is pinned as a cap on
*total* refactor attempts, not extra retries — the looser reading would let cost
exceed the budget by a full LLM call.

---

## Part 3 — Node 1: Scraper & Keyword agent ⬜ · S

Thin wiring — the hard work already exists from Part 1.

**Deliverables**
- `app/agents/scraper_keyword.py` — calls `scrape_job_posting()`, then
  `extract_keywords()`, writes `job_text` / `job_metadata` / `keywords` to state.
- Redis cache keyed on URL hash so re-running a session does not re-scrape
  (NFR-02.3).
- Graceful path: on `ScrapeError`, set state error and expect `job_text` to be
  supplied by manual paste instead.

**Acceptance**: unit test with a stubbed scraper; a manual-paste path test that
skips scraping entirely.

---

## Part 4 — Node 2: Data Retriever agent ⬜ · L

Match the job's keywords against the user's stored profile and rank what is
relevant. **No LLM** — this is the second place determinism pays off.

**Deliverables**
- `app/agents/data_retriever.py`
- `app/matching/profile_index.py` — build a *second* Aho-Corasick automaton, this
  time over the user's profile text (experience bullets, repo READMEs, skills), so
  keyword→evidence lookup is one pass per profile item rather than a nested loop.
- Relevance scoring: keyword weight (from Part 1 sections) × evidence strength ×
  recency.
- `suggestions` output: `add` (relevant item not currently on the resume) vs
  `emphasise` (already present, should move up) vs `drop`.

**Acceptance**: given a seeded profile and the sample JD, the top-ranked items
are the genuinely relevant ones; a keyword with no profile evidence produces no
suggestion (this is what prevents hallucination downstream).

---

## Part 5 — Node 3: Resume Refactorer agent ⬜ · L

The first real LLM call. Everything about it is designed around a token budget
and around not letting the model invent facts.

**Deliverables**
- `app/agents/refactorer.py`
- `app/prompts/refactor.py` — system prompt enforcing: use only supplied
  evidence, preserve the LaTeX preamble and section structure, return LaTeX plus
  a structured changelog.
- Context assembly that sends only matched profile items, never the whole profile
  (NFR-02.2), with a hard token ceiling and a truncation strategy.
- Correction mode: accepts prior output + evaluator feedback for Part 7.

**Acceptance**: with the mock provider the node returns compilable LaTeX; token
accounting is recorded in state; assembled prompt stays under 4,000 input tokens
for the sample JD (NFR-02.4).

---

## Part 6 — Node 4: Evaluator agent + guardrails ⬜ · L

The quality gate, and the thing that makes "fault-tolerant" true.

**Deliverables**
- `app/agents/evaluator.py`
- `app/guardrails/structural.py` — **deterministic, no LLM**: LaTeX preamble
  unchanged, section count preserved, balanced braces/environments, no
  `\write18`/`\input` injection, page-count sanity.
- `app/guardrails/factual.py` — **deterministic**: every skill claimed in the
  output LaTeX must appear in the retrieved evidence set. Reuses the Part 1
  automaton against the generated resume — this is the anti-hallucination check
  and it costs zero tokens.
- LLM pass only for what rules cannot judge: tone, bullet quality, keyword
  coverage.
- Structured verdict: `{passed, factual_errors[], structural_errors[], feedback}`.

**Acceptance**: a deliberately hallucinated LaTeX fixture (claims Kubernetes with
no evidence) is caught by the *deterministic* checker with no LLM call.

---

## Part 7 — Self-correction loop + routing ⬜ · M

**Interview-critical — this is resume bullet 2's "retry handling".**

**Deliverables**
- `app/graph/routing.py` — `route_after_evaluation()`: critical errors and
  `iteration_count < max_iterations` → back to refactorer; otherwise → human
  review with warnings attached (graceful degradation, never an infinite loop).
- Feedback-driven retry: the refactorer receives its previous output plus the
  specific structured errors, not a blank regeneration request.
- Token accounting proving the targeted retry costs a fraction of a full
  regeneration.

**Acceptance**: a test forces two failed evaluations and asserts the loop runs
exactly twice then proceeds; a test forces permanent failure and asserts it
degrades to human review at `max_iterations` rather than looping.

---

## Part 8 — Node 5: Human review interrupt ⬜ · M

**Deliverables**
- `app/agents/human_review.py` and `route_after_human_review()`.
- Resume-from-interrupt entrypoint: `accept` → compile, `request_changes` →
  refactorer with the user's instruction, `edit` → evaluator on user's LaTeX.
- Diff generation (`app/diff.py`) — section-level before/after for the UI.

**Acceptance**: graph pauses at `human_review` with state persisted; a separate
process resumes it from the checkpoint with a decision and it continues.

---

## Part 9 — Node 6: LaTeX → PDF compilation ⬜ · M

**Deliverables**
- `app/compile/latex.py` — `pdflatex` in a temp dir, two passes, timeout,
  non-root, no network, no shell escape.
- Error extraction: parse the `.log` and surface the actionable line, not 400
  lines of TeX noise (NFR-05.4).
- `app/compile/sanitize.py` — reject dangerous primitives before compiling.

**Acceptance**: a real resume template compiles to a valid PDF inside the
runtime image; a malformed template returns a readable error; a `\write18`
attempt is rejected.

---

## Part 10 — PostgresSaver checkpointing ⬜ · M

**Interview-critical — this is bullet 1's "fault-tolerant" and bullet 2's
"state tracking".**

**Deliverables**
- `app/graph/checkpointer.py` — `PostgresSaver` setup, `.setup()` migration on
  boot, connection pooling.
- `thread_id` = session id, so state is per-session and never shared.
- A kill-and-resume integration test: run to mid-pipeline, drop the process,
  rebuild the graph, resume from the checkpoint, assert no lost work.

**Acceptance**: that test passes against a real Postgres container. This is the
single most valuable test in the repo for interview purposes.

---

## Part 11 — FastAPI surface + SSE streaming ⬜ · L

**Deliverables**
- `app/main.py` — lifespan builds the automaton and checkpointer once at startup.
- `POST /internal/pipeline/run`, `POST /internal/pipeline/{id}/resume`,
  `GET /internal/pipeline/{id}/events` (SSE), `GET /internal/pipeline/{id}/pdf`,
  `POST /internal/extract` (extraction only — useful for demos), `GET /health`,
  `GET /ready`.
- Internal API key dependency: the AI service trusts only the gateway.
- Structured JSON logging with a request/session correlation id.

**Acceptance**: `curl` a job URL and watch progress events stream; OpenAPI docs
render at `/docs`.

---
---

# Phase B — Application Services (Parts 12-15)

## Part 12 — Postgres schema + Prisma + seed profile ⬜ · M

**Deliverables**
- `services/api/prisma/schema.prisma` — MVP subset of
  [01-requirements.md](./01-requirements.md) §5: `User`, `WorkExperience`,
  `Project`, `Skill`, `Education`, `ChatSession`, `ChatMessage`.
- Initial migration; LangGraph owns its own checkpoint tables in the same DB.
- `prisma/seed.ts` — one real profile (yours) so the pipeline has genuine data.

**Acceptance**: `prisma migrate deploy` + seed runs clean against the container.

---

## Part 13 — Express API gateway ⬜ · L

**Deliverables**
- Routes: `/api/profile/*` CRUD, `/api/sessions`, `/api/sessions/:id/message`,
  `/api/sessions/:id/review`, `/api/sessions/:id/stream`, `/api/download/:id`.
- `src/middleware/auth.ts` — real JWT verification with a dev-mode issuer that
  mints a token for the seeded user. Google OAuth drops in behind this seam later.
- `src/services/aiClient.ts` — enriches requests with profile + LaTeX, forwards to
  the AI service, relays SSE through to the browser.
- Rate limiting (Redis), request validation (zod), `pino` logging, error envelope.

**Acceptance**: full flow driven from `curl` against the gateway only, with the
AI service unreachable from outside the compose network.

---

## Part 14 — GitHub profile sync (PAT) ⬜ · M

**Deliverables**
- `src/services/github.ts` — list repos, languages, topics, README; skip forks
  and archived repos.
- Persist to `Project` with `lastSyncedAt`; `POST /api/profile/github/sync`.
- Freshness: serve cached unless older than TTL, honour ETags.

**Acceptance**: sync populates projects from your real GitHub; a second sync
within the TTL performs no API calls.

---

## Part 15 — Next.js frontend flow ⬜ · L

Deliberately one focused flow, not a chat clone.

**Deliverables**
- `/` — paste URL (or JD text) → live progress from SSE.
- Keyword confirmation step (Layer 4 — add/remove before generation).
- Matched-evidence panel ("here's what I found relevant, and why").
- Diff view + Monaco LaTeX editor + PDF preview + download.
- Profile page for experiences/skills/LaTeX template.

**Acceptance**: the entire URL → PDF journey completes in the browser.

---
---

# Phase C — Infrastructure (Parts 16-20)

> This phase is the Salesforce-interview payload. Each part is deliberately
> hand-written rather than generated wholesale, because the value is in being
> able to explain every line.

## Part 16 — Docker Compose full-stack wiring ⬜ · M

**Deliverables**
- Root `docker-compose.yml`: `web`, `api`, `ai`, `postgres`, `redis`.
- Healthchecks + `depends_on: condition: service_healthy` so boot order is real.
- `docker-compose.override.yml` for dev (hot reload, `:z` mounts for SELinux).
- Named volumes; internal network with only `web`/`api` published.
- Multi-stage builds for `api` and `web`; non-root users everywhere.

**Acceptance**: `docker compose up` from a clean clone yields a working app.
**Talking point**: image size reduction achieved, and why the AI image is large
(Chromium + TeX Live) and how the test target avoids paying that cost.

---

## Part 17 — GitHub Actions CI ⬜ · M

**Deliverables**
- `.github/workflows/ci.yml` — path-filtered matrix so touching `services/web`
  does not run Python tests. Jobs: lint (ruff/eslint) → typecheck (mypy/tsc) →
  test (pytest with a Postgres service container; jest) → build images.
- `.github/workflows/release.yml` — on tag: build multi-stage images, push to
  GHCR with SHA + semver tags, generate an SBOM.
- Layer caching via `docker/build-push-action` + GHA cache.
- Branch protection requiring the CI check.

**Acceptance**: a PR runs the full matrix; a tag publishes images to GHCR.

---

## Part 18 — Jenkins pipeline ⬜ · M

Same pipeline, second tool — because Jenkins is on your resume and it is what
enterprises like Salesforce actually run.

**Deliverables**
- `infra/jenkins/docker-compose.yml` — Jenkins LTS with Docker-in-Docker.
- `Jenkinsfile` — declarative: `Checkout → Lint → Test (parallel) → Build →
  Push → Deploy`, with `post { always { junit … } }` publishing results.
- Credentials binding for the registry (never inline secrets).
- `infra/jenkins/README.md` — how to bring it up, and an honest comparison of
  Jenkins vs GitHub Actions for this workload.

**Acceptance**: a Jenkins build goes green locally with test results published.

---

## Part 19 — Kubernetes on kind ⬜ · L

**Deliverables**
- Install `kubectl` + `kind`; `infra/k8s/kind-cluster.yaml` (control plane + 2
  workers, ingress-ready port mappings).
- `infra/k8s/base/` — Deployments, Services, ConfigMap, Secret, Ingress,
  Postgres StatefulSet with a PVC, HPA on the AI deployment.
- Probes on every workload (`liveness`, `readiness`, `startup` for the slow AI
  boot), resource requests/limits, `RollingUpdate` strategy.
- Kustomize overlays for `dev` / `prod`.
- `infra/k8s/README.md` — the demo script: scale the AI service to 3 replicas
  and show sessions surviving because state lives in Postgres (this is the
  concrete proof behind "distributed execution" in bullet 2).

**Acceptance**: `kind create cluster` → `kubectl apply -k` → app reachable via
ingress; killing an AI pod mid-pipeline loses no work.

---

## Part 20 — AWS EC2 deploy + CD + observability ⬜ · L

**Deliverables**
- `infra/aws/` — t3.micro provisioning notes, security groups (only 80/443/22),
  `user-data` bootstrap installing Docker.
- Caddy or nginx reverse proxy with automatic TLS.
- `.github/workflows/deploy.yml` — on `main`: build → push GHCR → SSH to EC2 →
  `docker compose pull && up -d`, with a health gate and rollback to the previous
  image tag on failure.
- `/metrics` endpoints; a `docker-compose.observability.yml` with Prometheus +
  Grafana and one dashboard (pipeline duration, LLM tokens, error rate).
- Root `README.md` with architecture diagram and a demo script.

**Acceptance**: pushing to `main` deploys automatically; the public URL works;
Grafana shows pipeline metrics.

---
---

## Sequencing note

Parts 2-11 are one continuous stretch of work and are best done in order.
Parts 12-15 can be worked in parallel with them if you want to split
frontend/backend. **Parts 16-17 should be pulled forward if the interview date
moves up** — a working Compose stack plus a green CI pipeline is more
demonstrable than a more complete AI pipeline with no deployment story.
