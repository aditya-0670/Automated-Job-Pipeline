# ResumeForge — Decision Log (ADRs)

> Each entry records a decision, the alternatives, and the reasoning.
> These are the answers to "why did you build it this way?" — the format exists
> so the reasoning survives after the decision is forgotten.

---

## ADR-001 — Monorepo, not 3 polyrepos

**Date**: 2026-08-20 · **Status**: accepted · **Supersedes**: [02-agent-architecture.md](./02-agent-architecture.md) §8

**Context.** The v0.2.0 design specified three independent Git repositories
(`resumeforge-web`, `-api`, `-ai`) to mirror enterprise microservice practice.

**Decision.** One repository, three service directories.

**Reasoning.** The stated benefit of polyrepo was learning inter-service
contracts and independent CI/CD. The contracts are preserved regardless — the
services are still separate processes, separate images, separate languages,
communicating over HTTP with an explicit API key boundary. What polyrepo actually
adds at this scale is three CI pipelines to maintain, cross-repo version
coordination, and no atomic commit when a contract changes on both sides. For a
single developer that is pure overhead. Path-filtered CI (Part 17) recovers the
"don't rebuild everything" property that polyrepo gives for free.

**Cost accepted.** Independent deployment is now a pipeline concern rather than a
repo boundary. If a service ever needs a different release cadence, extracting it
is a `git subtree split`.

---

## ADR-002 — No Kafka

**Date**: 2026-08-20 · **Status**: accepted

**Context.** Kafka appears on the resume and in the original design as the
decoupling layer between the API and the AI pipeline, with four topics
(`resume-jobs`, `resume-pipeline-events`, `resume-status`, `resume-dlq`)
partitioned by `user_id`.

**Decision.** No broker. Direct HTTP with SSE for progress, and LangGraph's
`PostgresSaver` for durability.

**Reasoning.** Kafka would buy three things here: durability, async decoupling,
and replay. Durability is already provided by checkpointing state to Postgres
after every node — and provided *better*, because a checkpoint is the actual
resumable pipeline state, not a message that a consumer must reconstruct state
from. Async decoupling has no MVP benefit: the flow is interactive, the user is
waiting, and there is nothing to defer. Replay is a debugging convenience that
checkpoint inspection also provides.

Against that: a broker plus KRaft, consumer group management, offset and lag
monitoring, and a DLQ policy — operational surface with no user-visible benefit
at single-user concurrency.

**When this flips.** Multi-tenant concurrent job queueing, or work that must
outlive the request (batch-tailoring one resume against 50 postings). At that
point the queue is doing something checkpointing cannot.

**Interview note.** State this as a *design decision with a documented trigger
condition*, never as "we used Kafka". The topic schema is real design work and
worth describing; claiming a running broker is not defensible.

---

## ADR-003 — Gemini, behind a provider interface

**Date**: 2026-08-20 · **Status**: accepted

**Decision.** `gemini-2.0-flash` as the default provider, with an `LLMProvider`
ABC and a deterministic `MockProvider`.

**Reasoning.** Gemini has a usable free tier, which matters for a project where
the pipeline may be run hundreds of times during development. The interface costs
~30 lines and makes provider choice a config value.

The mock is the more important half of this decision. It means the graph,
routing, self-correction loop, checkpointing, SSE streaming, and LaTeX
compilation are all runnable and testable **with no API key and no network** —
so CI can exercise the real pipeline, and a rate limit or an expired key never
blocks development. It is not a constant stub: it inspects the prompt and returns
evaluator-shaped JSON or echoes the LaTeX block, so downstream nodes receive
plausibly shaped input.

---

## ADR-004 — Deterministic guardrails before LLM evaluation

**Date**: 2026-08-20 · **Status**: accepted

**Context.** The Evaluator agent must catch hallucinated skills, broken LaTeX
structure, and poor bullet quality.

**Decision.** Run **rule-based checks first**, and call the LLM only for what
rules cannot judge.

**Reasoning.** Two of the three failure classes are decidable without a model.
"Does the generated resume claim a skill that appears nowhere in the retrieved
evidence?" is a set-difference question — and the Aho-Corasick automaton already
built for the job description can be run against the *generated resume* to answer
it exactly, at zero token cost. "Is the LaTeX preamble unchanged and are
environments balanced?" is parsing.

Only tone and bullet quality genuinely need language ability. Asking a model to
verify factual grounding would mean trusting a hallucination-prone component to
detect hallucination.

**Consequence.** The anti-hallucination guarantee is deterministic and testable —
a hallucinated fixture is caught with no LLM call at all.

---

## ADR-005 — Dev-mode JWT instead of Google OAuth for the MVP

**Date**: 2026-08-20 · **Status**: accepted, revisit at Part 20

**Decision.** Real JWT middleware, with a dev-mode issuer that mints a token for
a single seeded user. No Google OAuth in the MVP.

**Reasoning.** OAuth is well-understood plumbing that touches nothing else in the
system. It would consume roughly a day and improve none of the three resume
bullets. Because the middleware, the `userId` threading, and the per-user data
isolation are all built for real, adding Google as an issuer later is a contained
change rather than a refactor.

**Cost accepted.** FR-01 is not met in the MVP. This is stated explicitly rather
than quietly skipped.

---

## ADR-006 — Hand-rolled RAKE instead of `rake-nltk`

**Date**: 2026-08-20 · **Status**: accepted

**Decision.** Implement RAKE in-repo (~40 lines) rather than depend on
`rake-nltk`.

**Reasoning.** `rake-nltk` requires an NLTK corpus download. That is either a
network fetch during `docker build` — which makes the build non-reproducible and
fails behind a proxy — or a first-request download in production, which is worse.
RAKE is stopword-delimited phrase extraction with degree/frequency scoring; the
algorithm is short enough that owning it is cheaper than owning the dependency.

**Cost accepted.** A hand-tuned stoplist rather than NLTK's curated one. RAKE
only uses stopwords to find phrase boundaries, so precision there matters far
less than it would for a scoring model.

---

## ADR-007 — One flow page, not a chat UI

**Date**: 2026-08-20 · **Status**: accepted

**Decision.** A single wizard-style page (URL → keywords → evidence → diff → PDF)
instead of the ChatGPT-style interface with a session sidebar.

**Reasoning.** The interaction is genuinely a wizard: fixed steps, in order, with
two approval gates. Rendering it as a chat transcript adds message threading,
streaming-token UI, and session management to express a flow that has no
branching conversation in it. The two human-in-the-loop interrupts are better
served by purpose-built UI (a keyword checklist, a side-by-side diff) than by a
text box.

**Cost accepted.** FR-08 is deferred. Session persistence still exists in the
database, so a history view is additive later.

---

## ADR-008 — Test target excludes Chromium and TeX Live

**Date**: 2026-08-20 · **Status**: accepted

**Decision.** The AI service Dockerfile has three stages; the `test` stage
inherits `base` and adds only dev dependencies, while `runtime` adds Playwright's
Chromium and TeX Live.

**Reasoning.** Chromium plus a minimal TeX Live is well over 1GB. Unit tests do
not need either. Splitting the stages means CI test feedback does not wait on a
multi-gigabyte install, and the layer is cached separately from application code
so a source change never re-triggers it.

**Consequence.** Tests that genuinely need a browser or `pdflatex` must run
against the `runtime` target and are marked as integration tests.
