# ResumeForge — Keyword Extraction Deep Dive

> **Version**: 1.0.0 · **Updated**: 2026-08-20 · **Status**: implemented ✅
> Covers `services/ai/app/extraction/*`. This is the subsystem behind resume
> bullet 3, so it documents the *measurements*, not just the design.

---

## 1. Why not just ask an LLM?

| Reason | Detail |
|---|---|
| **Determinism** | If "Kubernetes" appears in a posting it must be extracted 100% of the time. Resume content decisions are made downstream from these keywords; a 98% extractor means silently different resumes for identical input. |
| **Cost** | Extraction is per-posting and unavoidable. At ~2,000 input tokens per JD it is pure recurring spend for a task that has an exact algorithm. |
| **Latency** | An API round trip is 2-5s. The automaton is ~3ms. |
| **Auditability** | A taxonomy hit is explainable — "matched alias `k8s` at offset 1423, inside the `requirements` section". A model's answer is not. |

The trade-off is honest and worth stating in an interview: **the taxonomy only
knows what it knows.** A framework released last week is invisible to Layer 2.
That is precisely why Layer 1 (statistical) and Layer 4 (human confirmation)
exist — see §6.

---

## 2. The four layers

```
job description text
        │
        ├──▶ Layer 1  YAKE + RAKE ensemble          catches unknown terms
        │              statistical, no vocabulary
        │
        ├──▶ Layer 2  Aho-Corasick taxonomy scan    catches known skills at
        │              489 patterns, one pass       any frequency + normalises
        │                                            aliases
        ▼
     Layer 3  Section-aware weighting                ranks by where it appeared
        │
        ▼
     Layer 4  User confirmation (add/remove)          final recall backstop
        │
        ▼
   ranked keyword set  ·  0 LLM tokens
```

### Layer 1 — statistical ensemble (`statistical.py`)

Two algorithms with near-complementary blind spots:

- **YAKE** scores terms by position, frequency, and context spread. Strong on
  important *single-word* domain terms. Configured `n=2` (unigrams + bigrams)
  with `dedupLim=0.85`.
- **RAKE** splits text on stopwords to form candidate phrases, then scores each
  word as `(degree + freq) / freq` and a phrase as the sum of its words. Strong on
  *multi-word* technical phrases like "distributed systems".

RAKE is implemented in-repo (~40 lines) rather than taken from `rake-nltk`,
because `rake-nltk` requires downloading an NLTK corpus — a network fetch at
build or first-run time is a reproducibility hazard in a container.

Both are normalised so "higher is better" before fusion. YAKE natively returns
ascending scores (lower = more relevant), so it is inverted.

### Layer 2 — Aho-Corasick taxonomy scan (`aho.py`)

**The algorithm.** Two phases, from Aho & Corasick (1975):

1. **Build** (once, at process start): insert every pattern into a trie, then add
   *failure links*. A failure link from a node points to the node representing
   the longest proper suffix of the current match that is also a prefix of some
   pattern. This is what allows a mismatch to resume without re-scanning input.
2. **Search** (per posting): walk the automaton one character at a time, following
   trie edges on match and failure links on mismatch. Every pattern ending at the
   current position is reported. One pass, O(n + z) where z is the match count —
   **independent of how many patterns exist.**

**Three implementation details that matter more than the algorithm:**

*Alias normalisation.* Each pattern's payload carries its canonical name, so
`k8s`, `kubectl`, `container orchestration` and `Kubernetes` all resolve to one
entry during the scan itself — no post-processing lookup table.

*Word-boundary validation.* `pyahocorasick` matches raw substrings, so a naive
integration finds `R` inside `React`, `Go` inside `Google`, `C` inside `Customer`,
and `Java` inside `JavaScript`. Every hit is boundary-checked against
`[a-z0-9_]` on both sides. Crucially, `-`, `.`, `+` and `/` are **excluded** from
the boundary set, because real skill names contain them (`c++`, `.net`,
`node.js`, `ci/cd`). Eight parametrised tests pin this behaviour.

*Longest-match-wins.* The automaton reports every pattern ending at a position,
so `spring boot` also yields `spring`. Nested shorter matches are dropped. After
sorting by start offset, containment collapses to one comparison against the
furthest end seen so far — a linear sweep.

### Layer 3 — section-aware weighting (`sections.py`)

A skill under "Minimum Qualifications" matters more than the same word in
"About Us" boilerplate. This layer extracts nothing new; it re-ranks, so that if
recall is imperfect the terms that fall off the end are the low-priority ones.

