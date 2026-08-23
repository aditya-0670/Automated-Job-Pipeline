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
| 3 | Node 1 — Scraper & Keyword agent | S | 1, 2 | ✅ |
| 4 | Node 2 — Data Retriever agent | L | 2 | ✅ |
| 5 | Node 3 — Resume Refactorer agent | L | 2, 4 | ✅ |
| 6 | Node 4 — Evaluator agent + guardrails | L | 5 | ✅ |
| 7 | Self-correction loop + routing | M | 5, 6 | ✅ |
| 8 | Node 5 — Human review interrupt | M | 7 | ✅ |
| 9 | Node 6 — LaTeX → PDF compilation | M | 2 | ✅ |
| 10 | PostgresSaver checkpointing | M | 2 | ✅ |
| 11 | FastAPI surface + SSE streaming | L | 3-10 | ✅ |
| 12 | Postgres schema + Prisma + seed profile | M | — | ✅ |
| 13 | Express API gateway | L | 11, 12 | ✅ |
| 14 | GitHub profile sync (PAT) | M | 12 | ✅ |
| 15 | Next.js frontend flow | L | 13 | ✅ |
| 16 | Docker Compose full-stack wiring | M | — | ✅ (ai/pg/redis; api+web seams ready) |
| 17 | GitHub Actions CI | M | 16 | ✅ |
| 18 | Jenkins pipeline | M | 17 | ✅ |
| 19 | Kubernetes on kind | L | 16 | ✅ |
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

## Part 3 — Node 1: Scraper & Keyword agent ✅ · S

Thin wiring — the hard work already exists from Part 1.

**Deliverables**
- `app/agents/scraper_keyword.py` — calls `scrape_job_posting()`, then
  `extract_keywords()`, writes `job_text` / `job_metadata` / `keywords` to state.
- Redis cache keyed on URL hash so re-running a session does not re-scrape
  (NFR-02.3).
- Graceful path: on `ScrapeError`, set state error and expect `job_text` to be
  supplied by manual paste instead.

**Acceptance**: ✅ all met. 13 tests with a stubbed scraper cover all three
input paths (URL, cache hit, pasted text), failure translation, and the
zero-token contract.

**Delivered beyond plan**: `clients/cache.py` is an interface
(`Null`/`Memory`/`Redis`) rather than a hard Redis dependency, so unit tests need
no server and a cache outage is indistinguishable from a miss — an optional
optimisation must never be able to fail the pipeline. Re-running the node via the
`modify_keywords` route resets `keywords_confirmed`, so an earlier approval
cannot silently apply to a different keyword set.

---

## Part 4 — Node 2: Data Retriever agent ✅ · L

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

**Acceptance**: ✅ met, and tested against the **real** profile (derived from
`aditya_ojha_resume.tex`) rather than toy data. Against the Salesforce posting the
top-ranked items are the LangGraph project and the Oracle role — the genuinely
relevant ones — and Kubernetes, which the profile cannot evidence, produces no
suggestion.

**Delivered beyond plan — skill implication.** The first run reported
"Message Queue" as a missing skill for a profile with Kafka, and "CI/CD" as
missing for one evidencing GitHub Actions. The taxonomy was flat. Entries now
carry an `implies` list (65 of them), and profile evidence expands along it at a
0.7 discount with `implied_by` recorded for transparency. Implication expands
**profile evidence only, never job-description extraction** — a posting asking
for Kubernetes is not asking for Docker.

---

## Part 5 — Node 3: Resume Refactorer agent ✅ · L

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

**Acceptance**: ✅ met. The assembled prompt for the real resume and posting is
under 4,000 input tokens, verified by a test. Live Gemini calls confirm the
preamble, all 12 packages and all 10 custom macros survive, every `\section`
is preserved in order, and the model claims none of the forbidden skills.

**Delivered beyond plan**: evidence is fitted to the token budget by dropping
from the least-relevant end with a floor of 3 items (and a warning when it
truncates, since a silently shortened prompt is a silently worse resume); a
model fallback chain and server-directed retry, because the free tier is 20
requests/day and returns 503 often (see challenges 18); and a repair for
double-escaped LaTeX from JSON round-trips.

---

## Part 6 — Node 4: Evaluator agent + guardrails ✅ · L

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

**Acceptance**: ✅ met, and asserted directly — a hallucinated fixture is caught
with `provider.calls == 0`. 31 guardrail tests plus 22 node tests. An inflated
metric (500+ → 800+) is caught exactly; an implied skill (Kafka evidencing
"message queue") is correctly accepted; the real resume passes against its own
profile.

**Delivered beyond plan**: the LLM quality pass is skipped entirely when rules
already found a blocking error — the output is about to be regenerated, so
spending tokens on its tone is waste. A failed quality review never fails the
pipeline, because the deterministic checks already established the resume is
sound.

---

## Part 7 — Self-correction loop + routing ✅ · M

**Interview-critical — this is resume bullet 2's "retry handling".**

**Deliverables**
- `app/graph/routing.py` — `route_after_evaluation()`: critical errors and
  `iteration_count < max_iterations` → back to refactorer; otherwise → human
  review with warnings attached (graceful degradation, never an infinite loop).
- Feedback-driven retry: the refactorer receives its previous output plus the
  specific structured errors, not a blank regeneration request.
- Token accounting proving the targeted retry costs a fraction of a full
  regeneration.

**Acceptance**: ✅ met. 8 integration tests drive the real Evaluator through the
real graph: one retry on a hallucination then pass, the retry receiving the
specific errors *and* its own prior output, a permanently failing model stopping
exactly at the cap, and graceful degradation to review with the unresolved issues
attached.

