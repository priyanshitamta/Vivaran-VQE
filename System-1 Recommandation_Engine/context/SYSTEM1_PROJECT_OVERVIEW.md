# System 1 — Skill-Gap-to-Course Recommendation Engine: Project Overview

> **Purpose of this document.** This is an accurate, implementation-faithful description of the **System 1 Recommendation Engine** as it exists and runs today. It is written for other AI agents to read when producing reports, presentations, or documentation about the project. It describes the **active** build — data layer, gap computation, course ranking, HTTP API, persistence, frontend, and tests.
>
> Content that is intentionally not part of the active build (optional, disabled-by-default enhancements) is omitted from this overview on purpose.

---

## 1. At a glance

| Item | Value |
|---|---|
| **Project** | System 1 — Skill-Gap-to-Course Recommendation Engine |
| **Initiative** | Vivaran-VQE, SIH 2026, Team EdTech, Problem Statement SIH26101 |
| **Domain** | AI-enabled Skill Intelligence Platform for officials in India's Official Statistical System |
| **Language** | Python 3.10+ |
| **Web framework** | FastAPI + Uvicorn (ASGI) |
| **Data layer** | pandas (CSV loaded at startup) |
| **Validation / API contract** | Pydantic v2 response schemas |
| **Input datasets** | 4 core CSVs + 1 live growth store (Dataset-5) |
| **Test framework** | pytest + FastAPI `TestClient` (httpx) |
| **API docs** | Auto-generated Swagger UI at `/docs` |
| **Active components** | Backend service, web intake UI (served by the API), CLI intake tool, automated test suite |

---

## 2. Context and purpose

System 1 is one module of a three-part AI-enabled **Skill Intelligence Platform** for the Indian Official Statistical System:

- **System 1 (this module)** — *Recommendation Engine.* Given an official's current skill levels and their job role's required skill levels, compute the **skill gaps** and recommend **training courses** from a catalogue that close those gaps.
- **System 2 (separate, teammates)** — AI **quiz generator**. It also produces quiz results that can raise an official's verified skill level.
- **System 3 (separate, teammates)** — the full-stack **web application**. Owns login/auth and enrollment; calls System 1's API for the employee dashboard and admin dashboard, and calls System 2 for quizzes.

**System 1 does not** build quizzes, manage enrollment/completion, or handle authentication — those are explicitly out of scope (see §13). It operates purely on skill/competency and course data.

---

## 3. What the system does

For **any employee** (pre-loaded, newly registered, or never-seen):

1. Determines which skills the employee's **role requires** and at what **levels**.
2. Determines the employee's **current levels** — preferring **quiz-verified** levels over **self-rated** levels.
3. Computes each **gap** = required level − current level (only positive gaps are true gaps).
4. Ranks **courses from the catalogue** that best close the gaps, using a weighted tag-overlap score.
5. **Explicitly reports** any gap skill with **zero course coverage** (currently `S014` Cloud Computing) instead of silently dropping it.

**Business value:** a new official (or admin) can immediately see "these are the skills you're short on for your role, and these are the specific courses to take to close each gap."

---

## 4. Input datasets

Five CSVs. The four core datasets are read-only reference data; Dataset-5 is the live registration store that grows at runtime.

| Dataset | File | Contents | Size (rows) |
|---|---|---|---|
| Dataset-1 | `Dataset-1_Skill_Taxonomy.csv` | Skill reference taxonomy | 16 skills (S001–S016) |
| Dataset-2 | `Dataset-2_Required_Competency.csv` | Required skills + levels per role | 800 rows / ~60 roles (R001–R060) |
| Dataset-3 | `Dataset-3_Syn_Employee_Profiles.csv` | Synthetic employee profiles | 805 employees (E001–E805) |
| Dataset-4 | `Dataset-4_Course_Catalouge.csv` | Course catalogue | 784 courses (C001–C784) |
| Dataset-5 | `Dataset-5_Real_Employee_Profiles.csv` | Real (registered) employee profiles | Grows from 0; created on first write |