| Section | Weight |
|---|---|
| `requirements` (qualifications, must-haves, who you are) | **2.0** |
| `responsibilities` (what you'll do, the role) | **1.5** |
| `preferred` (nice-to-have, bonus, a plus) | **1.0** |
| `benefits` (perks, compensation, EEO) | **0.4** |
| `about` (about us, our mission) | **0.3** |
| `unknown` (no headings detected) | **1.0** |

Headings are matched with anchored multiline regexes; each character offset is
attributed to the most recent heading. `SectionIndex` uses `bisect` for O(log s)
lookup. Unstructured postings degrade to a single `unknown` section rather than
failing.

### Layer 4 — user confirmation

Handled at the API boundary (Part 11/15), not in this package. The user sees the
ranked keywords and can add or remove before generation. This is the ultimate
false-negative catcher and the reason imperfect automated recall is acceptable.

### Fusion (`pipeline.py`)

| Signal | Score contribution |
|---|---|
| Taxonomy hit | base **10.0** × section weight |
| Statistical only | base **1.0** × confidence × section weight |
| Found by *both* layers | **+3.0** corroboration bonus |

A confirmed skill therefore always outranks an unconfirmed statistical phrase —
asserted by `test_taxonomy_hits_outrank_statistical_noise`. Statistical
candidates below 0.35 confidence are dropped as noise.

---

## 3. Measured results

Environment: `python:3.12-slim` container, taxonomy of 148 skills / 489 patterns.

### Correctness

39 tests green. The load-bearing one is
`test_matches_are_equivalent_to_naive_baseline`: the automaton and the O(n·m)
baseline must return **the identical skill set** on the real fixture. A faster
algorithm that returns different answers is not an optimisation, and without this
test the benchmark below would be meaningless.

`test_real_jd_finds_expected_skills` asserts recall of 26 named skills from the
sample posting, including alias-only mentions (`tf` → Terraform, `LWC` →
Lightning Web Components).

### Performance — automaton vs naive

Text: 4,771 words / 32,851 chars. Mean of 50 iterations.

| | Time |
|---|---|
| Automaton build (once, at startup) | **0.8 ms** |
| Naive search (489 scans of the text) | **9.5 ms** / posting |
| Aho-Corasick search (one pass) | **2.8 ms** / posting |
| **Speedup** | **3.4×** |

### Performance — the asymptotic claim, measured

The real argument is not the constant factor, it is the *slope*. Holding the text
fixed and growing the pattern set with synthetic non-matching patterns (which
isolates pattern count from match count):

| Patterns | Aho-Corasick | Naive | Speedup |
|---|---|---|---|
| 489 | 2.88 ms | 9.60 ms | 3.3× |
| 933 | 2.84 ms | 13.41 ms | 4.7× |
| 1,821 | 2.91 ms | 20.63 ms | **7.1×** |

**The automaton is flat.** 3.7× the patterns costs it 1.0× the time and costs the
baseline 2.1×. Growing the taxonomy — which is the realistic direction, since new
tools appear constantly — is free at query time and only costs build time
(0.8ms, once per process).

### Cost

Full 4-layer extraction on the sample posting: **well under 300ms, 0 LLM tokens**,
asserted by `test_uses_zero_llm_tokens` and `test_completes_well_under_300ms`.
The LLM-based alternative is ~2,000 input tokens and 2-5s per posting.

---

## 4. A performance bug worth remembering

The first benchmark run showed Aho-Corasick at **22.6ms vs naive at 10.3ms** —
the automaton *losing* by 2×, and getting worse as the pattern set grew.

Two causes, neither algorithmic:

1. **O(k²) deduplication.** Longest-match filtering checked each candidate
   against every already-kept match. On a repetitive posting the automaton
   reports thousands of hits, so this dominated everything else.
2. **Allocation per raw hit.** A frozen dataclass was constructed for every
   reported match *before* filtering. `pyahocorasick` scans in C but yields to
   Python on every match, so per-match Python overhead swamped the C-speed scan —
   while the naive baseline's `str.find` loop stayed entirely in C.

Fixes: make the dedup a single linear sweep over start-sorted matches, and keep
raw hits as plain tuples so only survivors pay for object construction. Result:
22.6ms → 2.8ms, and the pattern-count curve went flat.

The lesson is the interview-worthy part: **a better asymptotic complexity does
not survive a constant factor paid inside the inner loop.** The algorithm was
never the problem.

---

## 5. Extending the taxonomy

`services/ai/data/skill_taxonomy.json`:

```json
"Kubernetes": {
  "category": "devops",
  "aliases": ["k8s", "k8", "kubernetes cluster", "kubectl", "container orchestration"]
}
```

Rules that matter:
- Aliases are matched case-insensitively; write them lowercase.
- Add the *surface forms a posting actually uses*, not synonyms you'd like to
  imply. `container orchestration` → Kubernetes is a judgement call that happens
  to be right in practice; `experience with containers` → Docker is a stretch.
- An alias that is a substring of common English will produce false positives.
  Boundary validation catches word-level collisions, not semantic ones.
- Adding entries is free at query time. Do not optimise for taxonomy size.

Current coverage: 148 skills across `language`, `framework`, `database`, `cloud`,
`devops`, `tool`, `concept`, `soft_skill`, `qualification`.

---

## 6. Known limitations

| Limitation | Mitigation |
|---|---|
| Taxonomy cannot match what it has never seen | Layer 1 catches high-frequency unknown terms; Layer 4 lets the user add them |
| No semantic inference — "experience with containerization" does not imply Docker | Curated aliases handle the common cases explicitly; the rest is a Layer 4 decision |
| Section detection is heuristic; unusual headings fall to `unknown` | Degrades to neutral 1.0 weight rather than failing |
| Statistical layer produces `uncategorized` terms | They rank below taxonomy hits by construction and are shown to the user for confirmation |
| English only | Out of scope for v1 per [00-problem-statement.md](./00-problem-statement.md) §5 |

---

## 7. Reproducing the numbers

```bash
cd services/ai
docker build --target test -t resumeforge-ai:test .
docker run --rm resumeforge-ai:test pytest tests/ -s          # all 39 tests
docker run --rm resumeforge-ai:test pytest tests/test_benchmark.py -s   # the tables
```

> On Fedora/SELinux, bind-mounting the source for live iteration needs a relabel
> flag: `-v "$PWD":/app:z`. Without it the container hits
> `PermissionError: /app/pytest.ini`.
