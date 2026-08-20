# ResumeForge — Problem Statement & Requirements Specification

> **Version**: 0.1.0 (Draft)
> **Last Updated**: 2026-07-29
> **Author**: Aditya

---

## 1. Problem Statement

### 1.1 The Landscape

In the current job market, a single job opening receives **10,000+ applications** on average. No human HR team can manually review every application. As a result, companies deploy **Applicant Tracking Systems (ATS)** that automatically score and filter resumes. Applications with a resume score ≤ `min_score` are rejected in milliseconds — before any human ever reads them.

This means **talented candidates are being rejected not because they lack skills, but because their resumes aren't optimized for ATS keyword matching and formatting**.

### 1.2 Why Existing Solutions Fail

| Problem | Details |
|---|---|
| **Poor Formatting** | Existing tools break LaTeX/custom template structures, producing generic-looking resumes |
| **No Template Retention** | User's carefully crafted resume design is discarded and replaced with a cookie-cutter format |
| **No LaTeX Editing** | Users can't make fine-grained changes to their LaTeX source and see results |
| **Manual Input Required** | Users must manually copy-paste full job descriptions instead of just providing a URL |
| **Zero Transparency** | No explanation of *what* was changed, *why*, and how it aligns with the JD |
| **Surface-Level Changes** | Only modifies existing resume text — never pulls in relevant projects, skills, or experiences the user actually has but didn't include |
| **No Factual Consistency** | LLMs hallucinate skills, experiences, and achievements the user never had |

### 1.3 Our Solution — ResumeForge

A **chat-based web application** (UI similar to ChatGPT/Claude/Gemini) that:

1. Accepts a **job posting URL** from the user
2. **Extracts keywords** from the posting using an optimized algorithm (no LLM — minimizing token usage)
3. **Retrieves relevant data** from the user's stored profile (GitHub repos, experiences, skills, achievements)
4. Passes the matched data to an **LLM to refactor the resume** while preserving the user's LaTeX template structure
5. Shows a **diff/preview** for the user to review, modify, or accept
6. Generates a **downloadable PDF** from the final LaTeX source

---

## 2. Functional Requirements

### FR-01: Authentication & User Management

| ID | Requirement | Priority |
|---|---|---|
| FR-01.1 | Users must be able to sign up and sign in using **Google OAuth 2.0** | P0 |
| FR-01.2 | User sessions must persist across browser tabs and reasonable durations (7-day refresh tokens) | P0 |
| FR-01.3 | Users must be able to sign out and revoke access | P0 |
| FR-01.4 | Each user has an isolated profile and data store | P0 |
| FR-01.5 | Account deletion must cascade-delete all stored user data | P1 |

### FR-02: User Profile & Data Store

| ID | Requirement | Priority |
|---|---|---|
| FR-02.1 | Users can **upload their resume LaTeX source code** (.tex file or paste raw LaTeX) | P0 |
| FR-02.2 | Users can **update their LaTeX code** at any time (font, formatting, structure) | P0 |
| FR-02.3 | Users can connect their **GitHub account** (OAuth) to authorize repo access | P0 |
| FR-02.4 | System extracts and indexes: repo names, descriptions, READMEs, tech stacks (from languages API), and topics | P0 |
| FR-02.5 | Users can add **work experiences** — each with: company, role, duration, bullet points, and an optional detailed document of what they did | P0 |
| FR-02.6 | Users can add **achievements & certifications** — title, issuer, date, description, verification URL | P1 |
| FR-02.7 | Users can add **skills** — categorized as: proficient, familiar, currently learning | P1 |
| FR-02.8 | Users can add **education details** — institution, degree, GPA, relevant coursework | P1 |
| FR-02.9 | Users can add **any other freeform information** (publications, talks, volunteering, etc.) | P2 |
| FR-02.10 | All stored data must be editable and deletable by the user | P0 |

### FR-03: Job URL Processing & Keyword Extraction

| ID | Requirement | Priority |
|---|---|---|
| FR-03.1 | User pastes a **job posting URL** into the chat interface | P0 |
| FR-03.2 | System **scrapes/fetches the job posting** content from the URL | P0 |
| FR-03.3 | System supports common job platforms: LinkedIn, Indeed, Glassdoor, Lever, Greenhouse, Workday, company career pages | P0 |
| FR-03.4 | System **extracts keywords** using an optimized non-LLM algorithm (TF-IDF, RAKE, YAKE, or similar) | P0 |
| FR-03.5 | Keywords are categorized: **hard skills**, **soft skills**, **tools/technologies**, **domain knowledge**, **qualifications** | P1 |
| FR-03.6 | System shows the extracted keywords to the user for transparency before proceeding | P1 |
| FR-03.7 | User can **add/remove/modify extracted keywords** before resume generation | P2 |
| FR-03.8 | System handles edge cases: broken URLs, login-walled pages, redirects, CAPTCHA-blocked pages (graceful error with fallback: manual paste) | P0 |