**Key measured property**: detection costs **zero tokens** — only the *fix* costs
a model call, which is what makes bounded retries affordable.

---

## Part 8 — Node 5: Human review interrupt ✅ · M

**Deliverables**
- `app/agents/human_review.py` and `route_after_human_review()`.
- Resume-from-interrupt entrypoint: `accept` → compile, `request_changes` →
  refactorer with the user's instruction, `edit` → evaluator on user's LaTeX.
- Diff generation (`app/diff.py`) — section-level before/after for the UI.

**Acceptance**: ✅ met. 38 tests. The graph pauses before `human_review` with
`refactored_latex` already durable, and a **separately constructed graph object**
— sharing nothing but the checkpointer, standing in for a different replica —
resumes it with a decision and runs to completion. Every route is driven end to
end: `request_changes` loops through refactor and evaluate and pauses again with
`review_iteration` incremented; `modify_keywords` lands back at the keyword gate.

**Delivered beyond plan**

- **The diff is derived, never stored.** It is a pure function of two fields that
  are already checkpointed, so persisting it would put a third copy of the resume
  in every checkpoint row to save a sub-millisecond computation — and a fix to the
  diff algorithm now applies to sessions that were already paused when it shipped.
  Sections are the unit, not lines: LaTeX rewraps, so a line-level diff reports a
  reflowed paragraph as a rewrite and trains the user to click through without
  reading. Whitespace-only churn is not a change.
- **`app/graph/resume.py` refuses to resume the wrong interrupt.** A review
  decision posted to a session actually waiting at `keyword_review` would write a
  field no node reads and then run the wrong node; it now raises before touching
  the graph, and the session stays resumable.
- **A user-initiated revision restores the self-correction budget.**
  `iteration_count` bounds *automatic* retries; carried over, the user's third
  revision would silently get no guardrail retries at all because earlier rounds
  spent them. Each new round is a deliberate human decision to spend.
- **`final_latex` records what was approved**, separately from
  `refactored_latex`, so a later write cannot change what the PDF traces to.
- **Malformed input degrades rather than strands.** Resuming with no decision, or
  `request_changes` with no instruction, accepts the resume and says so in the
  warnings — the session has already been paid for in LLM spend and the user has
  already seen the output.
- **Node 6 wired and the registry closed** (`app/agents/compile_pdf.py`,
  `builder.real_nodes()`), so `accept` now produces a real PDF and the graph runs
  end to end. `real_nodes()` imports the agents lazily so `builder.py` stays free
  of LLM, scraper and database imports and the topology remains testable without
  them. Part 9's compiler needed only this wrapper; a compile failure ends with an
  actionable message and the approved LaTeX intact rather than discarding the run.

**Also fixed**: `test_search_scales_with_text_not_pattern_count` compared the full
taxonomy against a quartered one, which changes the *match* count as well as the
pattern count — so it measured per-match bookkeeping, not the property it names,
and its 2.5x bound was one loaded CI box away from a red build. It now grows the
pattern set with synthetic entries that appear nowhere in the text, isolating the
variable, and holds to 1.6x.

---

## Part 9 — Node 6: LaTeX → PDF compilation ✅ · M

**Deliverables**
- `app/compile/latex.py` — `pdflatex` in a temp dir, two passes, timeout,
  non-root, no network, no shell escape.
- Error extraction: parse the `.log` and surface the actionable line, not 400
  lines of TeX noise (NFR-05.4).
- `app/compile/sanitize.py` — reject dangerous primitives before compiling.

**Acceptance**: ✅ met with the **real** template — 164KB, 1 page, 572ms,
including `fontawesome5` and all 10 custom macros. Nine dangerous primitives are
rejected; a missing package is reported by name; `\write18` never reaches
pdflatex.

**Delivered beyond plan**: `pdf_content_hash()` masking the non-deterministic
`/ID` trailer so identical input hashes identically (useful for caching), an
escaped-percent-aware comment stripper (resumes are full of `60\%`, and treating
that as a comment would delete the rest of the line), and page-count warnings.

**Completed in Part 8**: the compiler shipped without a graph node, so `accept`
had nowhere to go. `app/agents/compile_pdf.py` closes that, and two tests in the
runtime image (the only place with TeX Live) now run the approved-LaTeX-to-file
path for real — the fast suite stubs the compiler on one side and the node on the
other, so nothing there ever put the two together.

---

## Part 10 — PostgresSaver checkpointing ✅ · M

**Interview-critical — this is bullet 1's "fault-tolerant" and bullet 2's
"state tracking".**

**Deliverables**
- `app/graph/checkpointer.py` — `PostgresSaver` setup, `.setup()` migration on
  boot, connection pooling.
- `thread_id` = session id, so state is per-session and never shared.
- A kill-and-resume integration test: run to mid-pipeline, drop the process,
  rebuild the graph, resume from the checkpoint, assert no lost work.

**Acceptance**: ✅ met. 12 tests pass against the real Postgres in Compose,
including the kill-and-resume test and a stronger one: an *expensive* node runs
exactly once across a simulated crash, proving recovery does not repeat LLM
spend. Verified in the database directly — `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes` tables, one row per node transition, partitioned by
`thread_id`.

**Fixed later**: the suite used fixed `thread_id`s against a persistent
database, so `events` — an *append* reducer — accumulated the previous run's
entries and the test failed on every run after the first. Green once per
database: fine in CI, which gets a clean service container, misleading on a
developer's long-lived Compose stack. Thread ids are now unique per run and each
run deletes its own threads.

