# ResumeForge — As-Built Architecture (MVP)

> **Version**: 1.0.0
> **Created**: 2026-08-20
> **Relationship to other docs**: [02-agent-architecture.md](./02-agent-architecture.md)
> is the *design* (v0.2.0, 3-repo polyrepo, Kafka evaluated). **This document is
> what is actually being built.** Where they disagree, this one is correct.

---

## 1. Deltas from the v0.2.0 design

Every change below is a deliberate MVP scope decision, not an oversight. The
reasoning for each is in [07-decision-log.md](./07-decision-log.md).

| Area | v0.2.0 design | As built | Why |
|---|---|---|---|
| Repo layout | 3 polyrepos | **1 monorepo**, 3 service directories | One CI pipeline instead of three; inter-service contracts are still real because the services are separate processes over HTTP |
| Message bus | Kafka topics | **None** — direct HTTP + SSE, Postgres checkpoints for durability | Broker overhead unjustified at single-user scale; see ADR-002 |
| LLM provider | OpenAI / Anthropic / Gemini | **Gemini** (`gemini-2.0-flash`) + deterministic mock | Free tier; provider is behind an interface so swapping is a config change |
| Auth | Google OAuth 2.0 | **Dev JWT** for a seeded user, behind real middleware | OAuth is plumbing with no bearing on the pipeline; the seam is in place |
| GitHub | OAuth app + repo scope | **Personal access token** | Same data, none of the OAuth dance |
| Frontend | ChatGPT-like chat UI + session sidebar | **One focused flow page** | The flow is a wizard, not a conversation; chat UI was costing days for no functional gain |
| Job queue | BullMQ / asyncio queue | **Synchronous graph run + SSE progress** | LangGraph already checkpoints; a queue adds a moving part with no MVP benefit |
| File storage | S3 / MinIO | **Container volume** | One less service; the S3 seam is a single function |

---

## 2. Service topology

```mermaid
graph TD
    subgraph PUBLIC["Published ports"]
        WEB["web · Next.js<br/>:3000"]
        API["api · Express + Prisma<br/>:4000"]
    end

    subgraph INTERNAL["Internal network only"]
        AI["ai · FastAPI + LangGraph<br/>:8000"]
        PG[("postgres<br/>:5432")]
        RD[("redis<br/>:6379")]
    end

    WEB -->|"REST + SSE"| API
    API -->|"REST + SSE<br/>X-Internal-Key"| AI
    API -->|"Prisma"| PG
    AI -->|"PostgresSaver<br/>checkpoints"| PG
    AI -->|"scrape cache"| RD
    API -->|"rate limits"| RD

    style PUBLIC fill:#1a3a5c,stroke:#2980b9,color:#fff
    style INTERNAL fill:#5c1a5c,stroke:#8e44ad,color:#fff
```

**The trust boundary is the point.** `ai` is never published. It accepts requests
only from `api`, authenticated with `INTERNAL_API_KEY`. The browser cannot reach
it, so the AI service does not need to know what a user session is — it takes a
`thread_id` and a payload.

### Why the gateway exists

| Reason | Detail |
|---|---|
| Auth centralisation | Only `api` validates user JWTs |
| Data enrichment | `api` attaches the profile and LaTeX template before forwarding, so `ai` needs no user table access for the request path |
| Protocol isolation | The browser's SSE connection terminates at `api`; `ai`'s SSE stream is internal |
| Blast radius | An LLM-prompt-injection bug in `ai` cannot reach the user database directly |

---

## 3. Request path: URL → PDF

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as api (Express)
    participant I as ai (FastAPI)
    participant P as Postgres

    B->>A: POST /api/sessions/:id/message {jobUrl}
    A->>P: persist message, load profile + LaTeX
    A->>I: POST /internal/pipeline/run (enriched payload)

    I->>I: Node 1 scrape + extract keywords (0 tokens)
    I->>P: checkpoint
    I-->>A: SSE step=SCRAPING → KEYWORDS_READY
    A-->>B: SSE relay

    Note over I: pauses for keyword confirmation (Layer 4)
    B->>A: POST /api/sessions/:id/review {keywords}
    A->>I: POST /internal/pipeline/:id/resume

    I->>I: Node 2 retrieve evidence (0 tokens)
    I->>I: Node 3 refactor (LLM)
    I->>I: Node 4 evaluate (rules first, LLM second)

    alt evaluation failed, iteration < 3
        I->>I: Node 3 again, with structured feedback
    end

    I->>P: checkpoint
    I-->>A: SSE step=HUMAN_REVIEW + diff
    A-->>B: diff + editor

    B->>A: accept
    A->>I: resume
    I->>I: Node 6 pdflatex
    I-->>A: SSE step=COMPLETE
    A-->>B: download link
```

**Two interrupt points**, both durable: keyword confirmation and final review.
State sits in Postgres between them, so the gap can be minutes or a server
restart.

---

## 4. Where determinism is enforced

This is the architectural spine of the project and the answer to "how do you stop
the LLM hallucinating".

```
                 ┌──────────────── zero LLM tokens ────────────────┐
job text  ──▶ scrape ──▶ extract keywords ──▶ retrieve evidence ──┐
                                                                   │
                                            ┌──────────────────────▼──────┐
                                            │ Node 3 Refactorer (LLM)     │
                                            │ sees ONLY matched evidence  │
                                            └──────────────────────┬──────┘
                                                                   │
              ┌──────── zero LLM tokens ────────┐                  │
              │ structural guardrails           │◀─────────────────┘
              │ factual guardrails (automaton)  │
              └────────────────┬────────────────┘
                               │ only what rules cannot judge
                      ┌────────▼────────┐
                      │ Node 4 LLM pass │  tone, bullet quality
                      └─────────────────┘
```

The LLM is confined to the two steps that genuinely need language ability.
Extraction, retrieval, and factual verification are all deterministic — the same
Aho-Corasick automaton that reads the job description is reused to verify that
every skill in the *generated* resume traces back to real evidence.

---

## 5. Failure model

| Failure | Mechanism | Where |
|---|---|---|
| AI process dies mid-pipeline | `PostgresSaver` checkpoint after every node; resume from `thread_id` | Part 10 |
| LLM returns bad output | Evaluator detects → feedback-driven retry, max 3, then graceful degradation | Part 7 |
| LLM API error / rate limit | `tenacity` exponential backoff, 3 attempts | `clients/llm.py` |
| No LLM credentials | Deterministic `MockProvider`, pipeline still runs | `clients/llm.py` |
| Job page unscrapable | HTTP → Playwright → manual paste | `clients/scraper.py` |
| LaTeX compile error | `.log` parsed, actionable line surfaced | Part 9 |
| Malicious LaTeX | Primitive denylist + non-root + no network + timeout | Part 9 |

Note what is *not* claimed: there is no leader election, no distributed
transaction, no cross-instance coordination. Multiple `ai` replicas can run
because each session is an independent `thread_id` row in Postgres — that is
horizontal scalability by statelessness, and it is worth describing precisely
rather than as "distributed execution".

---

## 6. Data ownership

| Store | Owner | Contents |
|---|---|---|
| Postgres — app tables | `api` via Prisma | users, profile, sessions, messages |
| Postgres — checkpoint tables | `ai` via LangGraph | serialised graph state per `thread_id` |
| Redis | both | scrape cache (TTL), rate-limit counters |
| Volume | `ai` | generated PDFs |

One database, two schemas, two owners, no shared tables. `ai` never writes to an
`api` table and vice versa — the contract between them is HTTP, not the database.