### Dataset-1 — Skill taxonomy
Columns: `skill_id`, `skill_name`, `category`, `description`, `proficiency_levels`.
- 16 skills across four categories: **Technical, Statistical, Digital Governance, Behavioural-Managerial**.
- All skills use a **3-level proficiency scale** (`1, 2, 3`). This is the reference table every other dataset's `skill_id` must point to.

### Dataset-2 — Required competency
Columns: `role_id`, `designation`, `department`, `skill_id`, `required_level`, `priority_weight`.
- Each role requires roughly 12–15 skills at levels 1–3.
- `priority_weight` (1–3) marks how critical a skill is for the role — used heavily in course scoring.
- `designation` (job title, e.g. "Director General") and `department` (e.g. "NSO") jointly define the human-readable role.

### Dataset-3 — Synthetic employee profiles
Columns: `employee_id`, `name`, `role_id`, `designation`, `department`, `current_assignment`, `educational_qualifications`, `work_experience_years`, `previous_trainings`, `self_rated_skills`, `quiz_verified_skills`.
- 805 synthetic people E001–E805. This is the **baseline workforce** the engine is validated against.
- Two **packed string formats**:
  - `self_rated_skills` / `quiz_verified_skills`: semicolon-separated `skill_id:level` pairs, e.g. `S001:2;S002:1`.
  - `previous_trainings`: comma-separated `course_id`s, e.g. `C649,C067`.
- `quiz_verified_skills` is a *subset* — only skills confirmed via quiz; quiz-verified values **override** self-ratings during gap computation.

### Dataset-4 — Course catalogue
Columns: `course_id`, `course_title`, `description`, `skill_tags`, `target_level`, `duration_minutes`, `mode`, `provider`, `language`.
- 784 courses C001–C784.
- `skill_tags`: comma-separated `skill_id`s the course addresses.
- `target_level`: the level (1–3) a course brings a learner to.
- `mode`: `self-paced` | `instructor-led` | `virtual-lab`; `language`: `English` | `Hindi` | `English/Hindi`.
- **Known data gap (intentional, not a bug):** no course is tagged with `S014` (Cloud Computing), which drives the explicit "unmatched gap" behaviour (see §6, Stage D).

### Dataset-5 — Real employee profiles (live store)
- Same schema as Dataset-3 **plus a `registered_at` timestamp**.
- Written by `POST /employees/register`; loaded at startup alongside Datasets 1–4. If absent on disk, the engine starts with **zero** real employees rather than failing.

### Data notes
- Source CSVs contain padding whitespace; the loader strips all whitespace from column names and string cells, and converts numeric columns to `int`.
- Packed fields are parsed **once at load time** into proper dictionaries/lists — business logic never re-parses raw strings.
- All foreign keys are validated defensively at startup (log-only, never raises), because datasets may be edited later.

---

## 5. Architecture and code layout

A strict **layering rule**: `routes → services → repositories → CSVs`. Routes only translate HTTP into service calls; services hold business logic; repositories are the single data-access layer; raw CSV parsing lives only in the data layer.

```
app/
  main.py                       # App assembly: load datasets, build repos & services,
                                #   CORS, mount routers, serve the web UI at "/"
  config.py                     # All paths + tunables (env-overridable via S1_*)
  data/
    loader.py                   # Stage A: read CSVs, clean, parse packed fields,
                                #   FK validation, thread-safe CSV writes
    repositories.py             # Skill / Role / Employee / Course repositories
  services/
    gap_service.py              # Stage B: skill-gap calculation
    recommendation_service.py   # Stage C+D: course ranking + unmatched-gap report
    registration_service.py     # Persist a new employee to Dataset-5 + compute
    analytics_service.py        # Admin aggregations over workforce segments
  models/
    schemas.py                  # Pydantic models = the stable JSON API contract
  api/
    routes.py                   # HTTP endpoints (thin, delegate to services)
frontend/
  index.html                    # Single-page intake UI, served by the API at "/"
tools/
  interactive_intake.py         # CLI intake wizard (terminal equivalent of the UI)
datasets/                       # The 4 core CSVs + Dataset-5 (grows)
tests/                          # pytest suite (see §11)
context/                        # Team docs + build spec
```