### FR-04: Data Matching & Retrieval

| ID | Requirement | Priority |
|---|---|---|
| FR-04.1 | System matches extracted keywords against the user's stored data (projects, experiences, skills, achievements) | P0 |
| FR-04.2 | Matching uses a **relevance scoring algorithm** — not just exact string match but semantic similarity (embeddings or synonym expansion) | P1 |
| FR-04.3 | System retrieves a **ranked list of relevant items** from the user's profile | P0 |
| FR-04.4 | Retrieved data is shown to the user transparently: "Here's what I found relevant from your profile" | P1 |
| FR-04.5 | User can **override**: add items the system missed, remove irrelevant ones | P2 |

### FR-05: Resume Refactoring (LLM-Powered)

| ID | Requirement | Priority |
|---|---|---|
| FR-05.1 | System sends the user's LaTeX template, matched data, and extracted keywords to an LLM | P0 |
| FR-05.2 | LLM refactors the resume to **maximize ATS score** while preserving the user's template structure | P0 |
| FR-05.3 | LLM **must not hallucinate** — it can only use data from the user's stored profile | P0 |
| FR-05.4 | LLM output is the **modified LaTeX source code** | P0 |
| FR-05.5 | System enforces **factual consistency** — cross-checks LLM output against user's stored data | P1 |
| FR-05.6 | System provides a **changelog/diff** showing exactly what was changed and why | P1 |
| FR-05.7 | Token usage is minimized — only relevant data is sent to the LLM (not the entire user profile) | P0 |

### FR-06: Review & Modification

| ID | Requirement | Priority |
|---|---|---|
| FR-06.1 | System shows a **preview popup/modal** with the refactored resume (rendered PDF preview + LaTeX diff) | P0 |
| FR-06.2 | User can **edit the LaTeX source** directly in the popup | P0 |
| FR-06.3 | User can **request further modifications** via chat (e.g., "make the experience section more concise") | P1 |
| FR-06.4 | User can **accept** the refactored resume | P0 |
| FR-06.5 | User can **reject and regenerate** with different parameters | P1 |
| FR-06.6 | Live LaTeX preview updates as the user edits | P2 |

### FR-07: PDF Generation & Download

| ID | Requirement | Priority |
|---|---|---|
| FR-07.1 | Upon acceptance, system **compiles the LaTeX** to PDF | P0 |
| FR-07.2 | User can **download the PDF** | P0 |
| FR-07.3 | System stores a **history of generated resumes** per user (job URL → generated resume) | P1 |
| FR-07.4 | User can re-download previously generated resumes | P1 |

### FR-08: Chat Interface

| ID | Requirement | Priority |
|---|---|---|
| FR-08.1 | Chat-based UI similar to ChatGPT/Claude/Gemini | P0 |
| FR-08.2 | Supports **conversation history** within a session | P0 |
| FR-08.3 | Shows **typing indicators** and streaming responses | P1 |
| FR-08.4 | Supports **multiple chat sessions** (one per job application) | P1 |
| FR-08.5 | Chat sidebar with history of past sessions | P1 |

---

## 3. Non-Functional Requirements

### NFR-01: Performance

| ID | Requirement | Target |
|---|---|---|
| NFR-01.1 | Job URL scraping and keyword extraction | < 5 seconds |
| NFR-01.2 | Data matching and retrieval | < 2 seconds |
| NFR-01.3 | LLM resume refactoring (end-to-end) | < 30 seconds |
| NFR-01.4 | LaTeX to PDF compilation | < 10 seconds |
| NFR-01.5 | Page load time (initial) | < 3 seconds |

### NFR-02: Cost Efficiency

| ID | Requirement | Details |
|---|---|---|
| NFR-02.1 | **Minimize LLM token usage** — keyword extraction must NOT use LLM | Critical |
| NFR-02.2 | Only send relevant, filtered data to the LLM — not the entire user profile | Critical |
| NFR-02.3 | Cache keyword extraction results for identical URLs | Recommended |
| NFR-02.4 | Target < 4,000 input tokens per resume refactoring call | Goal |

### NFR-03: Security

