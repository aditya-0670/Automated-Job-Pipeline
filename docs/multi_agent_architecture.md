# ResumeForge — Multi-Agent Architecture (LangGraph)

> **Version**: 0.2.0
> **Last Updated**: 2026-07-29
> **Author**: Aditya
> **Supersedes**: Linear pipeline architecture from v0.1.0

---

## Table of Contents

1. [Why Multi-Agent? Why LangGraph?](#1-why-multi-agent-why-langgraph)
2. [Agent Definitions](#2-agent-definitions)
3. [LangGraph State & Graph Design](#3-langgraph-state--graph-design)
4. [The Self-Correction Loop](#4-the-self-correction-loop)
5. [GitHub Data Freshness Strategy](#5-github-data-freshness-strategy)
6. [Guardrails Framework](#6-guardrails-framework)
7. [Token Budget Across Agents](#7-token-budget-across-agents)
8. [Updated Technology Stack](#8-updated-technology-stack)
9. [Updated Project Structure](#9-updated-project-structure)
10. [Challenges & Solutions (New/Updated)](#10-challenges--solutions-newupdated)

---

## 1. Why Multi-Agent? Why LangGraph?

### Why Not a Linear Pipeline?

Our v0.1.0 design was a **linear pipeline**: Scrape → Extract → Match → Refactor → Compile. This breaks down because:

| Problem | Why Linear Fails |
|---|---|
| **Self-correction** | If the Evaluator finds a factual error, a linear pipeline has no way to loop back to the Refactorer |
| **Conditional routing** | Some steps should be skipped or repeated depending on results (e.g., if GitHub data is stale, re-sync first) |
| **Parallel processing** | Keyword extraction and GitHub data freshness checks can run simultaneously |
| **Human-in-the-loop** | Users need to approve/modify at multiple checkpoints — linear pipelines don't model interrupts well |
| **Separation of concerns** | Each agent has a single responsibility, its own prompt, and can be tested/improved independently |

### Why LangGraph?

**LangGraph** (by LangChain) is a framework for building **stateful, multi-actor applications as directed graphs**. It's the right tool because:

- **Graph-native**: Models workflows as nodes (agents/functions) and edges (transitions) — perfect for our branching, looping flow
- **Stateful**: Maintains a shared `State` object that flows through the graph — every node can read/write to it
- **Cycles**: First-class support for cycles (self-correction loops) — the Evaluator can route back to the Refactorer
- **Conditional edges**: Route to different nodes based on runtime conditions (e.g., "if factual_errors > 0 → Refactorer, else → HumanReview")
- **Human-in-the-loop**: Built-in `interrupt()` for pausing the graph and waiting for user input
- **Streaming**: Native streaming support for real-time UX
- **Checkpointing**: Can save/restore graph state for persistence, resume, and time-travel debugging
- **Python-native**: LangGraph is Python — which aligns better with our keyword extraction libraries (YAKE, RAKE are Python packages)

> **Architecture Shift**: Moving the **core pipeline to Python (FastAPI + LangGraph)** while keeping the **frontend in Next.js (TypeScript)**. The frontend communicates with the LangGraph backend via REST/SSE.

---

## 2. Agent Definitions

We define **5 specialized agents** (graph nodes), each with a single responsibility:

### Agent Overview

```mermaid
graph TD
    SK["🔍 SCRAPER & KEYWORD AGENT\n\nFetch URL | Parse JD\nYAKE+RAKE | Categorize\n\n❌ NO LLM"] --> DR

    DR["📊 DATA RETRIEVER AGENT\n\nGitHub API | Match user data\nRank items | Suggest add/replace\n\n🟡 LLM-light"] --> RR

    RR["✍️ RESUME REFACTORER AGENT\n\nLLM-powered | Template-aware\nRewrites LaTeX | Generates diff\n\n✅ PRIMARY LLM"] --> EV

    EV{"🔎 EVALUATOR AGENT\n\nFactual consistency\nGrounding verification\nLaTeX validity check\nATS score estimation\n\n🟡 LLM + Deterministic"}

    EV -->|"❌ Errors found\n(iteration < max)"| RR
    EV -->|"✅ Passed or max\niterations reached"| HR

    HR["👤 HUMAN REVIEW NODE\n\nShow preview | Show diff\nAccept/Reject | Edit LaTeX\n\n⏸️ HUMAN-IN-LOOP"]

    style SK fill:#2d5016,stroke:#4a8c28,color:#fff
    style DR fill:#1a3a5c,stroke:#2980b9,color:#fff
    style RR fill:#5c1a5c,stroke:#8e44ad,color:#fff
    style EV fill:#5c3a1a,stroke:#e67e22,color:#fff
    style HR fill:#1a5c5c,stroke:#1abc9c,color:#fff
```

---

### Agent 1: Scraper & Keyword Agent

| Property | Details |
|---|---|
| **Name** | `scraper_keyword_agent` |
| **Responsibility** | Fetch job URL → extract text → extract & categorize keywords |
| **LLM Usage** | ❌ **None** — purely algorithmic |
| **Inputs** | `job_url` from user |
| **Outputs** | `job_text`, `keywords` (categorized), `job_metadata` (title, company, location) |

**Internal Steps**:
1. Check Redis cache for URL → if cached & fresh (< 24h), use cached content
2. Tier 1: HTTP fetch + Cheerio/BeautifulSoup → parse HTML
3. Tier 2 (fallback): Playwright headless browser → for JS-rendered pages
4. Tier 3 (fallback): Return error, ask user to paste JD manually
5. Extract keywords using **4-layer extraction pipeline** (see below)
6. Categorize keywords against **skill taxonomy database**
7. Show keywords to user for confirmation (Layer 4 — human checkpoint)
8. Write results to graph state

**Why no LLM here**: Keyword extraction is a solved statistical problem. Our 4-layer pipeline gives us ≥ 90% recall at zero token cost and < 300ms latency. An LLM would add 500+ tokens and 2-5 seconds of latency for marginal improvement.

#### Keyword Relevance: 4-Layer Extraction Pipeline

> **Core Principle**: We optimize for **high recall** (minimize false negatives), even at the cost of some false positives. Missing a keyword means we might miss relevant user data during matching — that's unacceptable. Extra keywords are harmless — the Data Matcher filters them out.

```mermaid
graph TD
    JD["📄 Job Description Text"] --> L1

    subgraph L1["Layer 1: YAKE + RAKE Ensemble — ~75-80% recall"]
        L1a["YAKE: statistical keyword extraction"]
        L1b["RAKE: multi-word phrase extraction"]
        L1c["Union of both sets"]
    end

    L1 --> L2

    subgraph L2["Layer 2: Skill Taxonomy Force-Match — +~15% recall"]
        L2a["Brute-force scan JD text against ~2000 taxonomy entries"]
        L2b["Alias expansion for each match"]
        L2c["Catches keywords YAKE/RAKE miss"]
    end

    L2 --> L3

    subgraph L3["Layer 3: Section-Aware Weighting — ranking, not extraction"]
        L3a["Parse JD sections: Required, Nice-to-Have, About"]
        L3b["Weight: Required ×2.0 | Responsibilities ×1.5"]
        L3c["Weight: Nice-to-Have ×1.0 | About ×0.3"]
    end

    L3 --> L4

    subgraph L4["Layer 4: User Confirmation Checkpoint — catches remaining"]
        L4a["Show extracted keywords to user"]
        L4b["User adds missed keywords / removes irrelevant ones"]
    end

    L4 --> OUT["✅ Final categorized keyword list"]

    style L1 fill:#5c1a5c,stroke:#8e44ad,color:#fff
    style L2 fill:#1a3a5c,stroke:#2980b9,color:#fff
    style L3 fill:#5c3a1a,stroke:#e67e22,color:#fff
    style L4 fill:#2d5016,stroke:#4a8c28,color:#fff
    style OUT fill:#1a5c5c,stroke:#1abc9c,color:#fff
```

**Layer 1: YAKE + RAKE Ensemble** (~75-80% recall)
- YAKE excels at single/multi-word keywords based on word position, frequency, and context
- RAKE excels at **phrases** (multi-word expressions like "distributed systems")
- Taking the **union** of both gives broader coverage than either alone

**Layer 2: Skill Taxonomy Force-Match** (~+15% recall — this is the critical layer)

After YAKE/RAKE, we run a brute-force scan of the entire JD text against our skill taxonomy database (~2000 curated entries). This catches keywords that YAKE/RAKE miss because they're statistically insignificant but **domain-critical**:

```python
def taxonomy_force_match(jd_text: str, taxonomy: dict) -> list[str]:
    """Catch keywords YAKE/RAKE miss by scanning against known skill taxonomy."""
    jd_normalized = jd_text.lower()
    matched = []
    
    for skill_name, skill_data in taxonomy.items():
        # Check primary name
        if skill_name in jd_normalized:
            matched.append(skill_name)
            continue
        # Check all aliases
        for alias in skill_data.get("aliases", []):
            if alias.lower() in jd_normalized:
                matched.append(skill_name)
                break
    
    return matched

# Example: JD says "...familiarity with Terraform is a plus..."
# YAKE might miss "Terraform" (appears once, low statistical weight)
# But taxonomy has: {"terraform": {"aliases": ["tf", "hashicorp terraform"]}}
# → Force-match catches it ✅
```

**Layer 3: Section-Aware Weighting** (ranking, not extraction)

JDs have structure. Keywords in "Requirements" matter more than those in "About Us":

```python
SECTION_WEIGHTS = {
    "required": 2.0, "must_have": 2.0, "qualifications": 2.0,
    "responsibilities": 1.5,
    "nice_to_have": 1.0, "preferred": 1.0,
    "about": 0.3, "benefits": 0.1,
}
# This layer doesn't catch MORE keywords — it ranks them correctly
# so even if we miss one, it's more likely to be low-priority
```

**Layer 4: User Confirmation Checkpoint** (catches remaining ~3-5%)

```
System: "I found these keywords from the JD:
  Hard Skills: Python, React, PostgreSQL, Docker
  Tools: AWS, Terraform, GitHub Actions  
  Soft Skills: leadership, cross-functional
  
  ❓ Did I miss anything? You can add or remove keywords."

User: "Add 'microservices' and 'REST APIs' — those are in the responsibilities section"
```

This is the **ultimate false-negative catcher** — the human fills in gaps the algorithm missed.

#### Keyword Relevance Validation Strategy

Before launch, we build a **test suite** to measure extraction quality:

| Metric | Target | Why |
|---|---|---|
| **Recall** (= 1 − false negative rate) | ≥ 90% | Missing a keyword = potentially missing relevant user data. Unacceptable. |
| **Precision** (= 1 − false positive rate) | ≥ 70% | Extra keywords are filtered by Data Matcher. Less critical. |

```python
# Validation approach:
# 1. Curate 50 real JDs from different domains (SWE, PM, DS, DevOps, etc.)
# 2. Manually label ground-truth keywords for each
# 3. Run our 4-layer pipeline (minus Layer 4 — user confirmation)
# 4. Measure recall and precision
# 5. Iterate on taxonomy entries until recall ≥ 90%
```

---

### Agent 2: Data Retriever Agent

| Property | Details |
|---|---|
| **Name** | `data_retriever_agent` |
| **Responsibility** | Match extracted keywords against user's stored profile data; find the most relevant projects, experiences, skills to include in the resume |
| **LLM Usage** | 🟡 **Light** — optional LLM call for semantic ranking of borderline items |
| **Inputs** | `keywords` (from Agent 1), `user_id` |
| **Outputs** | `matched_data` (ranked list), `suggestions` (add/replace recommendations) |

**This is the critical "intelligence" agent that answers**: *"Which of the user's projects, experiences, and skills are most relevant to THIS specific job?"*

**Internal Steps**:

```mermaid
graph TD
    START["Data Retriever Agent"] --> FC

    subgraph FC["1. FRESHNESS CHECK"]
        FC1["Check last GitHub sync timestamp"]
        FC2{"Stale > threshold?"}
        FC1 --> FC2
        FC2 -->|"Yes"| FC3["Trigger background re-sync"]
        FC2 -->|"No"| FC4["Use existing data"]
    end

    FC --> KM

    subgraph KM["2. KEYWORD MATCHING"]
        KM1["Exact match against skills"]
        KM2["Fuzzy match - trigram > 0.3"]
        KM3["Alias expansion from taxonomy"]
        KM4["Full-text search in projects, experiences, READMEs"]
        KM5["Score: exact x3.0 + fuzzy x1.5 + context x1.0"]
    end

    KM --> SS

    subgraph SS["3. SMART SUGGESTIONS"]
        SS1["Compare CURRENT resume items vs ALL available"]
        SS2{"alt_score > current x 1.5?"}
        SS1 --> SS2
        SS2 -->|"Yes"| SS3["SUGGEST REPLACE"]
        SS2 -->|"No"| SS4{"High relevance but not in resume?"}
        SS4 -->|"Yes"| SS5["SUGGEST ADD"]
    end

    SS --> OUT["4. OUTPUT\nranked_items + suggestions"]

    style START fill:#1a3a5c,stroke:#2980b9,color:#fff
    style OUT fill:#2d5016,stroke:#4a8c28,color:#fff
```

**Why this agent exists separately**: The previous design merged matching into a simple scoring function. But the *intelligence* of deciding what to ADD vs. REPLACE on the resume — that requires understanding the user's full profile holistically. This agent might use a lightweight LLM call (~500 tokens) for borderline decisions, but most of the work is algorithmic.

---

### Agent 3: Resume Refactorer Agent

| Property | Details |
|---|---|
| **Name** | `resume_refactorer_agent` |
| **Responsibility** | Rewrite the user's LaTeX resume to maximize ATS score while preserving template structure |
| **LLM Usage** | ✅ **Primary LLM consumer** |
| **Inputs** | `keywords`, `matched_data`, `suggestions`, `user_latex`, `feedback` (from Evaluator, if in correction loop) |
| **Outputs** | `refactored_latex`, `changelog` (list of changes with reasons) |

**Prompt Structure**:
```
SYSTEM: You are a professional resume writer...
        [rules about template preservation, no hallucination, etc.]

CONTEXT:
  - Target Job Keywords: {keywords}
  - User Data to Include: {matched_data}
  - Suggested Changes: {suggestions}  ← from Data Retriever
  - Current LaTeX: {user_latex}

CORRECTION CONTEXT (if self-correction loop):
  - Previous attempt errors: {evaluator_feedback}
  - Specific fixes required: {fix_instructions}

OUTPUT:
  - Refactored LaTeX code
  - JSON changelog: [{section, change_type, before, after, reason}]
```

**Key Design Decision**: The Refactorer does NOT decide what data to include — that decision was already made by the Data Retriever. The Refactorer's only job is to **write well** given the data it received. This separation prevents the LLM from making data selection decisions (which it's worse at than our algorithmic matcher).

---

### Agent 4: Evaluator Agent

| Property | Details |
|---|---|
| **Name** | `evaluator_agent` |
| **Responsibility** | Validate the refactored resume for factual consistency, grounding, LaTeX validity, and quality |
| **LLM Usage** | 🟡 **Hybrid** — deterministic checks + optional LLM for nuanced grounding |
| **Inputs** | `refactored_latex`, `changelog`, `user_profile_data`, `keywords` |
| **Outputs** | `evaluation_result` (pass/fail), `errors[]`, `warnings[]`, `feedback` (for self-correction) |

**Evaluation Pipeline (4 Stages)**:

```mermaid
graph TB
    subgraph S1["Stage 1: FACTUAL CONSISTENCY"]
        direction TB
        S1a["Extract named entities"]
        S1b["Cross-reference vs user data"]
        S1c["FAIL if company/role missing"]
        S1d["WARN if metric unverifiable"]
    end

    subgraph S2["Stage 2: GROUNDING CHECK"]
        direction TB
        S2a["Trace each bullet to source"]
        S2b["Keyword overlap ratio"]
        S2c["Optional LLM grounding check"]
    end

    subgraph S3["Stage 3: LATEX VALIDITY"]
        direction TB
        S3a["Syntax check: balanced braces"]
        S3b["Dry-run compilation"]
        S3c["Template structure preserved?"]
    end

    subgraph S4["Stage 4: QUALITY ASSESSMENT"]
        direction TB
        S4a["Keyword coverage %"]
        S4b["Action verbs in bullets?"]
        S4c["Within page limits?"]
    end

    S1 --> S2 --> S3 --> S4

    style S1 fill:#5c1a1a,stroke:#e74c3c,color:#fff
    style S2 fill:#5c3a1a,stroke:#e67e22,color:#fff
    style S3 fill:#1a3a5c,stroke:#2980b9,color:#fff
    style S4 fill:#2d5016,stroke:#4a8c28,color:#fff
```

**Routing Decision**:
```python
if len(factual_errors) > 0 or len(grounding_errors) > 0:
    if iteration_count < MAX_RETRIES (default: 3):
        → Route BACK to Refactorer with feedback
    else:
        → Route to Human Review with warnings
elif len(latex_errors) > 0:
    → Route BACK to Refactorer with LaTeX fix instructions
else:
    → Route to Human Review (PASS)
```

---

### Agent 5: Human Review Node (Iterative Conversational Loop)

| Property | Details |
|---|---|
| **Name** | `human_review_node` |
| **Responsibility** | Present the refactored resume to the user; enable an **iterative conversation** for refinements until the user is satisfied |
| **LLM Usage** | ❌ None |
| **Inputs** | `refactored_latex`, `changelog`, `evaluation_result`, `original_latex` |
| **Outputs** | `user_decision` (accept/request_changes), `user_change_request` (natural language instructions), `edited_latex` (if manually edited) |

> **Design Decision**: Instead of a binary Accept/Reject model, this node implements an **iterative refinement loop**. The user never "rejects" — they request specific changes in natural language, and the Refactorer applies only those changes while preserving everything the user was happy with. This is more token-efficient (partial rewrite vs. full regeneration) and gives the user fine-grained control.

**What the user sees**:
1. **Diff view**: Side-by-side original vs. refactored LaTeX (highlighted changes)
2. **Changelog**: Every change with the reason why it was made
3. **Evaluation report**: Factual consistency ✅, Grounding ✅, Quality score, Keyword coverage %
4. **Warnings** (if any): Items the Evaluator flagged but couldn't auto-fix
5. **Chat input**: Free-text field to describe what they want changed (e.g., *"Make the projects section more concise and move Kubernetes higher"*)
6. **Actions**: ✅ Accept → Compile PDF | 🔄 Request Changes → describe what to improve | ✏️ Edit LaTeX → manual edit + re-evaluate

**Iterative Flow Example**:
```
Iteration 1: System shows resume v1
  User: "Make the projects section more concise, and move Kubernetes higher"
  → Refactorer rewrites ONLY those parts → Evaluator re-checks → shows v2

Iteration 2: User sees v2
  User: "Good, but change the summary to focus more on backend engineering"
  → Refactorer rewrites summary only → Evaluator re-checks → shows v3

Iteration 3: User sees v3
  User: "Perfect" → Accept → Compile PDF
```

**How the Refactorer handles change requests** (iteration 2+):
```
SYSTEM: You are refining an already-approved resume. The user has requested
        specific changes. ONLY modify what the user asked for. Keep everything
        else EXACTLY as-is.

CURRENT RESUME: {current_refactored_latex}  ← NOT the original, the latest version
USER REQUEST: {user_change_request}

OUTPUT: Modified LaTeX + changelog of ONLY the changes made.
```

---

## 3. LangGraph State & Graph Design

### 3.1 Graph State Schema

Every node in the graph reads from and writes to a shared **state object**:

```python
from typing import TypedDict, Literal, Annotated
from langgraph.graph import add_messages

class ResumeForgeState(TypedDict):
    """Shared state across all agents in the LangGraph."""
    
    # ─── User Context ───
    user_id: str
    user_latex: str                    # User's current LaTeX template
    user_profile: dict                 # Full profile data (experiences, skills, etc.)
    
    # ─── Job Context ───
    job_url: str
    job_text: str                      # Scraped job description text
    job_metadata: dict                 # {title, company, location, platform}
    
    # ─── Keyword Extraction ───
    keywords: dict                     # {hard_skills: [], soft_skills: [], tools: [], ...}
    keywords_confirmed: bool           # User confirmed/modified keywords
    
    # ─── Data Retrieval ───
    matched_data: list[dict]           # Ranked relevant items from user profile
    suggestions: list[dict]            # Add/replace recommendations
    github_sync_status: str            # "fresh" | "stale" | "syncing" | "error"
    github_last_synced: str            # ISO timestamp
    
    # ─── Refactoring ───
    refactored_latex: str              # LLM-generated LaTeX
    changelog: list[dict]              # [{section, change_type, before, after, reason}]
    
    # ─── Evaluation ───
    evaluation_result: dict            # {passed: bool, factual_errors, grounding_errors, ...}
    evaluator_feedback: str            # Feedback for self-correction
    
    # ─── Control Flow ───
    iteration_count: int               # Self-correction loop counter
    max_iterations: int                # Default: 3
    current_step: str                  # For UI progress display
    error: str | None                  # Error message if any step fails
    
    # ─── Human-in-the-Loop (Iterative) ───
    user_decision: str                 # "accept" | "request_changes" | "edit"
    user_change_request: str           # Natural language change instructions (iterative)
    edited_latex: str                  # User's manual edits (if direct edit)
    human_review_iteration: int        # Tracks how many change-request rounds
    
    # ─── Output ───
    final_latex: str                   # Accepted LaTeX
    pdf_url: str                       # Compiled PDF download URL
    
    # ─── Chat Messages ───
    messages: Annotated[list, add_messages]  # Chat history for UI display
```

### 3.2 Graph Definition

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver

# Define the graph
builder = StateGraph(ResumeForgeState)

# ─── Add Nodes ───
builder.add_node("scraper_keyword",    scraper_keyword_agent)
builder.add_node("data_retriever",     data_retriever_agent)
builder.add_node("resume_refactorer",  resume_refactorer_agent)
builder.add_node("evaluator",          evaluator_agent)
builder.add_node("human_review",       human_review_node)
builder.add_node("compile_pdf",        compile_pdf_node)

# ─── Add Edges ───
builder.add_edge(START,                "scraper_keyword")
builder.add_edge("scraper_keyword",    "data_retriever")
builder.add_edge("data_retriever",     "resume_refactorer")
builder.add_edge("resume_refactorer",  "evaluator")

# ─── Conditional Edges ───
builder.add_conditional_edges(
    "evaluator",
    route_after_evaluation,     # Function that decides next node
    {
        "refactor_again": "resume_refactorer",   # Self-correction loop
        "human_review":   "human_review",         # Passed evaluation
    }
)

builder.add_conditional_edges(
    "human_review",
    route_after_human_review,
    {
        "accept":           "compile_pdf",          # User satisfied → compile
        "request_changes":  "resume_refactorer",    # Iterative: user describes changes
        "edit":             "evaluator",            # Manual LaTeX edit → re-evaluate
        "modify_keywords":  "scraper_keyword",      # User wants different keywords
    }
)

builder.add_edge("compile_pdf", END)

# ─── Build & Compile ───
checkpointer = PostgresSaver.from_conn_string(DATABASE_URL)
graph = builder.compile(
    checkpointer=checkpointer,
    interrupt_before=["human_review"],  # Pause for human input
)
```

### 3.3 Visual Graph (Mermaid)

```mermaid
graph TD
    START((Start)) --> SK["🔍 Scraper & Keyword Agent<br/><i>Fetch URL → Extract Keywords</i><br/><b>NO LLM</b>"]
    
    SK --> DR["📊 Data Retriever Agent<br/><i>Match user data → Rank relevance</i><br/><i>Suggest add/replace</i><br/><b>LLM-light</b>"]
    
    DR --> RR["✍️ Resume Refactorer Agent<br/><i>Rewrite LaTeX resume</i><br/><b>PRIMARY LLM</b>"]
    
    RR --> EV{"🔎 Evaluator Agent<br/><i>Factual consistency</i><br/><i>Grounding check</i><br/><i>LaTeX validation</i>"}
    
    EV -->|"❌ Errors found<br/>(iteration < max)"| RR
    EV -->|"✅ Passed OR<br/>max iterations hit"| HR["👤 Human Review<br/><i>Iterative Conversation</i><br/><b>INTERRUPT</b>"]
    
    HR -->|"✅ Accept"| PDF["📄 Compile PDF<br/><i>LaTeX → PDF</i>"]
    HR -->|"✏️ Edit LaTeX"| EV
    HR -->|"🔄 Request Changes<br/>(iterative refinement)"| RR
    HR -->|"🔑 Modify Keywords"| SK
    
    PDF --> DONE((End))
    
    style SK fill:#2d5016,stroke:#4a8c28,color:#fff
    style DR fill:#1a3a5c,stroke:#2980b9,color:#fff
    style RR fill:#5c1a5c,stroke:#8e44ad,color:#fff
    style EV fill:#5c3a1a,stroke:#e67e22,color:#fff
    style HR fill:#1a5c5c,stroke:#1abc9c,color:#fff
    style PDF fill:#3a1a1a,stroke:#e74c3c,color:#fff
```

---

## 4. The Self-Correction Loop

This is the core architectural innovation over the linear pipeline. The **Evaluator → Refactorer** cycle ensures the output is correct before the user ever sees it.

### How It Works

```mermaid
sequenceDiagram
    participant RR as Refactorer
    participant EV as Evaluator

    Note over RR,EV: Iteration 1
    RR->>EV: Resume v1 generated
    EV->>EV: Finds 2 factual errors
    Note right of EV: 1. Added 'Kubernetes' - user has none
    Note right of EV: 2. Changed tenure '6mo' to '1yr'
    EV->>RR: ERRORS - PLEASE FIX (with specific feedback)

    Note over RR,EV: Iteration 2
    RR->>RR: Reads feedback + previous attempt
    RR->>EV: Resume v2 generated
    EV->>EV: All checks pass ✅
    EV->>EV: Route to Human Review
```

### Self-Correction Routing Logic

```python
def route_after_evaluation(state: ResumeForgeState) -> str:
    """Decide whether to loop back for correction or proceed to human review."""
    
    evaluation = state["evaluation_result"]
    iteration = state["iteration_count"]
    max_iter = state.get("max_iterations", 3)
    
    has_factual_errors = len(evaluation.get("factual_errors", [])) > 0
    has_grounding_errors = len(evaluation.get("grounding_errors", [])) > 0
    has_latex_errors = len(evaluation.get("latex_errors", [])) > 0
    
    has_critical_errors = has_factual_errors or has_grounding_errors or has_latex_errors
    
    if has_critical_errors and iteration < max_iter:
        return "refactor_again"  # Loop back with feedback
    else:
        return "human_review"    # Pass to user (with warnings if max iterations hit)
```

### Why Max 3 Iterations?

| Iteration | Purpose |
|---|---|
| 1 | Initial generation — often 80% correct |
| 2 | Fix specific errors identified by Evaluator — catches 95% of issues |
| 3 | Final attempt for edge cases — after this, human review takes over |

> **Rationale**: After 3 iterations, if the LLM still can't fix the issue, it's likely a systemic prompt problem, not a fixable generation error. Sending it to the user with warnings is better than burning tokens in an infinite loop.

---

## 5. GitHub Data Freshness Strategy

### The Problem

GitHub data is a **live data source**. The user might:
- Push a new project 5 minutes ago that's highly relevant to the job they're applying for
- Update a README with new features/metrics
- Archive an old repo that shouldn't be on their resume

If our agents are working with stale data, the resume won't reflect the user's current capabilities.

### Solution: Multi-Layer Freshness Strategy

```mermaid
graph TD
    subgraph L1["Layer 1: PASSIVE FRESHNESS - Background"]
        L1a["On login: check last_synced timestamp"]
        L1b["If stale > 24h: async background sync"]
        L1c["User NOT blocked - works with existing data"]
        L1d["Notify user when sync completes"]
    end

    subgraph L2["Layer 2: ACTIVE FRESHNESS - Pre-Pipeline"]
        L2a["Before Data Retriever: check freshness"]
        L2b["If last_sync > 1h: quick incremental sync"]
        L2c["GET /user/repos?since=last_sync"]
        L2d["Adds ~1-2s but ensures current data"]
    end

    subgraph L3["Layer 3: USER-TRIGGERED SYNC"]
        L3a["Sync GitHub button in profile"]
        L3b["Full re-sync of all repos"]
        L3c["Shows progress indicator"]
    end

    subgraph L4["Layer 4: WEBHOOK-BASED - v2 Future"]
        L4a["Register GitHub App webhook"]
        L4b["Receive push events real-time"]
        L4c["Auto-update DB on every push"]
    end

    L1 --> L2 --> L3 --> L4

    style L1 fill:#2d5016,stroke:#4a8c28,color:#fff
    style L2 fill:#1a3a5c,stroke:#2980b9,color:#fff
    style L3 fill:#5c3a1a,stroke:#e67e22,color:#fff
    style L4 fill:#5c1a5c,stroke:#8e44ad,color:#fff
```

### Implementation in the Data Retriever Agent

```python
async def data_retriever_agent(state: ResumeForgeState) -> dict:
    user_id = state["user_id"]
    
    # ─── FRESHNESS CHECK ───
    github_profile = await get_github_profile(user_id)
    
    if github_profile and github_profile.connected:
        last_synced = github_profile.last_synced
        staleness = datetime.now() - last_synced
        
        if staleness > timedelta(hours=1):
            # Incremental sync — only fetch changed repos
            state["github_sync_status"] = "syncing"
            new_repos = await incremental_github_sync(
                user_id, 
                since=last_synced
            )
            state["github_sync_status"] = "fresh"
            state["github_last_synced"] = datetime.now().isoformat()
            
            if new_repos:
                # Notify user in chat: "Found 3 new/updated repos since last sync"
                state["messages"].append({
                    "role": "assistant",
                    "content": f"📡 Synced your GitHub — found {len(new_repos)} updated repos: "
                              f"{', '.join(r.name for r in new_repos)}"
                })
        else:
            state["github_sync_status"] = "fresh"
    
    # ─── PROCEED WITH MATCHING ───
    # Now we're guaranteed to have reasonably fresh data
    matched_data = await match_user_data(state["keywords"], user_id)
    suggestions = await generate_suggestions(matched_data, state["user_latex"])
    
    return {
        "matched_data": matched_data,
        "suggestions": suggestions,
        "github_sync_status": state["github_sync_status"],
    }
```

### Freshness Tracking in State

The graph state includes `github_sync_status` and `github_last_synced` so that:
1. The **UI can show** the sync status in real-time ("Data synced 2 min ago ✅")
2. The **Evaluator** knows whether the data was fresh when the resume was generated
3. If the user manually syncs mid-flow, they can re-run from the Data Retriever node

---

## 6. Guardrails Framework

In addition to the Evaluator Agent's self-correction loop, we implement **guardrails** at multiple levels:

### 6.1 Input Guardrails (Before Pipeline)

```python
# Applied before the graph even starts

class InputGuardrails:
    def validate_url(self, url: str) -> bool:
        """Check URL is a valid job posting URL, not a malicious link."""
        # Allowlist of known job platforms + generic URL validation
        # Block: file://, javascript:, data: schemes
        
    def validate_latex(self, latex: str) -> bool:
        """Check LaTeX code for dangerous commands."""
        # Block: \write18, \input{/etc/...}, \openout, etc.
        # Allow: standard resume LaTeX commands
        
    def validate_user_data(self, data: dict) -> bool:
        """Check user data for injection attempts."""
        # Sanitize all text fields
        # Max length enforcement
```

### 6.2 LLM Output Guardrails (Refactorer)

```python
class OutputGuardrails:
    def check_latex_only(self, output: str) -> bool:
        """Ensure LLM output is valid LaTeX, not prose/markdown."""
        # Must start with \documentclass or known LaTeX preamble
        # Must not contain markdown headers, code fences, etc.
        
    def check_length(self, output: str, max_pages: int = 2) -> bool:
        """Ensure resume doesn't exceed page limits."""
        
    def check_no_system_prompt_leak(self, output: str) -> bool:
        """Ensure LLM didn't leak system prompt contents."""
        
    def check_language(self, output: str) -> bool:
        """Ensure output is in English (v1)."""
```

### 6.3 Structural Guardrails (Template Preservation)

```python
class TemplateGuardrails:
    def compare_structure(self, original: str, refactored: str) -> dict:
        """Compare LaTeX structure between original and refactored."""
        original_sections = parse_latex_sections(original)
        refactored_sections = parse_latex_sections(refactored)
        
        # Check: same sections in same order
        # Check: same preamble (fonts, packages)
        # Check: no new \section or \subsection commands added
        # Returns: {preserved: bool, changes: [...]}
```

### 6.4 Guardrails in the Graph

Guardrails are applied at graph edges, not inside nodes:

```python
# Pre-node guardrails (edge functions)
def pre_refactorer_guardrail(state: ResumeForgeState) -> ResumeForgeState:
    """Applied before Refactorer runs. Validates inputs."""
    validate_latex(state["user_latex"])
    validate_matched_data(state["matched_data"])
    return state

# Post-node guardrails (built into Evaluator)
# The Evaluator IS the primary guardrail for LLM output
```

---

## 7. Token Budget Across Agents

### Per-Agent Token Usage

| Agent | LLM Calls | Input Tokens | Output Tokens | Notes |
|---|---|---|---|---|
| Scraper & Keyword | 0 | 0 | 0 | Purely algorithmic |
| Data Retriever | 0-1 | 0-500 | 0-200 | Optional LLM for borderline ranking |
| Resume Refactorer | 1 | ~3,500 | ~2,000 | Primary LLM consumer |
| Evaluator (grounding) | 0-1 | 0-800 | 0-200 | Optional LLM for nuanced checks |
| **Total per generation** | **1-3** | **~3,500-4,800** | **~2,000-2,400** | |
### Self-Correction Token Cost

| Scenario | Total LLM Calls | Total Input Tokens | Total Output Tokens |
|---|---|---|---|
| Pass on 1st try (ideal) | 1-3 | ~4,000 | ~2,200 |
| Pass on 2nd try (common) | 3-5 | ~8,500 | ~4,400 |
| Pass on 3rd try (rare) | 5-7 | ~13,000 | ~6,600 |
| Hit max iterations (very rare) | 7-9 | ~17,500 | ~8,800 |

> **Optimization**: The self-correction prompt includes ONLY the errors and fix instructions, not the entire context again. The Refactorer's 2nd+ attempts re-use the original context plus a compact error summary (~300 tokens).

## 8. Architecture: 3-Service Polyrepo

### Architecture Decision

> **v0.2.0 Decision**: We're splitting the system into **3 independent microservices**, each in its own Git repository. This follows industry microservices patterns and maximizes learning across the full stack.

**Why 3 services instead of a monorepo?**

| Factor | Monorepo | Polyrepo (our choice) ✅ |
|---|---|---|
| **Learning value** | Teaches workspace management | Teaches inter-service contracts, API versioning, independent CI/CD |
| **Deployment** | Deploy everything together | Deploy independently — update AI service without touching frontend |
| **Team scalability** | Everyone works in one repo | Services can be owned by different people |
| **Technology isolation** | Mixed Node.js + Python tooling | Clean: pure TS repo + pure Python repo |
| **Industry relevance** | Startups, small teams | Enterprise, microservices-heavy companies |

### Service Overview

```mermaid
graph TD
    subgraph REPO1["📦 Repo: resumeforge-web"]
        FE["Next.js Frontend\n(TypeScript)\n\nUI, Chat Interface\nResume Preview\nAuth UI"]
    end

    subgraph REPO2["📦 Repo: resumeforge-api"]
        BE["Node.js + Express Backend\n(TypeScript)\n\nAuth, Profile CRUD\nSession Management\nGitHub OAuth\nWebSocket Gateway"]
    end

    subgraph REPO3["📦 Repo: resumeforge-ai"]
        AI["Python + FastAPI + LangGraph\n\nAgent Pipeline\nKeyword Extraction\nResume Refactoring\nEvaluation"]
    end

    FE -->|"REST + WebSocket"| BE
    BE -->|"REST + SSE\n(internal API)"| AI

    subgraph DATA["Shared Infrastructure"]
        PG["PostgreSQL"]
        RD["Redis"]
        S3["File Storage"]
    end

    BE --> PG
    BE --> RD
    AI --> PG
    AI --> RD

    style REPO1 fill:#1a3a5c,stroke:#2980b9,color:#fff
    style REPO2 fill:#2d5016,stroke:#4a8c28,color:#fff
    style REPO3 fill:#5c1a5c,stroke:#8e44ad,color:#fff
    style DATA fill:#5c3a1a,stroke:#e67e22,color:#fff
```

### Service Responsibilities

| | **resumeforge-web** (Frontend) | **resumeforge-api** (Backend API) | **resumeforge-ai** (AI Service) |
|---|---|---|---|
| **Tech** | Next.js 14+, TypeScript | Node.js + Express, TypeScript | Python, FastAPI, LangGraph |
| **Owns** | UI rendering, client-side state, auth UI | User data, auth, sessions, GitHub OAuth, profile CRUD | Agent pipeline, keyword extraction, resume refactoring, evaluation |
| **DB Access** | ❌ None (goes through API) | ✅ Full CRUD (Prisma ORM) | ✅ Read user data + Write graph state (SQLAlchemy) |
| **LLM Calls** | ❌ None | ❌ None | ✅ All LLM interactions |
| **Communicates with** | Backend API only | Frontend + AI Service | Backend API only |
| **Deployed as** | Static site / Vercel / container | Long-running server (Railway / Render / container) | Long-running server (Railway / Render / container) |

### Communication Between Services

```mermaid
sequenceDiagram
    participant FE as Next.js Frontend
    participant BE as Express Backend
    participant AI as FastAPI AI Service

    Note over FE,AI: User submits a job URL

    FE->>BE: POST /api/chat/sessions/{id}/message
    Note right of BE: Validates auth, stores message

    BE->>AI: POST /internal/pipeline/run
    Note right of AI: Starts LangGraph execution

    AI-->>BE: SSE stream (progress, keywords, matches)
    BE-->>FE: WebSocket relay (real-time UI updates)

    Note over FE,AI: Pipeline reaches human review

    AI-->>BE: SSE: "awaiting_human_input"
    BE-->>FE: WebSocket: show preview + diff

    FE->>BE: POST /api/chat/sessions/{id}/review
    Note right of BE: User decides: accept / request changes

    BE->>AI: POST /internal/pipeline/resume/{id}
    Note right of AI: Resume graph with user input

    AI-->>BE: SSE: "pdf_ready"
    BE-->>FE: WebSocket: download link

    FE->>BE: GET /api/download/{id}
    BE->>AI: GET /internal/download/{id}
    AI-->>BE: PDF binary
    BE-->>FE: PDF binary
```

### Why Backend Acts as Gateway

The Express backend sits between Frontend and AI Service. The frontend **never** talks to the AI service directly:

| Reason | Details |
|---|---|
| **Auth centralization** | Only the Express backend validates JWTs and session tokens. The AI service trusts requests from the backend (internal API key). |
| **Protocol translation** | Frontend uses WebSocket (bidirectional, real-time). AI service uses SSE (unidirectional, simpler). Backend bridges both. |
| **Data enrichment** | Before forwarding to AI, the backend attaches user profile data, LaTeX template, and preferences from the DB. |
| **Rate limiting & abuse** | Backend can throttle per-user requests before they reach the AI service (v2 concern, but architecturally correct now). |
| **Security** | AI service is **not exposed to the internet** — it runs on an internal network. Only the backend can reach it. |

### Technology Stack (Per Service)

#### resumeforge-web (Frontend)

| Layer | Technology |
|---|---|
| Framework | Next.js 14+ (App Router) |
| Language | TypeScript |
| Styling | Tailwind CSS + shadcn/ui |
| State | Zustand |
| Communication | REST + WebSocket (to Backend API) |
| Deployment | Vercel / Docker container |

#### resumeforge-api (Backend API)

| Layer | Technology |
|---|---|
| Framework | Express.js |
| Language | TypeScript |
| ORM | Prisma |
| Auth | Passport.js + JWT + Google OAuth 2.0 |
| Real-time | WebSocket (socket.io or ws) |
| Caching | Redis (ioredis) |
| Deployment | Railway / Render / Docker container |

#### resumeforge-ai (AI Service)

| Layer | Technology | Rationale |
|---|---|---|
| API Framework | **FastAPI** (Python) | Async-first, great for streaming, LangGraph is Python-native |
| Agent Framework | **LangGraph** | Multi-agent orchestration, cycles, human-in-the-loop, checkpointing |
| LLM Client | **LangChain** (via LangGraph) | Provider-agnostic LLM calls (OpenAI, Anthropic, Google) |
| Keyword Extraction | **yake** + **multi_rake** | Native Python, zero token cost |
| DB Access | **SQLAlchemy** (async) | Read user data, write graph checkpoints |
| Task Queue | **asyncio Queue** | MVP-scoped, no Celery overhead |
| Deployment | Railway / Render / Docker container |

### Shared Infrastructure

| Component | Purpose | Access |
|---|---|---|
| **PostgreSQL** | Users, profiles, chat sessions, graph checkpoints | Backend (Prisma) + AI Service (SQLAlchemy) |
| **Redis** | Session cache, URL cache, pub/sub for real-time | Backend (ioredis) + AI Service (aioredis) |
| **File Storage** | PDFs, LaTeX files | Backend (upload) + AI Service (generate) |

> **Shared DB, Separate ORMs**: Both services access the same PostgreSQL instance. The Backend owns the schema (via Prisma migrations). The AI Service reads user data and writes to its own tables (graph checkpoints, evaluation results). Each service has its own ORM — no shared code between repos.

---

## 9. Repository Structures

### Repo 1: `resumeforge-web` (Frontend)

```
resumeforge-web/
├── src/
│   ├── app/                            # Next.js App Router
│   │   ├── layout.tsx
│   │   ├── page.tsx                    # Landing page
│   │   ├── (auth)/
│   │   │   ├── login/page.tsx
│   │   │   └── callback/page.tsx
│   │   └── (dashboard)/
│   │       ├── layout.tsx
│   │       ├── chat/
│   │       │   ├── page.tsx
│   │       │   └── [sessionId]/page.tsx
│   │       ├── profile/
│   │       │   ├── page.tsx
│   │       │   ├── latex/page.tsx
│   │       │   ├── github/page.tsx
│   │       │   └── experience/page.tsx
│   │       └── history/page.tsx
│   ├── components/
│   │   ├── ui/                         # shadcn/ui
│   │   ├── chat/
│   │   │   ├── ChatInterface.tsx
│   │   │   ├── MessageBubble.tsx
│   │   │   ├── ChatInput.tsx
│   │   │   ├── ChatSidebar.tsx
│   │   │   ├── KeywordReview.tsx       # Interactive keyword editor
│   │   │   ├── DataMatchReview.tsx     # Show matched data + suggestions
│   │   │   ├── EvaluationReport.tsx    # Show evaluation results
│   │   │   └── PipelineProgress.tsx    # Step-by-step progress indicator
│   │   ├── editor/
│   │   │   ├── LaTeXEditor.tsx         # Monaco editor for LaTeX
│   │   │   └── DiffViewer.tsx          # Side-by-side diff
│   │   └── preview/
│   │       ├── ResumePreview.tsx       # Modal with preview + diff + changelog
│   │       └── PDFViewer.tsx
│   ├── lib/
│   │   ├── api.ts                      # REST client for Backend API
│   │   ├── ws.ts                       # WebSocket client for real-time updates
│   │   └── auth.ts                     # Auth helpers
│   ├── stores/
│   │   ├── chatStore.ts
│   │   └── profileStore.ts
│   └── types/
│       └── index.ts                    # Shared TypeScript interfaces
├── package.json
├── next.config.js
├── tailwind.config.ts
├── tsconfig.json
├── Dockerfile
├── .env.example
└── README.md
```

### Repo 2: `resumeforge-api` (Backend API)

```
resumeforge-api/
├── src/
│   ├── index.ts                        # Express app entry point
│   ├── config/
│   │   ├── database.ts                 # Prisma client setup
│   │   ├── redis.ts                    # Redis client setup
│   │   └── env.ts                      # Environment variable validation
│   ├── routes/
│   │   ├── auth.routes.ts              # Google OAuth, JWT, sessions
│   │   ├── profile.routes.ts           # User profile CRUD
│   │   ├── github.routes.ts            # GitHub OAuth + sync trigger
│   │   ├── chat.routes.ts              # Chat session management
│   │   ├── review.routes.ts            # Human review actions
│   │   └── download.routes.ts          # PDF download proxy
│   ├── middleware/
│   │   ├── auth.middleware.ts          # JWT validation
│   │   ├── validation.middleware.ts    # Request body validation (Zod)
│   │   └── error.middleware.ts         # Global error handler
│   ├── services/
│   │   ├── auth.service.ts             # Auth business logic
│   │   ├── profile.service.ts          # Profile operations
│   │   ├── github.service.ts           # GitHub API interactions
│   │   ├── aiService.client.ts         # ★ HTTP client for AI Service (internal API)
│   │   └── websocket.service.ts        # WebSocket manager + SSE relay
│   ├── types/
│   │   └── index.ts                    # Backend-specific types
│   └── utils/
│       ├── logger.ts
│       └── helpers.ts
├── prisma/
│   ├── schema.prisma                   # DB schema (single source of truth)
│   └── migrations/                     # Prisma migrations
├── tests/
│   ├── auth.test.ts
│   ├── profile.test.ts
│   └── integration/
│       └── ai-service.test.ts          # Contract tests against AI Service
├── package.json
├── tsconfig.json
├── Dockerfile
├── .env.example
└── README.md
```

### Repo 3: `resumeforge-ai` (AI Service)

```
resumeforge-ai/
├── app/
│   ├── main.py                         # FastAPI app entry point
│   ├── config.py                       # Environment config
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes/
│   │   │   ├── pipeline.py             # POST /internal/pipeline/run
│   │   │   ├── resume.py               # POST /internal/pipeline/resume/{id}
│   │   │   ├── stream.py               # SSE streaming endpoint
│   │   │   └── download.py             # GET /internal/download/{id}
│   │   └── middleware/
│   │       └── internal_auth.py        # Validate internal API key (from Backend)
│   │
│   ├── graph/                          # ★ LangGraph Multi-Agent Pipeline
│   │   ├── __init__.py
│   │   ├── state.py                    # ResumeForgeState definition
│   │   ├── builder.py                  # Graph construction & compilation
│   │   ├── nodes/                      # Agent implementations
│   │   │   ├── __init__.py
│   │   │   ├── scraper_keyword.py      # Agent 1: Scrape URL + 4-Layer Keyword Extraction
│   │   │   ├── data_retriever.py       # Agent 2: Match & rank user data
│   │   │   ├── resume_refactorer.py    # Agent 3: LLM resume rewriting
│   │   │   ├── evaluator.py            # Agent 4: Validation + self-correction
│   │   │   └── human_review.py         # Agent 5: Human-in-the-loop (iterative)
│   │   ├── edges/                      # Conditional routing logic
│   │   │   ├── __init__.py
│   │   │   ├── evaluation_router.py    # Route after evaluation (loop or pass)
│   │   │   └── human_router.py         # Route after human decision
│   │   └── guardrails/                 # Input/output guardrails
│   │       ├── __init__.py
│   │       ├── input_guards.py
│   │       ├── output_guards.py
│   │       └── template_guards.py
│   │
│   ├── services/                       # Shared services used by agents
│   │   ├── __init__.py
│   │   ├── scraper/
│   │   │   ├── __init__.py
│   │   │   ├── http_scraper.py         # Tier 1: httpx + BeautifulSoup
│   │   │   ├── browser_scraper.py      # Tier 2: Playwright
│   │   │   └── parsers/                # Platform-specific parsers
│   │   │       ├── linkedin.py
│   │   │       ├── greenhouse.py
│   │   │       ├── lever.py
│   │   │       └── generic.py
│   │   ├── keywords/
│   │   │   ├── __init__.py
│   │   │   ├── yake_extractor.py
│   │   │   ├── rake_extractor.py
│   │   │   ├── ensemble.py             # Layer 1: Merge + deduplicate
│   │   │   ├── taxonomy_matcher.py     # Layer 2: Force-match against taxonomy
│   │   │   ├── section_parser.py       # Layer 3: Section-aware weighting
│   │   │   └── taxonomy.py             # Skill categorization
│   │   ├── matcher/
│   │   │   ├── __init__.py
│   │   │   ├── fuzzy_match.py
│   │   │   ├── scorer.py
│   │   │   └── suggestion_engine.py    # Add/replace logic
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── provider.py             # LLM provider abstraction
│   │   │   ├── prompts.py              # Prompt templates
│   │   │   └── token_counter.py        # Token budget management
│   │   ├── compiler/
│   │   │   ├── __init__.py
│   │   │   ├── latex_compiler.py       # Docker-sandboxed compilation
│   │   │   └── sanitizer.py            # LaTeX security checks
│   │   └── github/
│   │       ├── __init__.py
│   │       ├── sync.py                 # Full + incremental sync
│   │       └── repo_parser.py          # Extract useful info from repos
│   │
│   ├── models/                         # SQLAlchemy models (read from Prisma's DB)
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── chat_session.py
│   │   ├── github_repo.py
│   │   └── experience.py
│   │
│   └── data/
│       └── skill_taxonomy.json         # ~2000+ skills database
│
├── tests/
│   ├── test_scraper.py
│   ├── test_keywords.py
│   ├── test_matcher.py
│   ├── test_evaluator.py
│   └── test_graph_integration.py       # End-to-end graph tests
│
├── requirements.txt
├── pyproject.toml
├── Dockerfile
├── .env.example
└── README.md
```

### Local Development: `docker-compose.yml`

A shared `docker-compose.yml` lives in a separate infra repo or in any of the repos. For local dev, it spins up all 3 services + shared infra:

```yaml
# docker-compose.yml (for local development)
services:
  web:          # resumeforge-web
    build: ../resumeforge-web
    ports: ["3000:3000"]
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:4000

  api:          # resumeforge-api
    build: ../resumeforge-api
    ports: ["4000:4000"]
    environment:
      AI_SERVICE_URL: http://ai:8000
      DATABASE_URL: postgresql://user:pass@postgres:5432/resumeforge
      REDIS_URL: redis://redis:6379

  ai:           # resumeforge-ai
    build: ../resumeforge-ai
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: postgresql://user:pass@postgres:5432/resumeforge
      REDIS_URL: redis://redis:6379
      INTERNAL_API_KEY: ${INTERNAL_API_KEY}

  postgres:
    image: postgres:16
    ports: ["5432:5432"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
```

### Inter-Service API Contract

The Backend API and AI Service communicate via an **internal REST API**. This contract must be versioned and tested:

| Endpoint | Method | Who Calls | Purpose |
|---|---|---|---|
| `/internal/pipeline/run` | POST | Backend → AI | Start a new pipeline execution |
| `/internal/pipeline/resume/{id}` | POST | Backend → AI | Resume graph with human input |
| `/internal/stream/{id}` | GET (SSE) | Backend → AI | Subscribe to real-time progress |
| `/internal/download/{id}` | GET | Backend → AI | Fetch compiled PDF |
| `/internal/health` | GET | Backend → AI | Health check |

> **Contract Testing**: Both repos maintain contract tests. The Backend repo tests against the AI Service's API spec (OpenAPI). The AI Service repo tests that its responses match the spec. This prevents API drift between independently deployed services.

---

## 10. Challenges & Solutions (New/Updated)

### Challenge 9: GitHub Data Freshness in Multi-Agent Pipeline (NEW)

**Problem**: GitHub is a live data source. The Data Retriever agent might be working with stale data — the user pushed a highly relevant project 5 minutes ago but our DB hasn't synced yet. This directly impacts resume quality.

**Solution**:

| Strategy | Details |
|---|---|
| **Multi-layer freshness** | 4 layers: passive (on login), active (pre-pipeline), user-triggered, webhook (v2). See Section 5 for full details. |
| **Incremental sync** | Only fetch repos modified since last sync using GitHub API's `since` parameter. Reduces API calls from O(N) to O(changed). |
| **Non-blocking** | Don't block the pipeline on sync. Use best available data, notify user if new repos found mid-flow. |
| **Freshness in state** | Track `github_sync_status` and `github_last_synced` in graph state so Evaluator can flag "warning: GitHub data is 3 days old". |
| **User notification** | Chat message: "📡 Found 3 new repos since last sync: repo-a, repo-b, repo-c. These will be considered for matching." |

**Decision Log**: We considered always doing a full sync before every pipeline run, but this adds 5-15 seconds (for users with 50+ repos) and wastes GitHub API quota. Incremental sync is 90% faster and catches 99% of changes.

---

### Challenge 10: Multi-Agent Coordination & State Management (NEW)

**Problem**: With 5 agents sharing state, we need to prevent race conditions, ensure consistent state, handle partial failures, and manage the complexity of conditional routing.

**Solution**:

| Strategy | Details |
|---|---|
| **LangGraph's built-in state** | Shared `TypedDict` state ensures type safety. LangGraph handles serialization and concurrent access. |
| **Sequential execution** | Agents run sequentially (not in parallel), so no race conditions. Parallelism is only within a single agent (e.g., fetching multiple repos). |
| **Checkpointing** | Every state transition is checkpointed to PostgreSQL. If the server crashes, the graph resumes from the last checkpoint. |
| **Error boundaries** | Each node is wrapped in try/except. On failure: set `state["error"]`, route to error-handling node that notifies user. |
| **Idempotent nodes** | Nodes are designed to be re-runnable with the same input. If the graph resumes from a checkpoint, re-running a node produces the same result. |

**Decision Log**: We considered running the Scraper and Data Retriever in parallel (they're somewhat independent), but the Data Retriever needs keywords from the Scraper as input. True parallelism would only save ~1 second and adds complexity. Sequential is simpler and sufficient for v1.

---

### Challenge 11: Self-Correction Loop Convergence (NEW)

**Problem**: The Evaluator → Refactorer loop could theoretically loop forever if the LLM keeps making the same mistakes. We need to guarantee termination and manage the token cost of multiple iterations.

**Solution**:

| Strategy | Details |
|---|---|
| **Hard iteration cap** | Maximum 3 iterations (configurable). After that, send to Human Review with warnings regardless. |
| **Specific feedback** | Evaluator sends specific, actionable fix instructions — not vague "try again". This dramatically increases fix rate on retry. |
| **Error categorization** | Differentiate between fixable errors (hallucinated skill → "remove X") and systemic errors (LLM can't follow template structure → give up, escalate to human). |
| **Incremental context** | 2nd+ attempts include only the error feedback, not full re-prompt. Saves ~2,000 tokens per retry. |
| **Convergence tracking** | Track whether errors are *reducing* across iterations. If error count increases, abort early. |

**Decision Log**: We considered a "judge LLM" approach (separate LLM evaluates the first LLM's output) but our hybrid approach (deterministic checks + optional lightweight LLM) is 80% cheaper and catches 95% of issues. The deterministic checks are actually MORE reliable than an LLM judge for factual consistency.

---

### Challenge 12: Data Retriever "Add vs Replace" Intelligence (NEW)

**Problem**: The Data Retriever needs to make smart decisions about whether to recommend ADDING a project/experience to the resume or REPLACING an existing one. This requires understanding both the job requirements AND the resume's current content.

**Solution**:

| Strategy | Details |
|---|---|
| **Dual scoring** | Score both the resume's CURRENT items and ALL available items against the job keywords. Compare scores. |
| **Replace threshold** | Only suggest replacing when `alt_score > current_score × 1.5` (alternative is 50%+ more relevant). Prevents unnecessary churn. |
| **Section awareness** | Know which resume section each item belongs to. Don't suggest adding 5 projects if the resume only has space for 3. |
| **Page budget** | Track estimated page length. Don't suggest additions that would push resume past 1-2 pages. |
| **Explanation required** | Every suggestion includes a `reason` explaining WHY (e.g., "This project uses 4/5 of the required skills vs. your current project which matches 1/5"). This is shown to the user and fed to the Refactorer. |

**Decision Log**: We considered letting the LLM Refactorer make add/replace decisions itself, but LLMs are poor at data selection tasks (they lack the full context of the user's profile). The algorithmic matcher with relevance scores makes objectively better decisions, and the LLM can focus on what it's good at: writing.

---

### Challenge 13: Concurrent User Sessions (NEW)

**Problem**: Multiple users may submit job URLs simultaneously. Each triggers a full graph execution (scrape → extract → match → refactor → evaluate → review). How do we manage parallel sessions without blocking, state leakage, or resource exhaustion?

**Solution (MVP-scoped, ≤ 50-100 users)**:

| Strategy | Details |
|---|---|
| **Queue-based graph invocation** | Graph `ainvoke()` calls are submitted to an async task queue. The queue processes them at its own pace — no rate limiter needed at MVP scale. |
| **Per-session isolation** | Each graph execution gets a unique LangGraph `thread_id`. State is stored per-thread in PostgreSQL — zero cross-session interference. |
| **DB connection pool** | Pool size fixed to the allowable limit of the free-tier PostgreSQL subscription (typically 20-25 connections). Sufficient for MVP. |
| **Async FastAPI** | All endpoints are `async`. While one user's LLM call is in-flight (I/O bound, 3-5s), the event loop handles other users' requests. |
| **No memory spike mitigation** | Each graph state is ~50KB. Even 100 concurrent users = 5MB — negligible for MVP. Not a concern until we scale beyond 1000+ users. |

**Queue Architecture**:

```mermaid
sequenceDiagram
    participant U1 as User A
    participant U2 as User B
    participant API as FastAPI
    participant Q as Task Queue
    participant G as LangGraph

    U1->>API: POST /generate (Google SWE job)
    API->>Q: Enqueue graph invocation (thread: user-a-session-1)
    API-->>U1: 202 Accepted — processing

    U2->>API: POST /generate (Meta PM job)
    API->>Q: Enqueue graph invocation (thread: user-b-session-1)
    API-->>U2: 202 Accepted — processing

    Q->>G: ainvoke(state_a, thread="user-a-session-1")
    Q->>G: ainvoke(state_b, thread="user-b-session-1")
    Note over G: Both run concurrently (async I/O)

    G-->>Q: User A result ready
    Q-->>U1: SSE/WebSocket: resume preview ready

    G-->>Q: User B result ready
    Q-->>U2: SSE/WebSocket: resume preview ready
```

**How per-session isolation works**:

```python
# Each user's graph runs with its own thread_id
# LangGraph + PostgresSaver stores state per-thread — never shared

# User A submits a job URL
config_a = {"configurable": {"thread_id": "user-a-session-1"}}
await task_queue.enqueue(graph.ainvoke, initial_state_a, config=config_a)

# User B submits a different job URL (completely independent)
config_b = {"configurable": {"thread_id": "user-b-session-1"}}
await task_queue.enqueue(graph.ainvoke, initial_state_b, config=config_b)

# State for A and B stored in SEPARATE rows in PostgreSQL
# They NEVER interfere with each other
```

**Decision Log**: We considered adding a rate limiter (e.g., max 5 concurrent LLM calls) but decided it's unnecessary at MVP scale (≤ 50-100 users). A simple async queue is sufficient — the LLM API handles its own rate limits, and if a call fails due to rate limiting, LangGraph's checkpointing lets us retry from the last state. We'll revisit with a proper rate limiter (e.g., `asyncio.Semaphore` or Redis-based) when we scale beyond 100 users.

---

## Appendix: v0.1.0 → v0.2.0 Change Summary

| Aspect | v0.1.0 (Linear Pipeline) | v0.2.0 (Multi-Agent LangGraph) |
|---|---|---|
| Architecture | Linear: Scrape → Extract → Match → Refactor → Compile | Graph with cycles: 5 agents with self-correction loop |
| Backend | Node.js (Next.js API Routes) | Python (FastAPI + LangGraph) |
| Self-correction | ❌ None — single-shot generation | ✅ Evaluator → Refactorer loop (max 3 iterations) |
| Hallucination prevention | Post-hoc check (simple) | Multi-stage Evaluator agent with guardrails |
| Data selection | Simple scoring function | Dedicated Data Retriever agent with add/replace intelligence |
| GitHub freshness | Manual re-sync only | Multi-layer: passive, active, user-triggered, webhook |
| Keyword extraction | YAKE + RAKE only | **4-layer pipeline**: YAKE+RAKE → Taxonomy force-match → Section-aware weighting → User confirmation. Optimized for recall ≥ 90% |
| Human-in-the-loop | Basic accept/reject | **Iterative conversational loop** — user requests specific changes in natural language, Refactorer applies partial rewrites, repeats until user accepts |
| Concurrency | Not addressed | **Queue-based** `ainvoke()` with per-session LangGraph thread isolation. MVP-scoped: no rate limiter, pool size = free-tier limit |
| State management | Ad-hoc (pass data between functions) | Typed shared state with checkpointing |
| Resilience | No crash recovery | LangGraph checkpointing — resume from last state |
| Token efficiency | ~4,000 tokens (single pass) | ~4,000-13,000 tokens (1-3 iterations, optimized) |