**Boot sequence** (`app/main.py`, `create_app`): load all datasets once → validate references → build the four repositories → construct the services → mount CORS + three routers → expose `/health` and the web UI at `/`. The whole app runs as **one process**; there is no separate database server.

---

## 6. The core pipeline (Stages A–F)

### Stage A — Data loading
`app/data/loader.py` reads the CSVs from `datasets/`, strips whitespace, parses packed fields, converts numerics, and runs log-only foreign-key validation. `app/data/repositories.py` wraps each cleaned table in a lookup object so services query by `skill_id` / `role_id` / `employee_id` / `course_id` without touching DataFrames.

### Stage B — Skill-gap calculation (`GapService`)
For an employee:
1. Resolve the role (`role_id`) → fetch its required skills + levels from Dataset-2.
2. Current level per skill: **quiz-verified** value if present, else **self-rated** value; if neither is recorded the skill is treated as level **0** (so the full requirement is a gap).
3. `gap = required_level − current_level`; **keep only gaps > 0**.
4. Return sorted **largest gap first**, tie-broken by higher priority weight, then skill_id for determinism.

Output object (one per gap):

```json
{
  "skill_id": "S002",
  "skill_name": "Communication",
  "current_level": 1,
  "required_level": 3,
  "gap": 2,
  "priority_weight": 3
}
```

An employee who already meets/exceeds every requirement gets an **empty list** — a valid answer, not an error.

### Stage C — Course ranking (`RecommendationService`)
For each gap skill, collect every course tagged with that skill, then score candidate courses:

1. **Matched weight** = sum of `priority_weight` across the gap skills the course covers.
2. **Level relevance** = how well the course's `target_level` matches the highest required level among its matched gaps:
   `relevance = 1 / (1 + |target_level − max_required_level|)` — 1.0 when exact, decreasing as the course overshoots or undershoots.
3. **Raw tag score** = `matched_weight × level_relevance`.
4. Scores are **normalised** so the best candidate scores 1.0, then courses are sorted descending (ties by `course_id`) and truncated to `top_n` (default 5, max 50).

Each recommendation carries the ordered `matched_skills` (the gap skills it covers, in descending priority) and the normalised `score`. Only courses genuinely tagged with a gap skill are ever recommended — no fabricated or irrelevant fillers.

Output object:

```json
{
  "course_id": "C002",
  "course_title": "Flood-Disaster Response Training",
  "description": "…",
  "target_level": 3,
  "duration_minutes": 3600,
  "mode": "instructor-led",
  "provider": "NDRF",
  "language": "English/Hindi",
  "matched_skills": ["S002", "S003"],
  "score": 1.0
}
```

### Stage D — Unmatched gaps (required behaviour)
A gap skill with **zero matching courses** (today: `S014` Cloud Computing) must be reported explicitly — never silently dropped, never crashed on, never "fixed" by inventing a course:

```json
{
  "skill_id": "S014",
  "skill_name": "Cloud Computing",
  "status": "no_courses_available",
  "message": "No courses currently available for Cloud Computing. This gap has been flagged for the training team."
}
```

The `status` field is **machine-readable**, so a frontend can render a warning banner rather than string-matching free text.

### Stage E — HTTP API
See §7. Exposes the three lookup endpoints plus the on-the-fly, registration, update, batch, analytics, meta, and health endpoints.

### Stage F — Sanity checks (automated)
Three behaviours the test suite locks in (see §11):
1. An **already-qualified** employee → empty/short gaps and minimal/empty recommendations, no error.
2. An employee with a gap in an **uncovered skill** (S014) → explicit `no_courses_available` report.
3. A **typical mixed-profile** employee → recommendations that are actually tagged with their gap skills and rank plausibly.

---

## 7. HTTP API surface