| ID | Requirement | Details |
|---|---|---|
| NFR-03.1 | All API keys stored server-side, never exposed to client | Critical |
| NFR-03.2 | OAuth tokens encrypted at rest | Critical |
| NFR-03.3 | User data isolated — no cross-user data leakage | Critical |
| NFR-03.4 | HTTPS enforced in production | Critical |
| NFR-03.5 | Rate limiting on scraping and LLM endpoints | Required |
| NFR-03.6 | Input sanitization on all user-provided data (LaTeX injection prevention) | Required |

### NFR-04: Scalability

| ID | Requirement | Details |
|---|---|---|
| NFR-04.1 | Support **100 concurrent users** in initial release | Target |
| NFR-04.2 | Stateless API design for horizontal scaling | Required |
| NFR-04.3 | Database must handle 10,000+ user profiles | Target |
| NFR-04.4 | Background job queue for PDF compilation | Required |

### NFR-05: Reliability

| ID | Requirement | Details |
|---|---|---|
| NFR-05.1 | Graceful degradation when job URL scraping fails (fallback to manual paste) | Required |
| NFR-05.2 | LLM API failure handling with retry logic (exponential backoff) | Required |
| NFR-05.3 | Data persistence — user data must survive server restarts | Critical |
| NFR-05.4 | LaTeX compilation errors surfaced to user with actionable messages | Required |

### NFR-06: Usability

| ID | Requirement | Details |
|---|---|---|
| NFR-06.1 | Responsive design (desktop-first, mobile-friendly) | Required |
| NFR-06.2 | Dark mode support | Recommended |
| NFR-06.3 | Accessibility (WCAG 2.1 AA) | Recommended |
| NFR-06.4 | Intuitive onboarding flow for first-time users | Required |

### NFR-07: Maintainability

| ID | Requirement | Details |
|---|---|---|
| NFR-07.1 | Modular architecture — components loosely coupled | Required |
| NFR-07.2 | API documentation (OpenAPI/Swagger) | Required |
| NFR-07.3 | Comprehensive logging for debugging | Required |
| NFR-07.4 | Environment-based configuration (dev/staging/prod) | Required |

---

## 4. User Stories

### Epic 1: Onboarding
- **US-01**: As a new user, I want to sign in with my Google account so that I don't need to create yet another password.
- **US-02**: As a new user, I want a guided setup that walks me through connecting GitHub, uploading my LaTeX resume, and adding my experiences so I'm ready to use the tool quickly.
- **US-03**: As a user, I want to paste my LaTeX resume code and see a rendered preview so I know it's formatted correctly.

### Epic 2: Profile Management
- **US-04**: As a user, I want to add and edit my work experiences with detailed descriptions so the system has rich data to work with.
- **US-05**: As a user, I want to connect my GitHub so the system automatically knows about my projects and tech stack.
- **US-06**: As a user, I want to update my LaTeX template whenever I change my resume design (fonts, spacing, sections).

### Epic 3: Resume Generation
- **US-07**: As a user, I want to paste a job URL and have the system automatically extract what the job requires so I don't have to read and summarize it myself.
- **US-08**: As a user, I want to see which keywords were extracted so I understand what the system is optimizing for.
- **US-09**: As a user, I want the system to show me which of my experiences/projects are relevant to this job so I can verify correctness.
- **US-10**: As a user, I want my resume refactored to match the job requirements while keeping my formatting exactly as I designed it.
- **US-11**: As a user, I want to see a diff of what was changed so I have full transparency.
- **US-12**: As a user, I want to make manual edits to the generated LaTeX before finalizing.
- **US-13**: As a user, I want to download the final resume as a PDF.

### Epic 4: History & Iteration
- **US-14**: As a user, I want to see my past chat sessions so I can revisit previous job applications.
- **US-15**: As a user, I want to regenerate a resume with different parameters if I'm not satisfied.

---

## 5. Out of Scope (v1)

The following are explicitly **not** in scope for the initial release:

- Auto-applying to jobs on behalf of the user
- Cover letter generation
- Interview preparation features
- Multi-language resume support (English only in v1)
- Team/enterprise features
- Payment/subscription system (free in v1)
- Mobile native apps (web only)
- Browser extension for auto-detecting job pages
- ATS score prediction/estimation

---

## 6. Success Metrics

| Metric | Target |
|---|---|
| Time from URL paste to downloadable PDF | < 60 seconds |
| User-reported ATS pass rate improvement | ≥ 30% |
| Factual accuracy of generated resumes | 100% (no hallucinations) |
| Average LLM tokens per refactoring call | < 4,000 input tokens |
| User retention (weekly active) | ≥ 40% after 4 weeks |
