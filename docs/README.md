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
| 07 | [Decision Log](./07-decision-log.md) | ADRs: monorepo, no Kafka, Gemini, deterministic guardrails, dev auth | authoritative |

Also: [`../resume-defend.md`](../resume-defend.md) — interview prep for the three
resume bullets, kept at the repo root.

### Which document wins

Docs 00-02 are the **original design**, written before implementation. Docs 03-07
describe **what exists and what is planned**. Where they disagree, **03-07 are
correct** — docs 00-02 are kept because the design reasoning in them is still
valuable, and because the delta between designed and built is itself worth being
able to explain.

---

## Current state (2026-08-20)

**Part 1 of 20 complete.** The deterministic keyword extraction layer is built
and tested — 39 tests green in a container, with the Aho-Corasick automaton
measured at 2.8ms per posting against a naive baseline's 9.5ms, and flat as the
pattern set grows. See [06-build-plan.md](./06-build-plan.md) for the full board.

Next: the LangGraph pipeline (Parts 2-11), then the application services
(12-15), then infrastructure — Docker Compose, GitHub Actions, Jenkins,
Kubernetes, AWS (16-20).

---

## Quick start

```bash
cp .env.example .env          # then set GEMINI_API_KEY (optional — a
                              # deterministic mock provider runs without it)

# AI service tests and benchmarks (the only runnable part today)
cd services/ai
docker build --target test -t resumeforge-ai:test .
docker run --rm resumeforge-ai:test pytest tests/ -s
```

Full-stack `docker compose up` arrives in Part 16.

---

## The three claims this project has to support

Because the resume makes them, each maps to a specific part of the build:

| Claim | Where it lives | Status |
|---|---|---|
| Deterministic keyword extraction via Aho-Corasick, no LLM tokens | [05](./05-keyword-extraction.md), `services/ai/app/extraction/` | ✅ built and measured |
| State tracking, retry handling, job lifecycle, failure recovery | Parts 7 & 10 — self-correction loop and `PostgresSaver` | ⬜ next |
| Fault-tolerant containerised pipeline (LangGraph, Playwright, Postgres, Docker) | Parts 2-11, 16-20 | 🔄 in progress |