Interactive documentation is auto-generated at **`/docs`** (Swagger UI). Responses are validated Pydantic models whose key names **match the CSV column names exactly** (e.g. `skill_id`, `course_title`) — this is the stable integration contract.

### Endpoint catalogue

| Method | Path | Purpose |
|---|---|---|
| GET | `/employees/{employee_id}/gaps` | Skill gaps for one stored employee, largest first |
| GET | `/employees/{employee_id}/recommendations?top_n=5` | Ranked course recommendations + unmatched-gap report |
| GET | `/employees/{employee_id}/profile` | Raw employee profile (parsed packed fields) |
| POST | `/employees/compute?top_n=5` | Gaps + recommendations for a **new** employee from a request body — nothing saved |
| POST | `/employees/register?top_n=5` | Same, but **persists** the employee to Dataset-5 (returns 201) |
| POST | `/employees/{employee_id}/skills` | Update an employee's skill levels (System 2 quiz-writeback) |
| GET | `/employees/recommendations/batch?top_n=5` | Gaps + recommendations for **all** employees in one call (admin dashboard) |
| GET | `/analytics/synthetic` | Workforce analytics over the synthetic 805 only |
| GET | `/analytics/real` | Workforce analytics over real Dataset-5 registrations only |
| GET | `/analytics/combined` | Workforce analytics over the full workforce (both segments) |
| GET | `/analytics/registrations` | Every real employee, newest first (registration log) |
| GET | `/meta/skills` | All 16 skills (intake form builder) |
| GET | `/meta/roles` | All roles (`role_id` + `designation` + `department`) for role pickers |
| GET | `/meta/courses` | Full course catalogue (previous-trainings picker) |
| GET | `/meta/unique-values` | Distinct dropdown values per profile field |
| GET | `/health` | Liveness probe + loaded dataset counts |
| GET | `/` | The web intake UI (same-origin, no CORS) |

### Common response shapes

`GET /employees/{id}/recommendations` →

```json
{
  "employee_id": "E001",
  "recommendations": [ /* Recommendation objects, §6 Stage C */ ],
  "unmatched_gaps": [ /* UnmatchedGap objects, §6 Stage D */ ]
}
```

`POST /employees/compute` and `/register` (the "ComputeResponse") →

```json
{
  "employee": { /* full profile: id, name, role, designation, department,
                  assignment, qualifications, experience, previous_trainings,
                  self_rated_skills, quiz_verified_skills */ },
  "gaps": [ /* Gap objects */ ],
  "recommendations": [ /* Recommendation objects */ ],
  "unmatched_gaps": [ /* UnmatchedGap objects */ ]
}
```

### Compute vs Register (Approach A — new-employee intake)
- **`POST /employees/compute`** runs the exact same pipeline but is **stateless** — nothing is written. Ideal for a "preview". Omitted fields fall back to the role's own `designation`/`department`; an omitted `employee_id` echoes a `NEW-<role_id>` placeholder.
- **`POST /employees/register`** **persists** the profile: appends a row to Dataset-5 with a UTC `registered_at` timestamp and **hot-adds** the employee to the in-memory store, so they are immediately queryable through every GET endpoint. If no `employee_id` is supplied, the service allocates the next free id in the E-numbered space (E806, E807, …); a caller-supplied id that already exists returns **409**.
- Both accept the same `NewEmployeeInput` body and share one validation path.

### Skill update (System 2 integration)
`POST /employees/{employee_id}/skills` with a body such as:

```json
{ "quiz_verified_skills": { "S002": 3 } }
```

- At least one of `self_rated_skills` / `quiz_verified_skills` must be present.
- Merges levels into the employee's in-memory record **and persists them to disk** (Dataset-3 for synthetic employees, Dataset-5 for real ones), so the change survives a restart and the **very next gap call reflects it** — no stale startup snapshot.
- Returns the updated skill dictionaries; unknown employee → 404.

### Batch (admin dashboard)
`GET /employees/recommendations/batch` returns one entry per employee — synthetic + real:

