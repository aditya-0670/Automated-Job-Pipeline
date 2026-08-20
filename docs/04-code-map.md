# ResumeForge — Code Map

> **Version**: 1.0.0 · **Updated**: 2026-08-20
> One line per file: what it implements and why it exists.
> ✅ = written and tested · ⬜ = planned (with the build-plan part that creates it)

---

## Repository layout

```
job-automation-project/
├── docs/                      # this documentation set
├── services/
│   ├── ai/                    # Python · FastAPI · LangGraph   (the pipeline)
│   ├── api/                   # TypeScript · Express · Prisma  (the gateway)
│   └── web/                   # TypeScript · Next.js           (the UI)
├── infra/
│   ├── k8s/                   # kind cluster + manifests       (Part 19)
│   ├── jenkins/               # Jenkins controller             (Part 18)
│   └── aws/                   # EC2 provisioning               (Part 20)
├── .github/workflows/         # GitHub Actions                 (Part 17)
├── scripts/                   # dev helper scripts
├── docker-compose.yml         # full stack                     (Part 16)
├── .env.example               # every variable the stack reads
└── resume-defend.md           # interview prep (root, intentionally)
```

---

## `services/ai` — the AI pipeline

### Root

| File | Status | Implements |
|---|---|---|
| `Dockerfile` | ✅ | 3 stages. `base` = Python 3.12 + deps. `test` = adds dev deps, **no Chromium or TeX Live**, so CI test feedback is fast. `runtime` = adds Playwright Chromium + minimal TeX Live, non-root uid 10001, healthcheck. |
| `requirements.txt` | ✅ | Pinned runtime deps. Every version exact — an unpinned transitive bump is how a reproducible build stops being reproducible. |
| `requirements-dev.txt` | ✅ | pytest, pytest-asyncio, ruff. |
| `pytest.ini` | ✅ | `asyncio_mode=auto`, `pythonpath=.`, quiet output. |
| `.dockerignore` | ✅ | Keeps caches, venvs, `.env` and the Dockerfile itself out of the build context. |

### `app/` — service core

| File | Status | Implements |
|---|---|---|
| `config.py` | ✅ | `Settings` via pydantic-settings. Holds the `llm_configured` property that decides real-provider vs mock, and `psycopg_dsn` because LangGraph wants a libpq DSN. No secret has a real default. |
| `main.py` | ⬜ P11 | FastAPI app, lifespan startup (build automaton + checkpointer once), internal-key dependency, SSE endpoints. |
| `diff.py` | ⬜ P8 | Section-level before/after diff for the review UI. |

### `app/extraction/` — deterministic keyword extraction ✅ (Part 1)

The heart of the cost argument. Zero LLM tokens in this entire package.

| File | Status | Implements |
|---|---|---|
| `aho.py` | ✅ | `TaxonomyMatcher` — builds an Aho-Corasick automaton over the skill taxonomy once, then finds every skill in one O(n+z) pass. `SkillMatch` carries the canonical name, the surface form seen, and offsets. Handles **alias normalisation** (`k8s`→`Kubernetes`), **word-boundary validation** (so `R` doesn't match inside `React`), and **longest-match-wins** (so `spring boot` doesn't also yield `spring`). `get_matcher()` is an `lru_cache` singleton — build cost paid once per process. |
| `naive.py` | ✅ | The O(n·m) baseline: one full text scan per pattern. Exists so the performance claim is a measurement. Applies *identical* boundary and longest-match rules, which is what makes the comparison fair — enforced by an equivalence test. |
| `statistical.py` | ✅ | Layer 1. `extract_yake()` wraps YAKE (position/frequency/context scoring, good at single domain terms). `extract_rake()` is RAKE implemented in-repo — stopword-delimited candidate phrases scored by word degree/frequency, good at multi-word phrases. Hand-rolled rather than `rake-nltk` so the image needs no build-time corpus download. Both normalise to "higher score is better". |
| `sections.py` | ✅ | Layer 3. Regex heading detection partitions a posting into weighted sections (`requirements` 2.0 → `about` 0.3). `SectionIndex` gives O(log s) offset→section lookup so weighting a match list is cheap. Falls back to one `unknown` section for unstructured text. |
| `pipeline.py` | ✅ | Layer fusion. Taxonomy hits enter at base score 10, statistical candidates at 1×confidence, a term found by **both** layers gets a corroboration bonus — then everything is multiplied by its section weight and ranked. Emits `stats.llm_tokens_used = 0`, which is asserted in tests. |

### `app/clients/` — external I/O

