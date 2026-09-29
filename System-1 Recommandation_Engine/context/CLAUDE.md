# System 1 — Recommendation Engine: Agent Build Spec

## 0. Role of this document
You are a coding agent. This document is a complete, self-contained specification for building **System 1** of a larger platform — the **Skill-Gap-to-Course Recommendation Engine**. Four CSV datasets are provided alongside this file. Your job is to build a working backend service (with a minimal API) that implements the pipeline described below, using those datasets as the data layer. Do not invent additional scope beyond what is specified here. Where a decision is left open, make the simplest reasonable choice and state the assumption in code comments.

---

## 1. Problem context (why this exists)

This is part of an AI-enabled Skill Intelligence Platform for officials in India's Official Statistical System. Officials have a competency profile (skills at certain levels). Each job role requires certain skills at certain levels. The gap between what an official has and what their role requires should be computed, and the system should recommend relevant training courses from a catalogue to close that gap. This module (System 1) does NOT include the quiz/MCQ generator (that is a separate system) — build only the recommendation engine described here.

---

## 2. Input datasets (exact schemas — do not assume different column names)

All 4 files are CSVs with a header row. Load them as-is; strip whitespace from all string fields and column names on load (fields in the source CSVs contain padding whitespace).

### `Dataset-1_Skill_Taxonomy.csv`
| Column | Type | Notes |
|---|---|---|
| `skill_id` | string | Format `S001`–`S016`. Primary key. |
| `skill_name` | string | e.g. "AI/ML", "Communication" |
| `category` | string | One of: `Technical`, `Statistical`, `Digital Governance`, `Behavioural-Managerial` |
| `description` | string | Free text, used for embedding/semantic matching |
| `proficiency_levels` | string | Currently always `"1,2,3"` — a 3-level scale |

16 rows. This is the reference table — every `skill_id` used elsewhere must exist here.

### `Dataset-2_Required_Competency.csv`
| Column | Type | Notes |
|---|---|---|
| `role_id` | string | Format `R001`–`R060`. Foreign key target for Dataset 3. |
| `designation` | string | Job title, e.g. "Director General" |
| `department` | string | e.g. "NSO" |
| `skill_id` | string | Foreign key → Dataset 1. Format `S0XX` (already normalized). |
| `required_level` | int | 1, 2, or 3 |
| `priority_weight` | int | 1, 2, or 3 — how critical this skill is for the role |

800 rows. Each `role_id` has multiple rows (one per required skill — roughly 12–15 skills per role). Note: not every role requires every skill, and `S016` currently has zero required-competency rows (no role requires it) — handle this as a normal empty-result case, not an error.

### `Dataset-3_Syn_Employee_Profiles.csv`
| Column | Type | Notes |
|---|---|---|
| `employee_id` | string | Format `E001`–`E805`. Primary key. |
| `name` | string | Synthetic name |
| `role_id` | string | Foreign key → Dataset 2's `role_id` |
| `designation` | string | Redundant with role_id's designation, kept for convenience |
| `department` | string | |
| `current_assignment` | string | Free text |
| `educational_qualifications` | string | |
| `work_experience_years` | int | |
| `previous_trainings` | string | Comma-separated list of `course_id`s (e.g. `"C649,C067,C267,C057"`). All values are valid `course_id`s from Dataset 4. |
| `self_rated_skills` | string | Semicolon-separated `skill_id:level` pairs, e.g. `"S001:2;S002:1;S003:2"`. Covers all 16 skills for most employees. |
| `quiz_verified_skills` | string | Same format as above, but a **subset** of `self_rated_skills` — only skills that have been confirmed via a quiz. Not every skill has a quiz-verified entry. |

805 rows.

### `Dataset-4_Course_Catalouge.csv`
| Column | Type | Notes |
|---|---|---|
| `course_id` | string | Format `C001`–`C784`. Primary key. |
| `course_title` | string | |
| `description` | string | Free text — used for embedding/semantic matching. Note: a handful of rows (~5) have low-information/placeholder-style descriptions (e.g. just repeating the title). Do not special-case these; treat as normal data. |
| `skill_tags` | string | Comma-separated `skill_id`s this course addresses, e.g. `"S013,S010"`. Format already normalized to match Dataset 1. |
| `target_level` | int | 1, 2, or 3 — level this course brings a learner to |
| `duration_minutes` | int | Course length. Note: no zero values remain (already cleaned). Some values are very large (>100,000) — legitimate, do not filter out. |
| `mode` | string | `self-paced`, `instructor-led`, or `virtual-lab` |
| `provider` | string | Issuing body, e.g. "iGOT", "Ministry of Mines" |
| `language` | string | `English`, `Hindi`, or `English/Hindi` |

784 rows. **Known gap**: no course currently has `S014` (Cloud Computing) in its `skill_tags`. This is intentional, not a bug — see Section 5 for required handling.

---

## 3. Data relationships (for joins)

