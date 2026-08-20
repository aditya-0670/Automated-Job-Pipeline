# ResumeForge — Documentation

ATS-aware resume tailoring: paste a job posting URL, get back your own resume
rewritten against that posting — with your LaTeX template preserved and no
invented facts.

---

## Read in this order

| # | Document | What it covers | Status |
|---|---|---|---|
| 00 | [Problem Statement](./00-problem-statement.md) | The problem, functional and non-functional requirements, user stories, out-of-scope | reference |
| 01 | [Requirements & Technical Design](./01-requirements.md) | Original technical design: stack, flows, component deep dives, data models, API design | reference |
| 02 | [Agent Architecture](./02-agent-architecture.md) | LangGraph multi-agent design: the 6 nodes, state schema, self-correction loop, guardrails | design |
| 03 | [**As-Built Architecture**](./03-as-built-architecture.md) | **What is actually being built** — service topology, trust boundary, request path, failure model | authoritative |
| 04 | [**Code Map**](./04-code-map.md) | Every file: what it implements and why | authoritative |
| 05 | [Keyword Extraction Deep Dive](./05-keyword-extraction.md) | The 4-layer pipeline, the Aho-Corasick implementation, and the measured benchmarks | implemented ✅ |
| 06 | [**Build Plan**](./06-build-plan.md) | The 20 parts, with deliverables, dependencies and acceptance criteria | working plan |
| 07 | [Decision Log](./07-decision-log.md) | ADRs: monorepo, no Kafka, Gemini, deterministic guardrails, dev auth, model pinning, thinking tokens | authoritative |
| 08 | [**Infrastructure**](./08-infrastructure.md) | Docker image strategy, Compose topology, health vs readiness, CI design | implemented ✅ |
| 09 | [**Challenges & Solutions**](./09-challenges.md) | Every real problem hit, its root cause, and the fix — written to be said out loud in an interview | **interview prep** |

Also: [`../resume-defend.md`](../resume-defend.md) — interview prep for the
resume bullets, kept at the repo root.

**For interview preparation, read [09-challenges.md](./09-challenges.md) first.**
It is the only document containing things that went *wrong*, which is what
interviewers actually probe.

### Which document wins

Docs 00-02 are the **original design**, written before implementation. Docs 03-07
describe **what exists and what is planned**. Where they disagree, **03-07 are
correct** — docs 00-02 are kept because the design reasoning in them is still
valuable, and because the delta between designed and built is itself worth being
able to explain.

---

## Current state (2026-08-20)

**Parts 1, 2, 3, 4, 10, 16, 17 of 20 complete.** 143 tests green.

- **1** — deterministic extraction: 148 skills / 489 patterns, 2.8ms per posting
  vs a naive baseline's 9.5ms, and flat as the pattern set grows.
- **2, 3** — the LangGraph graph with both durable interrupts, the bounded
  self-correction loop, and Node 1 (scrape → extract) wired in.
- **4** — Node 2 retrieves and ranks profile evidence via a second automaton,
  with skill implication so Kafka evidences "Message Queue".
- **10** — durable checkpointing proven against real Postgres: an expensive node
  runs exactly once across a simulated crash.
- **16, 17** — pulled forward: a working Compose stack (`make up`, `make smoke`)
  and GitHub Actions CI with path filtering and an image smoke test.

Next: Parts 5-9 — the Refactorer and Evaluator (the first real LLM calls), the
self-correction loop, human review, and LaTeX compilation. Then 11-15 for the
HTTP surface and application services, then 18-20 for Jenkins, Kubernetes
and AWS.

---

## Quick start

```bash
cp .env.example .env          # then set GEMINI_API_KEY (optional — a
                              # deterministic mock provider runs without it)

make build-test    # build the fast test image (no Chromium/TeX Live)
make test          # 117 tests
make bench         # the Aho-Corasick benchmark tables
make lint          # the exact gates CI runs

make up            # start postgres + redis + ai
make smoke         # exercise the running stack over HTTP
make down          # stop
```

`make help` lists every target.

---

## The three claims this project has to support

Because the resume makes them, each maps to a specific part of the build:

| Claim | Where it lives | Status |
|---|---|---|
| Deterministic keyword extraction via Aho-Corasick, no LLM tokens | [05](./05-keyword-extraction.md), `services/ai/app/extraction/` | ✅ built and measured |
| State tracking, retry handling, job lifecycle, failure recovery | Parts 7 & 10 — self-correction loop and `PostgresSaver` | ⬜ next |
| Fault-tolerant containerised pipeline (LangGraph, Playwright, Postgres, Docker) | Parts 2-11, 16-20 | 🔄 in progress |