| File | Status | Implements |
|---|---|---|
| `scraper.py` | ✅ | 3-tier scraping with graceful degradation. Tier 1 HTTP+BeautifulSoup, Tier 2 Playwright Chromium (**lazily imported and launched** — a browser costs ~2-3s and ~300MB, wasteful for the 80% of pages that don't need it), Tier 3 raises `ScrapeError` carrying a user-facing "paste it manually" message. Known JS-only hosts (LinkedIn, Workday, Indeed) skip straight to Tier 2. `_clean()` strips noise tags but **preserves newlines**, because section detection depends on line starts. |
| `llm.py` | ✅ | `LLMProvider` ABC + `GeminiProvider` (official `google-genai` SDK, native JSON mode, tenacity retry over 4 attempts for 5xx/429) + `MockProvider` + `TokenLedger`. Tracks **thinking tokens separately** from output tokens — the LangChain wrapper reported them as 0, which would have silently broken the token-cost claim (ADR-010, ADR-011). `LLMTruncatedError` distinguishes an exhausted output budget (which yields *no* text on thinking models) from a retryable failure. The mock is not a constant stub — it inspects the prompt and returns evaluator-shaped JSON or echoes back the LaTeX block, so downstream nodes receive plausibly shaped input and stay genuinely testable. `complete_json()` tolerates markdown fences and brace-hunts as a last resort. `get_llm()` degrades to the mock when no key is set. |

### `app/graph/`, `app/agents/`, `app/guardrails/`, `app/compile/` ⬜

| File | Part | Implements |
|---|---|---|
| `graph/state.py` | P2 | `ResumeForgeState` TypedDict — the object every node reads and writes. |
| `graph/steps.py` | P2 | Lifecycle enum, single source of truth for UI progress. |
| `graph/builder.py` | P2 | `StateGraph` construction, edges, `interrupt_before=["human_review"]`. |
| `graph/routing.py` | P7 | `route_after_evaluation()` / `route_after_human_review()` — the self-correction loop. |
| `graph/checkpointer.py` | P10 | `PostgresSaver` wiring; `thread_id` = session id. |
| `agents/scraper_keyword.py` | P3 | Node 1 — scrape then extract (wires Part 1 code). |
| `agents/data_retriever.py` | P4 | Node 2 — rank profile evidence against keywords. |
| `agents/refactorer.py` | P5 | Node 3 — the LLM rewrite, plus correction mode. |
| `agents/evaluator.py` | P6 | Node 4 — rules first, LLM only for what rules can't judge. |
| `agents/human_review.py` | P8 | Node 5 — the interrupt node. |
| `matching/profile_index.py` | P4 | A *second* automaton, over the user's profile text. |
| `guardrails/structural.py` | P6 | Deterministic LaTeX structure preservation checks. |
| `guardrails/factual.py` | P6 | Deterministic anti-hallucination: every claimed skill must trace to evidence. |
| `compile/latex.py` | P9 | `pdflatex` sandbox — timeout, non-root, no network, no shell escape. |
| `compile/sanitize.py` | P9 | Dangerous-primitive denylist. |
| `prompts/*.py` | P5-6 | Prompt templates, versioned as code. |

### `data/` and `tests/`

| File | Status | Implements |
|---|---|---|
| `data/skill_taxonomy.json` | ✅ | 148 canonical skills, 489 surface patterns, 9 categories. Each entry has a category and aliases. Deliberately includes the Salesforce stack (Apex, SOQL, LWC, Visualforce). Growing this file costs automaton **build** time, not query time. |
| `tests/fixtures/sample_jd.txt` | ✅ | A realistic Salesforce SMTS posting with all five section types, used as the shared fixture. |
| `tests/test_aho.py` | ✅ | 20 tests: pattern count, canonical + alias matching (8 parametrised cases), alias flagging, **word-boundary false-positive prevention**, single-letter skills matching when standalone, longest-match-wins, **equivalence with the naive baseline**, real-JD recall of 26 expected skills, empty input, reuse stability, singleton caching. |
| `tests/test_extraction_pipeline.py` | ✅ | 16 tests: zero tokens asserted, <300ms, ranking order, taxonomy outranks statistical noise, `requirements` outranks `preferred`, corroboration, categories populated, aliases not duplicated, max-keywords clipping, empty input, serialisation, and section detection on the real fixture. |
| `tests/test_benchmark.py` | ✅ | 3 measurements (not correctness): Aho vs naive on a ~4.8k-word posting; query time vs pattern count; and a scaling curve growing the pattern set 489→1821 with synthetic non-matching patterns to isolate the pattern-count variable. |

---

## `services/api` — Express gateway ⬜ (Parts 12-14)

| File | Part | Implements |
|---|---|---|
| `prisma/schema.prisma` | P12 | `User`, `WorkExperience`, `Project`, `Skill`, `Education`, `ChatSession`, `ChatMessage`. |
| `prisma/seed.ts` | P12 | One real profile so the pipeline runs on genuine data. |
| `src/middleware/auth.ts` | P13 | JWT verification; dev-mode issuer for the seeded user. The Google OAuth seam. |
| `src/services/aiClient.ts` | P13 | Enrich → forward → relay SSE. The only code that knows the AI service exists. |
| `src/services/github.ts` | P14 | Repo/language/README sync via PAT, with TTL and ETag handling. |
| `src/routes/*` | P13 | profile CRUD, sessions, review, stream, download. |

## `services/web` — Next.js UI ⬜ (Part 15)

| File | Part | Implements |
|---|---|---|
| `app/page.tsx` | P15 | The single flow: URL → keywords → evidence → diff → PDF. |
| `app/profile/page.tsx` | P15 | Experiences, skills, LaTeX template editor. |
| `components/ProgressStream.tsx` | P15 | SSE consumer driving the step indicator. |
| `components/LatexEditor.tsx` | P15 | Monaco editor + PDF preview. |

## `infra/` and CI ⬜ (Parts 16-20)

| File | Part | Implements |
|---|---|---|
| `docker-compose.yml` | P16 | 5 services, healthchecks, ordered boot, internal network. |
| `.github/workflows/ci.yml` | P17 | Path-filtered lint → typecheck → test → build matrix. |
| `.github/workflows/release.yml` | P17 | Tag → GHCR push with SHA + semver tags. |
| `.github/workflows/deploy.yml` | P20 | main → build → SSH to EC2 → health-gated rollout with rollback. |
| `Jenkinsfile` | P18 | Declarative pipeline mirroring CI, with published JUnit results. |
| `infra/k8s/base/*` | P19 | Deployments, Services, Ingress, StatefulSet + PVC, HPA, probes, limits. |
| `infra/k8s/kind-cluster.yaml` | P19 | Control plane + 2 workers, ingress port mappings. |
| `infra/aws/*` | P20 | Security groups, user-data bootstrap, reverse proxy + TLS. |
