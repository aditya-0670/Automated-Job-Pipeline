# ResumeForge

A multi-agent pipeline that tailors a resume to a job posting **without
inventing anything**.

Handing a job description and a resume to an LLM fails in the way that matters
most: it claims skills you do not have. A resume that mentions Kubernetes
because the posting did is worse than a generic one — it gets you past the
filter and caught in the interview. So the design constraint here is not
"generate a resume", it is *generate one that cannot claim anything the profile
cannot evidence*, and most of the interesting engineering follows from that.

```
Job posting URL ─┐
                 ├─► Scrape ─► Extract keywords ─► [you confirm] ─► Match evidence
Pasted text ─────┘              (no LLM, ~50ms)                       (no LLM)
                                                                          │
                     ┌────────────────────────────────────────────────────┘
                     ▼
                 Refactor ──► Evaluate ──── clean ────► [you review] ──► Compile ──► PDF
                  (LLM)      │ rules first,                  │
                     ▲       │ 0 tokens                      │ request changes
                     └───────┘ blocking error, retries left  └──────────────┐
                       targeted retry with the specific errors              │
                     ◄──────────────────────────────────────────────────────┘
```

The two `[you …]` steps are **durable interrupts**: the graph stops, the state
is a row in Postgres, and it can be resumed minutes or a deploy later.

## Why it cannot hallucinate a skill

Three deliberate choices, in order of how much they matter:

1. **Extraction is deterministic.** An Aho-Corasick automaton over a 153-skill
   taxonomy, plus a YAKE/RAKE statistical ensemble and section weighting. Query
   time is independent of pattern count, so the taxonomy grows for free. **Zero
   LLM tokens, ~50ms**, and roughly 2,000 tokens saved per run.
2. **The rewrite sees only matched evidence**, never the whole profile —
   assembled prompt under 4,000 input tokens.
3. **The check is set membership, not judgement.** The same automaton runs over
   the *generated* resume, and every skill it finds must appear in the retrieved
   evidence set. Detection **costs zero tokens** and never depends on a model
   call succeeding; only the *fix* costs one. That is what makes bounded retries
   affordable.

On a live end-to-end run the tailored summary cited Docker, REST API and
PostgreSQL — all evidenced — and omitted Kubernetes, Azure and GCP, which the
posting asked for and the profile cannot support.

## Running it

```bash
cp .env.example .env          # works as-is; a GEMINI_API_KEY is optional
make up                       # postgres, redis, ai, api, web
make db-migrate && make db-seed
make smoke-gateway            # drives the flow end to end, spends nothing
```

Then open **http://localhost:3000**. Without an API key the pipeline runs on a
deterministic mock provider, so the whole system is explorable with no
credentials and no spend.

| command | what it does |
|---|---|
| `make test` | 351 Python tests |
| `make api-test` | 79 gateway tests |
| `make e2e` | the browser journey in real Chromium |
| `make observability` | Prometheus + Grafana on the running stack |
| `make k8s-up` | the same application on a 3-node kind cluster |
| `make ci-up && make ci-build` | Jenkins, configured from code |

## The parts worth reading

**[`services/ai`](services/ai)** — the LangGraph pipeline. Six nodes, two
durable interrupts, a bounded self-correction loop, and checkpointing to
Postgres after every node. The [keyword extraction
write-up](docs/05-keyword-extraction.md) explains the four-layer design and why
the automaton beats the obvious approaches.

**[`services/api`](services/api)** — the Express gateway. It owns the database
and holds the internal key; the AI service has no database credentials at all,
so every run is *handed* the profile it is allowed to use. That is why one
user's data cannot reach another's session.

**[`services/web`](services/web)** — one focused flow, not a chat clone. The
keyword gate, a section-level diff, a Monaco editor served from this origin, and
a PDF preview.

**[`infra/`](infra)** — [Kubernetes on kind](infra/k8s/README.md) with the
demonstration that **destroying every AI pod mid-pipeline loses no work**;
[Jenkins](infra/jenkins/README.md) configured entirely from code, with an honest
comparison against GitHub Actions; and [AWS](infra/aws/README.md), which is
written but has never been run.

**[`docs/06-build-plan.md`](docs/06-build-plan.md)** — twenty parts, each with
what was delivered beyond the plan and what went wrong. The failures are the
useful part.

## Honest status

| area | state |
|---|---|
| AI pipeline, gateway, frontend | working, tested, driven in a real browser |
| Docker Compose, GitHub Actions, Jenkins, Kubernetes | working locally, verified by running them |
| AWS deployment | **written, never run** — no AWS account behind this repo |
| Grafana dashboard | 14 panels, all showing live data locally |

The AWS half is the one thing here that has not been executed, and
[`infra/aws/README.md`](infra/aws/README.md) lists exactly what a first real
deploy would have to check.