**Delivered beyond plan**: `latest_state()` so the gateway can answer "where is
my session?" after a page reload **without advancing the graph**; `thread_config()`
raises on an empty session id, because a typo would silently start a fresh thread
and lose the session. The service degrades honestly if Postgres is unreachable —
it still starts and serves extraction, and `/ready` reports
`checkpointer: unavailable` rather than crash-looping the container.

---

## Part 11 — FastAPI surface + SSE streaming ✅ · L

**Deliverables**
- `app/main.py` — lifespan builds the automaton and checkpointer once at startup.
- `POST /internal/pipeline/run`, `POST /internal/pipeline/{id}/resume`,
  `GET /internal/pipeline/{id}/events` (SSE), `GET /internal/pipeline/{id}/pdf`,
  `POST /internal/extract` (extraction only — useful for demos), `GET /health`,
  `GET /ready`.
- Internal API key dependency: the AI service trusts only the gateway.
- Structured JSON logging with a request/session correlation id.

**Acceptance**: ✅ met, and verified against the running Compose stack, not only
in tests. `make smoke-pipeline` starts a real session over HTTP, watches the
progress event stream, **restarts the `ai` container mid-session**, and confirms
the session is still paused at the keyword gate with all 35 keywords intact —
served by a process that never ran the graph. 42 tests cover the surface with
stub nodes and an in-memory checkpointer, so no token is spent and no database is
needed. OpenAPI renders at `/docs`.

**Delivered beyond plan**

- **The run outlives its request.** A full run makes at least two LLM calls; if
  the request drove it, a closed tab would cancel a node mid-flight and waste
  spend already incurred. `run` and `resume` return **202** with a session id and
  hand the work to a background task, so a disconnecting client loses its *view*
  of the pipeline and nothing else.
- **Progress is tailed from the checkpoint, not from the running task.** The
  obvious implementation streams `graph.astream()` to the client, which works
  only while the stream and the run share a process — with two replicas a
  reconnecting browser lands on the wrong one and sees nothing. Every node
  already appends to the checkpointed `events` list, so the stream polls that
  instead. Any replica serves any session, reconnection resumes from the last
  sequence number, and the stream is a read that can never advance the graph.
  This is the same property Part 19's demo rests on, reached through HTTP.
- **SSE ids are the event sequence numbers**, so a browser's automatic
  `Last-Event-ID` on reconnect replays exactly the gap — no duplicates, no hole.
  Only progress frames carry an id: numbering a terminal frame would let a client
  ask to resume from a position that is not in the event list. An unparseable id
  replays the session rather than failing it.
- **A pause ends the stream.** The graph is waiting on a person; holding the
  connection open would be waiting on them too. A 10-minute cap closes forgotten
  streams, and the client reconnects and loses nothing.
- **A crash in a background task becomes durable state.** The request already
  returned 202, so an unhandled exception would otherwise be invisible and the
  client would watch a stream that never ends. The failure is written to the
  checkpoint, which every replica can see, and the stream terminates with the
  reason. A node that raises leaves LangGraph reporting a pending `next` node —
  indistinguishable from an interrupt from outside — so a recorded failure now
  outranks the pending task rather than reporting a pause nobody will answer.
- **One resume endpoint, not two.** The server already knows where the session is
  paused; asking the client to route would let a review decision be posted to a
  keyword pause and be silently ignored. Input is validated *before* the task is
  launched, so bad input is a 400, not a failure discovered on the stream.
- **Status codes chosen to tell a polling client what to do**: 409 (not 404) for
  a PDF that does not exist *yet*; 410 for a checkpointed path whose file is gone
  after a restart with a non-persistent volume; 409 for a reused session id,
  because a session id is a checkpoint thread id and reuse would resume someone
  else's pipeline rather than start a new one; 503 with a reason when there is no
  checkpointer, since a pipeline that cannot survive a restart is worse than an
  honest refusal.
- **`app/api/` split out** so `main.py` is composition only — startup, the trust
  boundary, correlation — and every rule the endpoints apply lives in
  `app/graph/`, reachable from a test or a script without going through FastAPI.

---
---

# Phase B — Application Services (Parts 12-15)

## Part 12 — Postgres schema + Prisma + seed profile ✅ · M

**Deliverables**
- `services/api/prisma/schema.prisma` — MVP subset of
  [01-requirements.md](./01-requirements.md) §5: `User`, `WorkExperience`,
  `Project`, `Skill`, `Education`, `ChatSession`, `ChatMessage`.
- Initial migration; LangGraph owns its own checkpoint tables in the same DB.
- `prisma/seed.ts` — one real profile (yours) so the pipeline has genuine data.

**Acceptance**: ✅ met. `make db-migrate` applies `0001_init` and `make db-seed`
loads the real profile — 2 experiences, 2 projects, 34 skills, 1 education entry,
5 achievements and the 8.5KB LaTeX template — against the Compose Postgres.
Re-running the seed changes nothing: same user id, same row counts. `make
api-test` is green (13 tests), and a new `api · schema, migration and seed` CI
job runs migrate → seed → **seed again** → typecheck → test against a real
Postgres service container.

**Delivered beyond plan**