```json
{
  "employee_id": "E001",
  "gaps": [/* Gap objects */],
  "recommendations": [/* Recommendation objects */],
  "unmatched_gaps": [/* UnmatchedGap objects */]
}
```

Each row mirrors exactly what the per-employee endpoints return, so the frontend renders a batch row the same way it renders a single result — and it never needs to loop hundreds of requests.

### Analytics (admin)
`/analytics/synthetic`, `/analytics/real`, `/analytics/combined` each return the same aggregation over their segment:

```json
{
  "segment": "synthetic",
  "count": 805,
  "by_department": [{"key": "NSO", "count": 120}, "…" (top 10)],
  "by_role": [{"key": "R001", "count": 14}, "…" (top 10)],
  "avg_gaps_per_employee": 4.63,
  "avg_total_gap_magnitude": 6.12,
  "fully_qualified_count": 42,
  "s014_gapped_count": 180
}
```

Segments stay **separate** so live registrations never pollute the synthetic baseline's statistics; gap metrics reuse the same `GapService` as everything else (one engine, no duplicated logic).

### Meta (frontend form-building helpers)
- `/meta/skills`, `/meta/roles`, `/meta/courses` — reference lists for building intake dropdowns.
- `/meta/unique-values` — returns the distinct values actually present in the datasets for `designation`, `department`, `current_assignment`, and `educational_qualifications`, so forms offer valid choices instead of free text.

### Health
`GET /health` → `{ "status": "ok", "employees_loaded": …, "employees_synthetic": 805, "employees_real": …, "courses_loaded": 784 }`. Lightweight liveness probe used by orchestration and by the frontend to detect the service is up.

### Validation and error handling
- **404** — unknown `employee_id` on the lookup / update endpoints.
- **422** — invalid `top_n` (outside 1–50); unknown `role_id`; unknown `skill_id`; skill level outside 1–3; `work_experience_years` outside 0–80; an empty skill-update body.
- **409** — registering an `employee_id` that already exists.
- No authentication/authorization — explicitly out of scope for this module.

---

## 8. Frontend and interactive intake

The module ships **two intake clients** — the same "pick a role, enter skills, see gaps + courses" flow in both.

### Web UI (served by the API)
`frontend/index.html` is served at **`http://localhost:8000/`** by the backend itself, so the page and the API share one origin (no CORS, no hardcoded port). The UI:
- Pulls `/meta/skills`, `/meta/roles`, `/meta/unique-values` to build **cascading dropdowns** (choose a designation → only valid departments appear → the role is derived from the pair).
- Collects **self-rated skills** and optional **quiz-verified skills** (skill + level 1–3 rows) and optional **previous trainings**.
- Submits to **`POST /employees/register?top_n=5`** and renders a results screen: employee ID, the skill-gap list, a warning banner for any unmatched gap, and ranked course cards.
- Shows clear in-page error messages (missing required fields, server rejections, or unexpected errors) rather than failing silently.

### CLI intake tool
`tools/interactive_intake.py` is the terminal equivalent. With the server running (`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`), run `python interactive_intake.py` and answer each prompt; type `?` at prompts that support it to see valid options. It posts to `/employees/register` and pretty-prints the same gaps / recommendations / unmatched-gap report.

---

## 9. Data integrity and persistence strategy

- **In-memory repositories** are built once at startup from the CSVs — single-employee lookups stay fast and never re-parse the whole file per request.
- **Dataset-5 writes are thread-safe** (guarded by a lock): append-on-register and in-place skill-updates cannot interleave.
- **Live updates**: skill updates and registrations modify the in-memory store **and** the correct CSV, so behaviour is consistent across the current process and a later restart.
- **Real vs synthetic** employees are held separately; lookups check real first (real takes precedence on any id collision), and the analytics service keeps the two segments independent.
- **Defensive reads**: blank CSV cells (read back as NaN) are normalised to `None` so profiles serialise cleanly; packed-field parsers tolerate empty/malformed values with warnings rather than crashing.

---

## 10. Configuration

