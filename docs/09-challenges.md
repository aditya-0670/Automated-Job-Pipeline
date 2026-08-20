# ResumeForge — Challenges & How They Were Solved

> **Version**: 1.0.0 · **Updated**: 2026-08-20
> **Purpose**: every non-trivial problem hit while building this, what caused it,
> and how it was fixed. Written to be *said out loud* in an interview.
>
> These are real. Each one has a commit behind it, and most have a test that
> exists specifically because the bug happened. That is the difference between
> "I used Aho-Corasick" and "I debugged Aho-Corasick".

---

## How to use this document

Interviewers rarely reward a description of a happy path. What earns credibility
is: *here is what went wrong, here is how I found it, here is why my fix is
correct.* Each challenge below is written in that order.

The strongest three, if you only memorise three:

| # | Challenge | Why it lands |
|---|---|---|
| **1** | [The algorithm that lost to the baseline](#challenge-1--aho-corasick-was-slower-than-the-naive-baseline) | Shows you measure, profile, and know the difference between asymptotics and constant factors |
| **4** | [A silent taxonomy ambiguity](#challenge-4--a-silently-wrong-automaton-ambiguous-taxonomy-patterns) | Shows a test caught a *correctness* bug that produced no error |
| **7** | [Chromium the image contained but could not run](#challenge-7--the-image-contained-chromium-and-could-not-launch-it) | Shows you understand containers, users, and why test coverage had a structural blind spot |

---
---

# Part A — Algorithms & correctness

## Challenge 1 — Aho-Corasick was *slower* than the naive baseline

**Symptom.** The first benchmark had the automaton at **22.6ms** against a naive
one-scan-per-pattern baseline at **10.3ms**. Worse, it got *slower* as patterns
were added — the exact opposite of the algorithm's guarantee.

**Why this was alarming.** Aho-Corasick is O(n + z) per query, independent of
pattern count. If measurements contradict the theory, either the theory is
misapplied or the implementation is wrong. It was the implementation.

**Diagnosis.** Two causes, neither algorithmic:

1. **O(k²) deduplication.** Longest-match-wins filtering compared each candidate
   against every already-kept match. On a repetitive posting the automaton
   reports thousands of hits, so this quadratic step dominated everything else.
2. **Allocation per raw hit.** A frozen dataclass was constructed for *every*
   reported match, before filtering. `pyahocorasick` scans in C but yields to
   Python on every match — so per-match Python overhead swamped the C-speed
   scan, while the naive baseline's `str.find` loop stayed entirely inside C.

**Fix.** Keep raw hits as plain tuples so only survivors pay for object
construction, and replace the containment scan with a single linear sweep: after
sorting by start offset, every already-kept span begins at or before the current
one, so containment collapses to one comparison against the furthest end seen.

**Result.** 22.6ms → **2.8ms**, and the pattern-count curve went flat.

| Patterns | Aho-Corasick | Naive | Speedup |
|---|---|---|---|
| 492 | 2.88 ms | 9.60 ms | 3.3× |
| 933 | 2.84 ms | 13.41 ms | 4.7× |
| 1,821 | 2.91 ms | 20.63 ms | **7.1×** |

**The line to say:** *"Better asymptotic complexity doesn't survive a constant
factor paid inside the inner loop. The algorithm was never the problem — my
integration was."*

---

## Challenge 2 — Word boundaries: `R` matched inside `React`

**Symptom.** The taxonomy contains single-letter skills (`R`, `C`) and short ones
(`Go`, `Java`). `pyahocorasick` matches **raw substrings**, so a naive
integration found `R` in "React", `Go` in "Google", `C` in "Customer", and `Java`
in "JavaScript". Every posting produced phantom skills.

**Why the obvious fix is wrong.** Wrapping patterns in `\b` regex boundaries
throws away the whole point of the automaton (one pass, no regex engine). And
treating every non-alphanumeric as a boundary breaks real skill names: `c++`,
`.net`, `node.js`, `ci/cd`.

**Fix.** Validate boundaries *after* the scan, on the match list — which is tiny
compared with the text. A match is rejected if either side is `[a-z0-9_]`.
Crucially `-`, `.`, `+` and `/` are **excluded** from the boundary set, because
they appear inside legitimate skill names.

**Guarded by** eight parametrised tests, plus one asserting that `R` and `C`
*do* match when they stand alone — over-correcting would be its own bug.

---

## Challenge 3 — `spring boot` also matched `spring`

**Symptom.** The automaton reports *every* pattern ending at each position, so a
posting saying "Spring Boot" yielded both `Spring Boot` and `Spring`.

**Fix.** Longest-match-wins: discard any match fully contained inside a longer
one. Implemented as the linear sweep from Challenge 1, so the fix for correctness
and the fix for speed are the same code.

---

## Challenge 4 — A silently wrong automaton: ambiguous taxonomy patterns

**This is the best correctness story in the project.**

**Symptom.** After adding `Containerization` as a canonical skill (needed as an
implication target, Challenge 5), one test failed:
`test_matches_are_equivalent_to_naive_baseline`. The automaton found
`Containerization` and `Container Orchestration`; the exhaustive baseline did not.

**Root cause.** `containerization` was already an **alias of Docker**. Two
canonical entries claimed the same surface form. `pyahocorasick.add_word` with a
duplicate key **silently overwrites** the payload — last insertion wins — whereas
the naive scan iterates every entry and finds both. So the automaton was
returning an arbitrary answer depending on dictionary insertion order, and
keywords were being mislabelled **with no error raised anywhere**.

**Why it mattered more than it looked.** Nothing crashed. Extraction still
returned plausible keywords. A resume would have been tailored against a
mislabelled skill set, and the only symptom would have been slightly-wrong
output — the worst kind of bug.

**Fix.** Two layers:
1. The matcher now **raises at build time** if two canonical skills claim the
   same pattern. Fail loudly at startup, never silently at query time.
2. Resolved the six real collisions found once looked for — `infrastructure as
   code` (Terraform vs the concept), `test automation` (Unit Testing vs the
   concept), `concurrency` (Multithreading vs the concept), and two harmless
   self-collisions. The general rule adopted: **a generic concept must not be an
   alias of one specific tool that happens to provide it.**

**Guarded by** tests on both the code path and the shipped data file, so the
data cannot drift into ambiguity either.

**The line to say:** *"The equivalence test between my optimised matcher and a
naive baseline existed to make the benchmark fair. It ended up catching a
correctness bug that produced no error message — which is the real argument for
keeping a slow reference implementation around."*

---

## Challenge 5 — A flat taxonomy reported false skill gaps

**Symptom.** Running the retriever against the real profile, the "skills this
posting wants that you cannot evidence" list included **Message Queue** — for a
profile that lists Kafka — and **CI/CD**, for a profile whose project describes
GitHub Actions. Both are wrong, and both *understate the candidate*, which for
this product is the worst possible direction to be wrong in.

**Root cause.** The taxonomy was a flat set of independent skills. Nothing
encoded that Kafka *is* a message queue, or that GitHub Actions *is* CI/CD.

**Fix.** Taxonomy entries gained an `implies` list — 65 of them. Profile evidence
expands along implications at a **0.7 discount**, recording `implied_by` so the
UI shows the reasoning rather than asking the user to trust it.

Three deliberate constraints:

| Constraint | Reason |
|---|---|
| Expansion applies to **profile evidence only**, never job-description extraction | A posting asking for Kubernetes is *not* asking for Docker. Expanding the JD side would invent requirements. |
| **One level**, no transitive closure | `Next.js → React → JavaScript` chains drift far from the original evidence, and the further a claim sits from a real mention the harder it is to defend to a recruiter. |
| Implication must be a fact about the **technology**, not an inference about the **person** | "You cannot orchestrate containers without containers" is a fact. "They use AWS so they probably know Terraform" is a guess. |

**Result.** Message Queue, CI/CD, Database Design, Distributed Systems and
Containerization all correctly recovered. The remaining gaps (Kubernetes,
Jenkins, Salesforce, Apex, Spring Boot, Terraform, Prometheus, Grafana) are
genuinely absent — which is useful, honest information.

---

## Challenge 6 — Preventing hallucination without trusting a model to detect it

**The problem.** The core promise is that the resume never claims something the
user cannot back up. The obvious design — ask the LLM to check its own output —
means trusting a hallucination-prone component to detect hallucination.

**Fix: make it a set-membership test.** The same Aho-Corasick automaton that
reads the job description is run against the **generated resume**. Every skill it
finds must appear in the evidence set the retriever produced from the profile.
Set difference, zero tokens, exactly reproducible.

The LLM is confined to the two steps that genuinely need language ability —
rewriting prose, and judging tone and bullet quality. Factual grounding and
structural preservation are decided by rules.

**The line to say:** *"Two of the three failure classes are decidable without a
model. 'Does the output claim a skill with no supporting evidence?' is a set
difference. 'Is the LaTeX preamble unchanged and are environments balanced?' is
parsing. Only tone needs a model. So I don't ask a model to verify facts."*

---
---

# Part B — LLM integration

## Challenge 7 — The pinned model did not exist

**Symptom.** The config specified `gemini-2.0-flash`, taken from the design docs.
Querying the live models endpoint with a real key: it is **retired**. And
`gemini-2.5-flash` returns `404 — no longer available to new users`.

**Fix.** Verify model IDs against `GET /v1beta/models` rather than trusting
documentation or memory. Pinned `gemini-3.7-flash` explicitly rather than the
`gemini-flash-latest` alias, so a model cannot change underneath a working
system.

**Also observed:** `gemini-3.7-flash` returned `503 UNAVAILABLE` ("high demand")
on two separate probes during a single session. Transient overload is a **normal
operating condition**, not an exception — which is why the provider retries 5xx
and 429 with exponential backoff over four attempts.

**The line to say:** *"Model lifecycles are short and retirement is silent from
the code's point of view — it surfaces as a 404 at runtime, not a failure at
build time."*

---

## Challenge 8 — The SDK reported a wrong token count

**Symptom.** `langchain-google-genai` reported `output_tokens: 0` for a call that
actually consumed 94 tokens.

**Root cause.** Gemini 3.x models emit **reasoning tokens** that are billed as
output but are *not* included in `candidates_token_count`. The wrapper read only
that field and dropped the rest.

**Why it was disqualifying.** This project's headline claim is token cost. A
silently wrong token counter makes every cost number in it unverifiable.

**Fix.** Dropped the LangChain wrapper — LangGraph does not require it — and used
the official `google-genai` SDK directly. `LLMResponse` now carries
`thinking_tokens` as its own field, with `billed_output_tokens` combining them.
The wrapper also still imports the **deprecated** `google.generativeai` package.

**Measured, worth quoting:** a one-token answer costs **72 thinking tokens** by
default on `gemini-3.7-flash` (116 on 3.6-flash). With an explicit
`thinking_budget=128`, the same prompt costs **11 total tokens instead of 65**.
Curiously `thinking_budget=0` does *not* disable reasoning — a small positive
value does.

**Second-order consequence.** With thinking models, an exhausted output budget
produces **no text at all** rather than truncated text, because reasoning
consumes the allowance first. So `max_output_tokens` is set generously (16,384),
and a dedicated `LLMTruncatedError` distinguishes this from a transient failure —
retrying an under-budgeted request unchanged would fail identically.

---

## Challenge 9 — Building an LLM pipeline that runs without an LLM

**The problem.** The graph, routing, self-correction loop, checkpointing and SSE
all needed to be built and tested *before* a working API key existed — and CI
must not depend on one afterwards.

**Fix.** A `MockProvider` behind the `LLMProvider` interface. Not a constant
stub: it inspects the prompt and returns evaluator-shaped JSON or echoes back the
LaTeX block, so downstream nodes receive plausibly shaped input and remain
genuinely testable.

**The payoff, which is bigger than the workaround.** CI runs with **no**
`GEMINI_API_KEY` at all. Live-API tests mark themselves `integration` and skip.
So a rotated key, an exhausted quota, or a 503 from Google **can never turn the
build red** — a red build always means the code is broken.

---

## Challenge 10 — Enum in checkpointed state

**Symptom.** Running the graph printed:
`Deserializing unregistered type app.graph.steps.Step from checkpoint. This will
be blocked in a future version.`

**Root cause.** `current_step` held a `Step` enum instance. LangGraph serialises
state with msgpack and will refuse unregistered types in a later release — a
latent, version-dependent failure.

**Fix.** Store the plain string. `Step` is a `StrEnum`, so
`state["current_step"] == Step.SCRAPING` still reads naturally and
`Step(value)` recovers the enum. A regression test asserts the **checkpointed**
type directly, not just the in-memory one.

**Generalised rule adopted:** everything in graph state must be JSON-native. No
dataclasses, no sets, no datetimes, no enums. Domain objects convert to dicts at
the node boundary.

---
---

# Part C — Infrastructure

## Challenge 11 — The image contained Chromium and could not launch it

**Symptom.** The production image built fine, and Playwright inside it printed
*"please run `playwright install`"* — from an image whose entire build step was
`playwright install`.

**Root cause.** `playwright install` ran as **root**, so Chromium landed in
`/root/.cache/ms-playwright`. The service runs as **`appuser`**, whose `HOME` is
different. The browser was present and unreachable.

**Fix.** Pin `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` before installing, and
`chmod -R a+rX` it so any user can read it.

**The deeper problem — a structural test blind spot.** Nothing in the fast test
suite could have caught this, because the fast `test` image *deliberately* has no
Chromium and no TeX Live (that split keeps CI test feedback off a multi-gigabyte
install). So the browser path was untested by construction.

**Fix for the blind spot.** A fourth Docker stage, `runtime-test` = the real
runtime image plus pytest. Twelve tests now verify Chromium launches and
`pdflatex` compiles **as the non-root service user**, and CI runs them as their
own job.

**The line to say:** *"The bug was a one-line environment variable. The real
lesson was that my test pyramid had a hole exactly where the expensive
dependencies lived — so I added a stage whose only job is to test the image I
actually ship."*

---

## Challenge 12 — 338MB of image for nothing

**Symptom.** The base image carried `build-essential`, added "to compile
pyahocorasick's C extension".

**Diagnosis.** Checking the build log: pip downloaded
`pyahocorasick-…-manylinux…whl`. Every pinned dependency ships a prebuilt wheel
for CPython 3.12. Nothing was ever compiled.

**Fix.** Removed it. Test image **1.26GB → 837MB, a 34% reduction** — on a layer
CI pulls on every run.

**The principle:** a compiler in a production image is both dead weight and
attack surface. If a dependency genuinely needs building, the answer is a builder
stage that produces a wheel and a runtime stage that copies it forward — not
shipping a toolchain to production.

---

## Challenge 13 — LaTeX packages: a 624MB decision

**Symptom.** The real resume template (`\usepackage{fontawesome5}`) would not
compile in the image. `fontawesome5` and `moderncv` live in
`texlive-fonts-extra` — **624MB compressed**, on an image already near 3GB.

**Why there is no clever fix.** A missing `.sty` is a hard compile failure. You
cannot degrade gracefully out of a package the document requires.

**Decision.** Install it, because the actual template needs it. The package set
is now driven by parsing the real template's `\usepackage` lines rather than
guessed:

```
latex-base / recommended   geometry, fontenc, inputenc, lmodern, babel
latex-extra                titlesec, enumitem, fancyhdr, tabularx, xcolor
fonts-extra                fontawesome5
```

**Before the template was available**, the gap was pinned by a test asserting the
packages were *absent* — so that adding them later would fail the test and force
the documentation to be updated, rather than the gap quietly closing and the docs
going stale.

---

## Challenge 14 — SELinux broke every bind mount

**Symptom.** On Fedora, `docker run -v "$PWD":/app` produced
`PermissionError: [Errno 13] Permission denied: '/app/pytest.ini'` for every file.

**Root cause.** SELinux labels. A host directory is not labelled for container
access by default.

**Fix.** The `:z` mount flag, which relabels the volume as shared. It appears in
the Makefile, the compose override and the docs, because the failure message
names a file rather than the actual cause and is genuinely confusing the first
time.

---

## Challenge 15 — "Started" is not "ready"

**Symptom.** Services racing at startup — the AI service attempting to connect
before Postgres could serve.

**Root cause and fixes.** Three distinct issues, each with its own lesson:

1. **`depends_on` without a condition only waits for *start*.** A started
   Postgres container is not a ready one: `initdb` runs after the process begins
   accepting connections. Fixed with `condition: service_healthy`.
2. **`pg_isready` needs `-U` and `-d`.** Without them it checks the *default*
   database and reports healthy while ours is still being created.
3. **A short `start_period` restart-loops a slow service.** Chromium and TeX Live
   make the AI container slow to boot; a healthcheck grace period of 40s stops
   Docker killing a container that was going to become healthy.

**Related design point — liveness vs readiness.** `/health` deliberately checks
**no** dependencies; `/ready` reports whether startup finished building the
automaton. Conflating them is the classic mistake: a liveness probe that checks
the database turns a brief database blip into a cascading restart storm, because
restarting the process cannot fix a downed database.

---

## Challenge 16 — Every dependency pin was stale, one by a major version

**Symptom.** Pins written from memory: `langgraph==0.2.62` when current was
**1.2.11**; `langchain-google-genai==2.0.8` when current was 4.3.4;
`yake==0.4.8` when current was 0.7.3.

**Why it mattered.** LangGraph 1.x has a different API surface than 0.2. Writing
the entire graph against the 0.2 API would have been **a day of wasted work**
discovered late.

**Fix.** Verify versions against the index *before* writing code against a
library, then confirm the specific API surface needed actually exists
(`StateGraph`, `interrupt`, `Command`, `PostgresSaver`, `AsyncPostgresSaver`).
All 40 extraction tests passed unchanged after the upgrade, which is what made
the upgrade safe to do in one step.

---

## Challenge 17 — Python 3.14 on the host, no wheels

**Symptom.** The development machine runs Python 3.14. `pyahocorasick` and `yake`
have no wheels for it, and no compiler was installed.

**Fix.** Never run the service on the host. The container pins 3.12 and is the
single source of truth for the environment; the Makefile has no "local
virtualenv" target on purpose.

This is the honest, specific answer to *"why Docker?"* for this project — not
"consistency" in the abstract, but: the host interpreter is wrong, Playwright
needs a specific Chromium plus a long list of shared libraries, and TeX Live is a
2GB install with OS-specific quirks that must produce byte-identical PDFs in dev
and prod.

---
---

# Part D — Judgement calls that are not bugs

These are questions an interviewer may push on. Each has a defensible answer and
a documented trigger for revisiting it — see
[07-decision-log.md](./07-decision-log.md).

| Question | Short answer |
|---|---|
| **"Why no Kafka?"** | Durability comes from checkpointing state after every node, which is strictly better than a message a consumer must rebuild state from. The flow is interactive, so there is nothing to defer. Replay is served by checkpoint inspection. The topic schema was designed (`resume-jobs`, `resume-pipeline-events`, `resume-status`, `resume-dlq`, partitioned by `user_id`); the broker was not justified at single-user concurrency. Trigger to revisit: multi-tenant concurrent queueing, or work that must outlive the request. |
| **"Monorepo or microservices?"** | Three services, three images, three languages, one repository. The contracts are real — separate processes over HTTP with an API-key trust boundary. Path-filtered CI recovers polyrepo's "don't rebuild everything" without losing atomic cross-contract commits. |
| **"Is the AI service exposed?"** | No. Only the gateway can reach it, authenticated with a shared internal key compared using `compare_digest`. Two Docker networks so the browser-facing container cannot address the database at all. |
| **"How is 'distributed execution' true?"** | Precisely: the AI service holds no session state — it lives in Postgres keyed by `thread_id` — so any replica can serve any session. That is horizontal scalability by statelessness. It is *not* a claim about consensus or distributed transactions, and it is worth saying so before being asked. |
| **"Why is extraction deterministic instead of an LLM?"** | Cost, latency, and auditability — but mainly determinism. Resume content decisions are made downstream from these keywords, so a 98% extractor means silently different resumes for identical input. A taxonomy hit is explainable: "matched alias `k8s` at offset 1423, inside the requirements section." |

---
---

# Appendix — Bugs found by tests that exist for another reason

A recurring theme worth having ready, because it demonstrates how the test suite
was designed rather than just that it exists.

| Test | Written to prove | Actually caught |
|---|---|---|
| `test_matches_are_equivalent_to_naive_baseline` | That the benchmark is a fair comparison | A silent taxonomy ambiguity mislabelling keywords (Challenge 4) |
| `ruff` lint gate in CI | Code style | A genuine `B023` closure-capture bug in the benchmark — loop variables captured instead of bound |
| Running the graph end to end with stubs | That routing works | An enum in checkpointed state that would break on a future LangGraph release (Challenge 10) |
| The `/ready` probe | Orchestrator health reporting | That the service starts fine with an unreachable database, so degradation had to be reported rather than assumed |

**The line to say:** *"The most valuable tests I wrote caught bugs they weren't
written for."*