- **"Two owners, one database" is now enforced by Postgres, not by a comment.**
  Prisma refused the first `migrate deploy` outright (P3005: "the database schema
  is not empty") because LangGraph's three checkpoint tables were already in
  `public`. The fix is a separate namespace: Prisma owns everything in the `app`
  schema, LangGraph owns `public`, and the API's `DATABASE_URL` carries
  `?schema=app`. The boundary the AI service's docstring claimed is now a thing
  the database itself will not let either side cross.
- **Migrations are authored with `migrate diff --from-empty`, never `migrate
  dev`.** `migrate dev` diffs the whole database against the schema and offers to
  reset on drift; here that would delete every in-flight session. `make db-diff
  NAME=...` writes the next migration for review, and only `migrate deploy` ever
  touches the database.
- **`src/profile.ts` and a round-trip test, which is the schema's real
  acceptance criterion.** The AI service consumes a `user_profile` document whose
  shape is fixed by its evidence index; a schema that stores plausible user data
  but cannot rebuild that document fails in Part 13, after the gateway has been
  written against it. The test reads the *seeded database* and asserts it
  reproduces `real_profile.json` exactly — experiences, projects, education
  (rendered summary included), achievements and skills.
- **That test found a missing model.** The plan's MVP list has no `Achievement`,
  but the AI's evidence index treats achievements as first-class evidence, so
  without the table the seeded profile silently lost five pieces of evidence —
  the LeetCode, Codeforces, CodeChef, ICPC and HackOn entries the resume actually
  leans on. Added, with the reason recorded in the schema.
- **Two data-modelling decisions worth defending**: `endDate = null` means
  "current", with no companion `isCurrent` boolean — two columns describing one
  fact are two columns that can disagree; and month formatting reads `@db.Date`
  columns in **UTC**, because a local-timezone read turns 2026-01-01 into
  December 2025 for every user west of Greenwich, which is a wrong date on a
  resume that reviews do not catch.
- **The seed reads the pipeline's own fixture** rather than a copy. A copy would
  drift, and the seeded database would then hold a profile the pipeline's tests
  never exercise. The round-trip test is what makes that coupling checked rather
  than merely intended.
- **Node runs in a container like everything else** (`NODE_RUN` in the Makefile),
  as the host user, so nothing it writes lands root-owned — which the first
  install attempt did.

---

## Part 13 — Express API gateway ✅ · L

**Deliverables**
- Routes: `/api/profile/*` CRUD, `/api/sessions`, `/api/sessions/:id/keywords`,
  `/api/sessions/:id/review`, `/api/sessions/:id/stream`,
  `/api/sessions/:id/pdf`.
- `src/middleware/auth.ts` — real JWT verification with a dev-mode issuer that
  mints a token for the seeded user. Google OAuth drops in behind this seam later.
- `src/services/aiClient.ts` — enriches requests with profile + LaTeX, forwards to
  the AI service, relays SSE through to the browser.
- Rate limiting (Redis), request validation (zod), `pino` logging, error envelope.

**Acceptance**: ✅ met, and `make smoke-gateway` is the check. It asserts
`localhost:8000` is **unreachable from the host**, then drives the flow through
port 4000 only: 401 unauthenticated → dev token → the profile the pipeline will be
given (2 experiences, 2 projects, 34 skills, 5 achievements, template present) →
`POST /api/sessions` → the SSE stream relayed through the gateway → paused at the
keyword gate with 35 keywords. Everything up to that gate costs nothing, so the
smoke test is free to run; the target prints the one command that continues into
paid territory. 45 tests (32 for the gateway) run against a fake AI client and a
real database.

**Delivered beyond plan**

- **Dev auth changes only who can *mint* a token.** The tempting shape —
  "if AUTH_MODE is dev, attach the seeded user" — means the verification path
  never runs until the first real token in production meets code that has never
  executed. Instead `POST /api/auth/dev-token` issues, and every request is
  verified identically in both modes. `issuer` and `audience` are asserted too,
  not just the signature, so a token minted with the same secret for another
  purpose cannot authenticate a session. Expired and invalid are distinguished
  because one means "refresh" and the other means "stop retrying".
- **Rate limiting fails in two different directions, on purpose.** Reads fail
  **open**: a limiter that rejects when Redis is down turns a cache outage into a
  total outage. Pipeline runs fail **closed**: each one spends model quota that is
  20 requests a day on the free tier, and "we could not check the limit, so we
  allowed it" is how a broken Redis becomes an exhausted quota. Refusing is
  recoverable; spending is not.
- **Ownership lives in the `where` clause, never in a check after the fetch.**
  Routes use `updateMany`/`deleteMany`/`findFirst` with `userId` in the filter, so
  a forgotten check cannot exist — the row is simply not found. Tested with a
  second user against every session route, including the SSE and PDF ones, which
  are the easy pair to forget.
- **The SSE relay forwards bytes untouched and aborts upstream on disconnect.**
  Re-parsing frames would put a second SSE implementation between the pipeline and
  the browser when the event ids are already correct; and without the abort, every
  closed tab leaks a connection plus a Postgres poll on the AI service. It also
  sets `x-accel-buffering: no`, because nginx buffers `text/event-stream` by
  default and turns a live stream into one delivery at the end.
- **Preconditions are checked before anything is spent.** No template, or no
  experience and no project, is a 400 from the gateway — the pipeline would refuse
  too, but only after paying for a scrape and an extraction.
- **The session row is written before the run starts, and marked `failed` if the
  start fails.** A run that begins and is then not recorded is a run the user pays
  for and cannot find; the reverse is visible and explainable. The row's id **is**
  the LangGraph `thread_id`, so one key addresses both halves.
- **Unexpected exceptions never reach the client.** Their text is written for
  developers and routinely contains connection strings and query fragments; a test
  asserts a thrown `postgresql://user:password@...` does not appear in the
  response.
- **A real Dockerfile and a CI image smoke test, because of a bug that no test
  could catch.** Prisma picks its query-engine binary from whatever OpenSSL exists
  where `generate` runs, so a build stage without openssl silently produced a
  1.1.x engine for a 3.0.x runtime and the container crash-looped on boot. The
  target is now declared in `schema.prisma` — the runtime's platform is a property
  of the runtime, not of the builder — and CI builds the image, boots it, and
  checks `/ready` reports a real database connection.
- **`ai` is no longer published.** The production compose file gives it no host
  port at all; the dev override republishes it so `make smoke` and
  `make smoke-pipeline` can still talk to it directly. That is what makes "the AI
  service is unreachable from outside the compose network" a property of the
  deployment rather than a claim.

---

## Part 14 — GitHub profile sync (PAT) ✅ · M

**Deliverables**
- `src/services/github.ts` — list repos, languages, topics, README; skip forks
  and archived repos.
- Persist to `Project` with `lastSyncedAt`; `POST /api/profile/github/sync`.
- Freshness: serve cached unless older than TTL, honour ETags.

**Acceptance**: the second half is met and asserted — a second sync inside the
TTL returns `status: "fresh"` with **`apiRequests: 0`**, and the test proves it by
counting calls on a fake `fetch` rather than trusting the field. A forced sync
sends the stored ETag and GitHub's 304 costs one request that is *not* charged
against the rate limit. 24 new tests (69 total in the service).

**Not yet done against real GitHub**: `GITHUB_TOKEN` is empty in `.env`, so the
happy path has never run against a live account. `make github-sync` does the whole
thing — store the PAT, sync, re-sync, force, list the resulting projects — as soon
as a PAT is set. What *has* been verified live is the unhappy path: a bogus token
posted through the gateway reached `api.github.com`, came back 401, and surfaced
as `{"code":"github_unauthorized"}` with a 400.

**Delivered beyond plan**

- **The PAT is encrypted at rest** (AES-256-GCM, `src/crypto.ts`), with its own
  `ENCRYPTION_KEY` separate from `JWT_SECRET` — one signs short-lived tokens and
  rotates freely, the other is all that stands between a database dump and a
  usable credential, so sharing them would make rotating the cheap one break the
  expensive one. Verified in the database: the column reads `v1.7--QAqKC…`. GCM
  rather than CBC because a tampered ciphertext must fail loudly instead of
  decrypting into something that gets sent to GitHub as a credential, and the
  `v1.` prefix exists so a future scheme is distinguishable rather than guessed.
- **Two mechanisms, doing different jobs.** The TTL answers "should we sync at
  all?" with no network call and *before the token is even decrypted*; the ETag
  answers "did anything change?" with a request GitHub does not bill. Conflating
  them would mean either a stale profile or a wasted rate limit.
- **Forks and archived repos are dropped before their detail requests, not
  after.** Each repo costs two extra calls (languages, README), and paying them
  to discover something is a fork is the expensive way to learn it.
- **A private repo's README is not read.** Private repos are kept — a resume can
  legitimately cite work the reader cannot open — but the point of enrichment is
  public evidence, and a private README is the likeliest place for something the
  user did not mean to publish.
- **The sync never writes `bullets`, and never touches a hand-written project.**
  Bullets are the resume's prose, the user's or the Refactorer's; the sync owns
  evidence only. Manual projects have a null `repoName` and Postgres treats NULLs
  as distinct in a unique index, so the two sets cannot collide — tested with a
  manual project and a repo of the same name.
- **A repository that disappears from GitHub is kept, not deleted.** The user may
  have edited its bullets; deleting is destructive and was not asked for.
- **A repo untouched for 90 days gets an end date** rather than reading as
  "Present" on a resume, which would be a small untruth.
- **Failures are classified rather than flattened**: a rejected token is a 400 the
  user can act on, an exhausted rate limit is a 429 carrying `resetAt`, a plain
  refusal is a 403, and an unreachable GitHub is a 502. An undecryptable stored
  token is reported without a single API call.
- **The `db-diff` flow got its first real use** and needed two fixes:
  `migrate diff --from-empty` never writes `migration_lock.toml` (only
  `migrate dev` does, and this project never runs it), and `--from-migrations`
  wants a shadow database whose obvious URL is the real one — which Prisma drops
  and recreates. It now diffs read-only against the live database.

---

## Part 15 — Next.js frontend flow ✅ · L

Deliberately one focused flow, not a chat clone.

**Deliverables**
- `/` — paste URL (or JD text) → live progress from SSE.
- Keyword confirmation step (Layer 4 — add/remove before generation).
- Matched-evidence panel ("here's what I found relevant, and why").
- Diff view + Monaco LaTeX editor + PDF preview + download.
- Profile page for experiences/skills/LaTeX template.

**Acceptance**: ✅ met, driven in a **real browser** start to finish.
`make e2e FULL=1` completes the whole journey in one run — start screen → pasted
posting → live SSE progress → keyword gate with 35 keywords → generation → review
gate with the diff → approve → compile → PDF — with no browser console errors.
The compiled PDF was checked outside the browser too: 164KB, one page, and its
tailored summary cites Docker, REST API and PostgreSQL while omitting Kubernetes,
Azure and GCP. **That is the anti-hallucination guarantee holding in a live
end-to-end run, not in a fixture.** Screenshots land in `out/e2e/`.
`make e2e` without `FULL=1` stops at the keyword gate and spends nothing;
`SESSION_URL=…` resumes an existing session, so a frontend fix can be retested
without paying for the same generation twice.

**Delivered beyond plan**

- **Monaco is served from this origin, not a CDN.** `@monaco-editor/react`
  fetches the editor from jsDelivr by default — unusable offline, a third-party
  origin on every page load, and blocked outright by any reasonable CSP. The image
  build copies `monaco-editor/min/vs` into `public/monaco/vs`. The e2e test asserts
  `.monaco-editor` is actually visible, because a missing local copy leaves the
  "Loading editor…" placeholder forever and no DOM assertion would notice.
- **The screen renders from `paused_at`, never from a local step counter.** The
  pipeline can move *backwards* — self-correction, "change keywords" — and a
  counter in the browser would get that wrong. A reload lands on exactly the right
  gate because the answer comes from the checkpoint.
- **The stream is for progress; the status endpoint is for truth.** Events tell
  the user something is happening, and the full state (keywords, diff, warnings)
  is fetched when the pipeline stops. Reconstructing state from the event log
  would mean a reloaded page showed less than a fresh one.
- **CORS on the gateway is an allowlist, not `*`.** Bearer tokens live in
  `localStorage`, and the allowlist is what makes a stolen token useful only from
  a page we served. `Allow-Credentials` is deliberately never set — combined with
  a reflected origin, that is the pair that turns permissive CORS into CSRF. Four
  tests cover it, including that a preflight never reaches an authenticated route.
- **Unresolved guardrail failures sit above the approve button**, not in a
  details pane. The pipeline degrades gracefully rather than looping forever, so
  the moment the user signs off is exactly when they must see what was not fixed.
  Unsupported keywords are shown too, with the reason they were left off.
- **The PDF preview is an `<iframe>`.** The browser already has a PDF renderer;
  shipping pdf.js to preview a one-page document is a large dependency for nothing.
- **A trimmed `evidence` list was added to the AI service's status endpoint** —
  the full `matched_evidence` carries the entire text of every bullet and README
  that was indexed, which is megabytes on a poll.
- **`next` was upgraded from 15.5.4 to 16.3.2** because npm flagged a security
  advisory (CVE-2025-66478) on the version I first pinned. Pinned exactly, like
  every other dependency here: a caret means the image built tomorrow is not the
  image tested today.
- **The e2e suite reuses the AI image's Chromium** rather than installing
  `@playwright/test` in `services/web`, which would add a browser download to a
  service that otherwise needs none, to drive the same three clicks.
**Four bugs the paid run found, three of them real** — the reason it was worth
running rather than reasoning about:

1. **The stream was never reopened after a gate was answered.** The server ends
   the stream at every pause (correctly — it is waiting on a person), so once the
   user answers there is no stream and nothing to reopen it. The page sat at the
   keyword gate while the pipeline finished behind it. `useSession` now exposes
   `resume()`, called exactly when a gate is answered.
2. **`session_status` called any pending node a pause.** A *running* graph always
   has a `next` node — the one about to execute — so a status read taken mid-run
   reported "paused at refactorer" and the UI rendered a gate that does not exist.
   Only the two interrupt points mean "waiting for a person" now.
3. **A resuming client was told about the stop it had just answered.** Reopening
   the stream races the background task's first checkpoint, so the reader saw the
   *old* pause and reported it as the end. A caller that is caught up is asking
   "what happens next?", not "where am I?", so that first already-known stop is
   waited through. The client has to say where it got to for this to work, and
   `EventSource` only sends `Last-Event-ID` on its *own* reconnects — hence the
   `lastEventId` query parameter, which the gateway forwards.
4. **A 401 on the PDF iframe**, caught by the journey's console-error check.
   `pdfUrl` and `streamUrl` read the token synchronously during the first render,
   before `ensureToken()` resolved, so on a cold load they went out empty. Both
   now wait for the token.

- **One layout bug found by looking at the screenshot**: an expanded diff dragged
  the whole page into horizontal scroll (the full-page capture was 4,191px wide).
  The diff now gets its own full-width row in a fixed-layout table and scrolls
  inside its own box; the e2e test measures `scrollWidth - clientWidth` and fails
  if the page scrolls sideways at all.
- **One copy bug, also from the screenshot**: with a pasted description there is
  no job metadata, so the keyword-gate hint read "this posting. Click a keyword…"
  — a sentence starting mid-clause. It now describes the source instead of naming
  it.

---
---

# Phase C — Infrastructure (Parts 16-20)

> This phase is the Salesforce-interview payload. Each part is deliberately
> hand-written rather than generated wholesale, because the value is in being
> able to explain every line.

## Part 16 — Docker Compose full-stack wiring ✅ · M

**Deliverables**
- Root `docker-compose.yml`: `web`, `api`, `ai`, `postgres`, `redis`.
- Healthchecks + `depends_on: condition: service_healthy` so boot order is real.
- `docker-compose.override.yml` for dev (hot reload, `:z` mounts for SELinux).
- Named volumes; internal network with only `web`/`api` published.
- Multi-stage builds for `api` and `web`; non-root users everywhere.

**Acceptance**: ✅ met for the services that exist. `make up` yields three
healthy containers; `make smoke` proves /health, /ready, a 401 without the
internal key, and real keyword extraction over HTTP. Postgres 17.11 and Redis
are both reachable from inside the `ai` container. `api` and `web` are wired as
commented seams with their networks and dependencies already decided.

**Pulled forward** ahead of Parts 11-15 so the infra story landed early, which
also unblocks Part 10 (checkpointing needs a real Postgres).

**Delivered beyond plan**: a `Makefile` with 20 targets, structured JSON logging
with request-id correlation across app and uvicorn lines, and the minimal
FastAPI surface (`/health`, `/ready`, `/internal/extract`) needed to make the
container a real service. Two networks so that `web`, once it exists, cannot
address `postgres` or `ai` at all.

---

## Part 17 — GitHub Actions CI ✅ · M

**Deliverables**
- `.github/workflows/ci.yml` — path-filtered matrix so touching `services/web`
  does not run Python tests. Jobs: lint (ruff/eslint) → typecheck (mypy/tsc) →
  test (pytest with a Postgres service container; jest) → build images.
- `.github/workflows/release.yml` — on tag: build multi-stage images, push to
  GHCR with SHA + semver tags, generate an SBOM.
- Layer caching via `docker/build-push-action` + GHA cache.
- Branch protection requiring the CI check.

**Acceptance**: ✅ written and locally verified — `make lint` runs the exact
gates CI runs (`ruff check`, `ruff format --check`) and passes; the full suite
passes against real Postgres/Redis containers. Awaiting a GitHub remote to
execute on.

**Delivered beyond plan**: the built image is **smoke tested** in CI, not merely
built — the job starts it, polls `/health`, reads `/ready`, and asserts that an
unauthenticated `POST /internal/extract` returns 401. CI runs with **no**
`GEMINI_API_KEY` on purpose, so a rotated or rate-limited key can never turn the
build red; live-API tests skip themselves. `ci-status` aggregates into one
required check and treats `skipped` as success, so path filters do not fail the
gate.

---

## Part 18 — Jenkins pipeline ✅ · M

Same pipeline, second tool — because Jenkins is on your resume and it is what
enterprises like Salesforce actually run.

**Deliverables**
- `infra/jenkins/docker-compose.yml` — Jenkins LTS with Docker-**outside**-of-Docker.
- `Jenkinsfile` — declarative: `Checkout → Lint → Test (parallel) → Build →
  Push → Deploy`, with `post { always { junit … } }` publishing results.
- Credentials binding for the registry (never inline secrets).
- `infra/jenkins/README.md` — how to bring it up, and an honest comparison of
  Jenkins vs GitHub Actions for this workload.

**Acceptance**: ✅ met. `make ci-up && make ci-build` → **build 8, SUCCESS in
248s, 430 tests published, 0 failures, 37 skipped.** Results appear in Jenkins'
test report whether the build passes or fails.

**Delivered beyond plan**

- **The controller is configuration, not clicks.** JCasC (`casc.yaml`) owns the
  security realm, the authorization strategy, the registry credential and the job
  definition; `plugins.txt` pins the plugin set. `make ci-reset && make ci-up`
  reproduces it exactly, and a change made in the UI is gone on the next rebuild.
  The setup wizard is disabled, so there is no manual first-run step — which is
  the thing that makes most Jenkins installations unreproducible.
- **Not `unsecured`.** A controller with no authentication *and* the host's
  Docker socket is a root shell for anyone who can reach the port, and "it is
  only local" is how that ends up exposed on a VPN.
- **Docker-outside-of-Docker rather than DinD**, with the cost stated rather than
  glossed: a job that can reach the host's socket can do anything root can do on
  the host. Fine for a local demo, not for a shared controller, and the README
  names the fix (an agent per job, or a socket proxy).
- **Push and Deploy are gated on `branch 'main'` *and* a configured registry**, so
  a local build skips them instead of failing on a registry that does not exist.
  The credential is bound by id — rotatable without touching the pipeline, and
  `****` in the log.

**Four things that went wrong, all of them instructive**

1. **`readFileFromWorkspace` cannot be used from JCasC.** There is no seed job, so
   there is no workspace, and the null pointer fails the whole controller at
   startup — not the job. The job-dsl script reads the file directly instead.
2. **An unsandboxed pipeline is held for manual approval on first run**, which
   turns "rebuild the controller" into "rebuild the controller, then go click a
   button". Sandboxed; declarative pipelines do not need the escape.
3. **Docker created the workspace's parent directory as root**, so Jenkins could
   not create the `<job>@tmp` sibling it needs and *every* `sh` step failed with
   AccessDeniedException before running a command. The image now seeds the
   directory with the right ownership.
4. **`-v` and `docker build` need different paths under DooD** — the daemon
   resolves one and the client reads the other. Getting it wrong does not error:
   Docker creates the missing directory and mounts it **empty**, and the build
   fails several steps later complaining about a missing lockfile.

**Three bugs the pipeline found in the project itself** — which is the argument
for running CI against a clean environment rather than a developer's machine:

1. **A checkpoint test was passing on stale data.**
   `test_latest_state_reads_without_advancing` used a bare thread id that Part
   10's uniqueness fix had missed. It passed locally *only* because rows from
   earlier runs were still in the dev database; a fresh CI Postgres failed it
   immediately.
2. **`make test-checkpoint` only worked with the dev override applied**, because
   it ran `docker compose exec ai pytest` and the production `ai` image has no
   pytest. It now runs the test image against the compose network, in both modes.
3. **A flaky readiness check, and stale test reports.** `pg_isready` answers
   "accepting" while the official Postgres image is still running initdb against
   a temporary server, so the next command gets "rejecting connections" — the
   pipeline waits on a real `select 1`. And because the workspace persists
   between builds, a build that failed before running a single test still
   published the *previous* build's green results; reports are now deleted at the
   start of every run.

---

## Part 19 — Kubernetes on kind ✅ · L

**Deliverables**
- Install `kubectl` + `kind`; `infra/k8s/kind-cluster.yaml` (control plane + 2
  workers, ingress-ready port mappings).
- `infra/k8s/base/` — Deployments, Services, ConfigMap, Secret, Ingress,
  Postgres StatefulSet with a PVC, HPA on the AI deployment.
- Probes on every workload (`liveness`, `readiness`, `startup` for the slow AI
  boot), resource requests/limits, `RollingUpdate` strategy.
- Kustomize overlays for `dev` / `prod`.
- `infra/k8s/README.md` — the demo script.

**Acceptance**: ✅ both halves met. `make k8s-up` → the app answers on
**http://localhost:8081** through the ingress, and a dev token fetched through it
returns the seeded profile — 2 experiences, 2 projects, 34 skills, template
present — so the whole stack is talking inside the cluster.

And the durability claim, `make k8s-demo`, which is stronger than the plan asked
for. The plan said "killing an AI pod loses no work"; the demo destroys **every**
AI pod while a session is paused at the keyword gate, and then:

```
-- destroying every AI pod --
   ai-59f64b45b9-4j57x
   ai-59f64b45b9-wrvj4
   ai-59f64b45b9-8wp5h 5s      (replacements)
   ai-59f64b45b9-czvbg 5s
-- the same session, on pods that did not exist when it started --
 paused at keyword_review with 35 keywords ✓
-- and it continues, not just reads --
   EVALUATING human_review
```

The last line is the point: the session was not merely *readable* after its pods
died, it **continued** — matching, refactoring and evaluating on pods that did
not exist when it started. No pod ever held the session. The cluster carries no
`GEMINI_API_KEY`, so the resume ran on the deterministic mock provider and cost
nothing.

**Delivered beyond plan**

- **NetworkPolicies**, expressing the compose file's two-network split for
  Kubernetes: `ai` accepts traffic only from `api`, and Postgres only from `ai`,
  `api` and the migration Job. Without one, the `web` pod — the code closest to
  the browser — could talk straight to the database.
- **Migrations as a Job, not an init container.** An init container runs once per
  replica and per restart, so scaling the gateway to three pods runs three
  concurrent migrations racing on one advisory lock. A Job runs once and is
  visible as a thing that succeeded or failed.
- **The prod overlay deletes the committed dev Secret** rather than overriding
  it, so a production apply fails loudly if the real secret was never created,
  instead of quietly deploying with development credentials.
- **Probes chosen per job, not copy-pasted**: liveness hits `/health` (nothing
  but the process) while readiness hits `/ready` (dependencies) — a liveness
  probe that fails when Postgres is down converts one outage into a crash loop.
  Postgres inverts it: readiness runs a real `select 1`, liveness is only a TCP
  check, because restarting a database over one slow query is self-inflicted.

**Four things that went wrong, all worth knowing**

1. **`AI_PORT` was not mine.** Kubernetes injects Docker-links compatibility
   variables for every Service, so a Service named `ai` puts
   `AI_PORT=tcp://10.96.59.213:8000` into every pod — which collided with this
   service's own `AI_PORT` setting and failed integer validation at startup. A
   crash loop whose cause is invisible unless you print the environment. Fixed
   with `enableServiceLinks: false`: nothing here reads those variables.
2. **kindnet enforces NetworkPolicy.** My own comment said it did not. The
   migration Job sat retrying "waiting for postgres..." forever while the
   gateway, which the policy allowed, connected fine. A policy only enforced in
   production is a policy that fails in production.
3. **The ingress controller floated off the node with the host ports.** kind's
   `extraPortMappings` are per-node; ingress-nginx's kind manifest used to select
   `ingress-ready=true` and as of v1.13 selects only `kubernetes.io/os: linux`.
   The controller landed on a worker, reachable from inside the cluster and
   nowhere else — a connection reset on localhost:8081 with a perfectly healthy
   controller in its logs. Now pinned by an explicit committed patch.
4. **The seed could not read its own data.** It reads the AI service's fixtures
   by relative path so the seeded profile cannot drift from the pipeline's tests
   — a path that exists in a checkout and not in the API image, whose build
   context is `services/api` alone. The seed now honours `SEED_DATA_DIR` and the
   cluster injects the same files as a ConfigMap: one source of truth, two
   delivery mechanisms.

**Two honest limitations**, both recorded in the README rather than hidden:

- **Images are side-loaded, so every node that can run a workload needs a copy.**
  The AI image is 3GB (Chromium for scraping, TeX Live for compilation), and this
  machine did not have 9GB spare for three copies — the third replica failed
  `ImagePullBackOff` exactly that way. Rather than leave that as a mystery, the
  dev overlay pins application pods to nodes labelled
  `resumeforge.dev/images-loaded`, applied by `make k8s-images` to the nodes it
  loaded. That is the truth about a registry-less cluster, and the prod overlay
  has no such selector because images come from a registry there. Part 20's GHCR
  push removes the constraint entirely.
- **The HPA reports `FailedGetResourceMetric`** on a bare kind cluster: nothing
  serves `pods.metrics.k8s.io` without metrics-server. The autoscaler is correct
  and inert. Left out because CPU is the wrong signal for this workload anyway —
  the expensive part of a run is waiting on a model, which costs latency, not
  CPU — which is also why the target is 60% rather than 80%: CPU rising at all
  means real local work is queuing.

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