```
Dataset-1.skill_id  ←── referenced by ──  Dataset-2.skill_id
Dataset-1.skill_id  ←── referenced by ──  Dataset-3.self_rated_skills (parsed keys)
Dataset-1.skill_id  ←── referenced by ──  Dataset-3.quiz_verified_skills (parsed keys)
Dataset-1.skill_id  ←── referenced by ──  Dataset-4.skill_tags (parsed, comma-split)

Dataset-2.role_id   ←── referenced by ──  Dataset-3.role_id

Dataset-4.course_id ←── referenced by ──  Dataset-3.previous_trainings (parsed, comma-split)
```

All foreign keys are currently valid (no dangling references) as of this dataset version. Still, write defensive code — do not assume this will always hold if datasets are edited later.

---

## 4. Pipeline to implement

### Stage A — Data loading layer
Load all 4 CSVs into your chosen data structures (pandas DataFrames or equivalent). Provide a clean accessor/repository layer — do not scatter raw CSV parsing throughout business logic. Parse the semicolon/comma-packed fields (`self_rated_skills`, `quiz_verified_skills`, `skill_tags`, `previous_trainings`) into proper lists/dicts at load time.

### Stage B — Skill-gap calculation
Given an `employee_id`:
1. Look up their `role_id` → find that role's required skills + levels from Dataset 2.
2. Look up the employee's current skill levels from Dataset 3's `self_rated_skills` (prefer `quiz_verified_skills` for a skill if present — it's more trustworthy than self-rating; fall back to `self_rated_skills` otherwise).
3. For each required skill: `gap = required_level - current_level`. Only skills where `gap > 0` are true gaps.
4. Return a sorted list of gaps, largest gap first, including `priority_weight` from Dataset 2 for later use in scoring.

Output shape (example):
```json
[
  {"skill_id": "S002", "skill_name": "Communication", "current_level": 1, "required_level": 3, "gap": 2, "priority_weight": 3},
  ...
]
```

### Stage C — Course matching / recommendation
Two-tier approach — implement Tier 1 first, Tier 2 is an upgrade:

**Tier 1 (build first): Tag-overlap scoring**
- For each gap skill, find all courses in Dataset 4 whose `skill_tags` include that `skill_id`.
- Score each candidate course: `score = sum(priority_weight of matched gap skills) * (course.target_level relevance to required_level)`. Simplest version: just count how many of the employee's gap skills a course addresses, weighted by `priority_weight`.
- Rank and return top-N (default N=5) courses per employee.

**Tier 2 (upgrade once Tier 1 works): Embedding-based semantic matching**
- Precompute embeddings for each course's `description` field (use `sentence-transformers`, model `all-MiniLM-L6-v2` or similar) — do this once at startup/build time, not per-request.
- Represent each gap skill as a short text (e.g. `"{skill_name}: {skill.description}"`), embed it the same way.
- Compute cosine similarity between gap-skill embeddings and course embeddings.
- Blend this similarity score with the Tier 1 tag-overlap score (e.g. weighted sum) for final ranking.

### Stage D — Handling unmatched gaps (required behavior, not optional)
If a gap skill has **zero matching courses** in Dataset 4 (this will currently happen for `S014`), the API response must explicitly report this rather than silently omitting it:
```json
{
  "skill_id": "S014",
  "skill_name": "Cloud Computing",
  "status": "no_courses_available",
  "message": "No courses currently available for Cloud Computing. This gap has been flagged for the training team."
}
```
Do not crash, return an empty array with no explanation, or fabricate a course.

### Stage E — API layer
Expose at minimum:
```
GET /employees/{employee_id}/gaps
  → returns Stage B output

GET /employees/{employee_id}/recommendations?top_n=5
  → returns Stage C output, plus any Stage D unmatched-gap entries

GET /employees/{employee_id}/profile
  → returns raw employee profile (for frontend display)
```
Use whatever backend framework you judge simplest to stand up quickly (FastAPI/Express are both reasonable) — pick one and be consistent. Return JSON. Include basic error handling (404 for unknown `employee_id`, etc.) — do not build authentication/authorization for this module, that belongs to a different part of the platform.

### Stage F — Sanity test cases (run before declaring done)
- An employee whose skills already meet/exceed all requirements → gaps list should be empty or very short → recommendations should be empty or minimal, not an error.
- An employee with a large gap in a skill with zero course coverage (e.g. anyone gapped on `S014`) → must produce the Stage D response, not a silent failure.
- An employee with a typical mixed profile → recommendations should be plausible (courses actually tagged with their gap skills should rank near the top).

---

## 5. Explicit non-goals for this module
- Do not build the MCQ/quiz generator (separate system).
- Do not build the dashboards/frontend (separate concern — this module only needs to expose the API).
- Do not build real iGOT Karmayogi API integration — Dataset 4 already stands in as the mock catalogue.
- Do not attempt to "fix" the `S014` coverage gap by inventing a course — the correct behavior is the explicit no-courses-available response in Stage D.
- Do not implement authentication.

## 6. Deliverable
A working backend module/service, runnable locally, that:
1. Loads the 4 datasets.
2. Exposes the 3 endpoints in Stage E.
3. Passes the 3 sanity checks in Stage F.
4. Includes a short `README.md` explaining how to run it and what each endpoint returns.