All settings live in `app/config.py`, overridable with `S1_`-prefixed environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `S1_DATA_DIR` | `<repo>/datasets` | Where the CSV datasets live |
| `S1_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins (lock down before production) |
| *(fixed constants)* | — | `DEFAULT_TOP_N = 5`, `MAX_TOP_N = 50`, `MAX_LEVEL = 3`, Dataset-5 column order |

CORS is enabled from day one because the wider platform's frontend will call this module from a different origin during full-stack integration.

---

## 11. Running the system and testing

### Run the server

```bash
pip install -r context/requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Web UI: **http://localhost:8000/**
- Swagger API docs: **http://localhost:8000/docs**
- Health probe: **http://localhost:8000/health**

### Run the tests

```bash
python -m pytest -v
```

An automated suite of **50+ tests** across six files (all passing):

- `test_sanity.py` — the three Stage-F sanity checks over real HTTP, plus 404 / 422 paths, profile shape, and health.
- `test_gap_service.py` — gap shape/ordering, quiz-verified precedence, empty-gap edge case, role/skill validity across the whole workforce, spot-checks.
- `test_recommendation_service.py` — `top_n`, only-gap-skill coverage, empty/uncovered cases, S014 unmatched reporting, score ordering.
- `test_compute_endpoint.py` — on-the-fly compute: profile echo, id defaulting, already-qualified → empty, quiz precedence, S014 handling, **consistency guarantee** (compute matches the stored-employee path byte-for-byte for identical inputs), and 422 validation.
- `test_dataset5.py` — registration persistence to Dataset-5, immediate queryability, auto id assignment, duplicate-id 409, analytics segment separation, registration log, health counts.
- `test_integration_additions.py` — live skill updates reflected on the next call and persisted to disk, the batch endpoint (row shape, `top_n`, key names), machine-readable unmatched status, health speed, and the explicit absence of enrollment/quiz/auth fields.

---

## 12. Key design decisions

1. **Strict layering** — routes → services → repositories → CSVs; no raw CSV parsing in business logic, and routes contain no business logic.
2. **Stable, schema-validated JSON as the integration contract** — Pydantic models define every response; key names match dataset column names exactly so frontends bind directly.
3. **One shared pipeline for every entry point** — lookups, on-the-fly compute, and registration all run the same gap + ranking core (`…_for_employee` service methods), guaranteeing consistent answers.
4. **Quiz-verified beats self-rated** — the more trustworthy signal always wins when computing current levels.
5. **Explicit unmatched-gap reporting** — zero-coverage skills surface with a machine-readable `status`, never silently.
6. **Real and synthetic data stay separate** — the validated synthetic baseline is never polluted by live registrations; analytics compare the segments apples-to-apples.
7. **Hot in-memory updates + CSV persistence** — registrations and quiz-writebacks take effect immediately and survive restarts.
8. **Configurable and defensive** — env-var overrides, thread-safe file writes, NaN-normalising reads, and log-only foreign-key validation so the engine runs even if datasets are later edited.
9. **CORS enabled from day one** for the wider platform; no auth in this module.
10. **Dataset-4 doubles as the mock catalogue** — no live iGOT Karmayogi API integration; the known `S014` coverage gap is handled by design, not patched.

---

## 13. Scope boundaries (explicit non-goals)

This module deliberately does **not** implement, and keeps no state for:
- **MCQ/quiz generation** (System 2's responsibility).
- **Enrollment / completion tracking** — that state belongs to System 3's database, not here.
- **Authentication / authorization**.
- **Real iGOT Karmayogi API integration** — Dataset-4 is the mock course catalogue.

Its responsibility is narrow and stable: **skill gap computation + course recommendation over the five datasets, exposed as a clean HTTP service.**

---

## 14. Numbers that matter (recap)

- **16** skills (S001–S016) on a 1–3 scale · **~60** roles (R001–R060) · **800** role-skill requirement rows
- **805** synthetic employees (E001–E805) · **784** courses (C001–C784)
- **1** real-employee store (Dataset-5) that grows with every registration
- **16** API routes · **50+** passing automated tests · default `top_n = 5` (max 50)
