# System 1 — Skill-Gap-to-Course Recommendation Engine

Backend service for the **AI-enabled Skill Intelligence Platform**. Computes
skill gaps for officials against their role requirements and recommends
training courses from the catalogue to close them.

## What it does

1. Loads 4 CSV datasets (skill taxonomy, role competency requirements,
   employee profiles, course catalogue).
2. For any employee, computes the gap between their current skill levels and
   the levels their role requires — preferring **quiz-verified** levels over
   self-ratings.
3. Recommends the top-N courses (default 5) that best cover the gap skills,
   scored by weighted tag-overlap (Tier 1) with an optional semantic
   embedding blend (Tier 2).
4. Explicitly reports gaps with **zero course coverage** (currently `S014`
   Cloud Computing) instead of silently dropping them.

## Requirements

- Python 3.10+
- Core deps: `pip install -r requirements.txt`

Optional (Tier 2 semantic ranking): `requirements-tier2.txt` — see below.

## Run the server

```bash
python -m uvicorn app.main:app --reload
# or
python -m app.main
```

Server starts on http://localhost:8000.

Interactive API docs (Swagger UI): **http://localhost:8000/docs**

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/employees/{employee_id}/gaps` | Skill gaps, largest first |
| GET | `/employees/{employee_id}/recommendations?top_n=5` | Ranked course recommendations + unmatched-gap report |
| GET | `/employees/{employee_id}/profile` | Raw employee profile (parsed packed fields) |
| POST | `/employees/compute?top_n=5` | On-the-fly gaps + recommendations for a **new** employee (full profile in body; no CSV row needed) |
| GET | `/health` | Liveness probe + loaded dataset counts |

Errors: `404` for unknown `employee_id`; `422` for invalid `top_n`
(outside 1–50), an unknown `role_id`, an unknown `skill_id`, or a skill
level outside 1–3. No authentication (out of scope for this module).

### On-the-fly compute (new-employee intake)

`POST /employees/compute` runs the exact same Stage B/C/D pipeline as the
lookup endpoints, but the person does **not** need to exist in Dataset-3 —
the full profile is sent in the request body. This is the entry point the
full-stack frontend uses when a new official registers: they pick a role and
enter their skill levels, and get their gaps, course recommendations and
unmatched-gap report back in one round trip.

```bash
curl -X POST "http://localhost:8000/employees/compute" \
  -H "Content-Type: application/json" \
  -d '{
    "employee_id": "E-NEW-001",
    "name": "Aarti Sharma",
    "role_id": "R012",
    "self_rated_skills": {"S001": 2, "S002": 2, "S010": 1},
    "quiz_verified_skills": {"S001": 2}
  }'
```

Response — the submitted profile echoed back (`designation`/`department`
fall back to the role's own values) plus all computed outputs:

```json
{
  "employee": {
    "employee_id": "E-NEW-001",
    "name": "Aarti Sharma",
    "role_id": "R012",
    "designation": "...",
    "department": "...",
    "current_assignment": null,
    "educational_qualifications": null,
    "work_experience_years": null,
    "previous_trainings": [],
    "self_rated_skills": {"S001": 2, "S002": 2, "S010": 1},
    "quiz_verified_skills": {"S001": 2}
  },
  "gaps": [
    {"skill_id": "S002", "skill_name": "Communication", "current_level": 2,
     "required_level": 3, "gap": 1, "priority_weight": 3}
  ],
  "recommendations": [
    {"course_id": "C002", "course_title": "Flood-Disaster Response Training",
     "description": "...", "target_level": 3, "duration_minutes": 3600,
     "mode": "instructor-led", "provider": "NDRF", "language": "English/Hindi",
     "matched_skills": ["S002", "S003"], "score": 1.0}
  ],
  "unmatched_gaps": [
    {"skill_id": "S014", "skill_name": "Cloud Computing",
     "status": "no_courses_available",
     "message": "No courses currently available for Cloud Computing. This gap has been flagged for the training team."}
  ]
}
```

Notes:
- `self_rated_skills` / `quiz_verified_skills` accept only skill levels 1–3;
  unknown `role_id` / `skill_id` values are rejected with `422` (helps the
  frontend catch typos at intake time).
- Quiz-verified levels are preferred over self-ratings, exactly as in the
  lookup endpoints.
- `employee_id` is optional; if omitted the response echoes a
  `NEW-<role_id>` placeholder.
- The GET lookup endpoints still serve the analytics/admin view over the
  full 805-person workforce.

### Example responses

`GET /employees/E001/gaps` →

```json
[
  {"skill_id": "S002", "skill_name": "Communication", "current_level": 2,
   "required_level": 3, "gap": 1, "priority_weight": 3}
]
```

`GET /employees/E001/recommendations` →

```json
{
  "employee_id": "E001",
  "recommendations": [
    {"course_id": "C002", "course_title": "Flood-Disaster Response Training",
     "description": "...", "target_level": 3, "duration_minutes": 3600,
     "mode": "instructor-led", "provider": "NDRF", "language": "English/Hindi",
     "matched_skills": ["S002", "S003"], "score": 1.0}
  ],
  "unmatched_gaps": [
    {"skill_id": "S014", "skill_name": "Cloud Computing",
     "status": "no_courses_available",
     "message": "No courses currently available for Cloud Computing. This gap has been flagged for the training team."}
  ]
}
```

## Tier 2 — semantic ranking (optional upgrade)

Tier 1 alone passes all sanity checks. Tier 2 blends embedding-based
cosine similarity into the ranking:

```bash
pip install -r requirements-tier2.txt --index-url https://download.pytorch.org/whl/cpu
set S1_ENABLE_EMBEDDINGS=1    # PowerShell: $env:S1_ENABLE_EMBEDDINGS="1"
python -m uvicorn app.main:app
```

Course descriptions are embedded **once at startup** (`all-MiniLM-L6-v2`,
~90 MB download on first run), not per request. If the model fails to load,
the app degrades gracefully to Tier 1 with a warning.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `S1_DATA_DIR` | repo root | Where the 4 CSVs live |
| `S1_ENABLE_EMBEDDINGS` | `0` | Enable Tier 2 semantic ranking |
| `S1_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |
| `S1_SEMANTIC_WEIGHT` | `0.35` | Blend weight for cosine score |
| `S1_SEMANTIC_THRESHOLD` | `0.30` | Cosine floor for semantic admission |
| `S1_CORS_ORIGINS` | `*` | Comma-separated allowed CORS origins |

## Tests

```bash
python -m pytest -v
```

Covers the three Stage-F sanity checks plus unit tests for gaps,
recommendations, unmatched-gap handling, and endpoint error paths.

## Project layout

```
app/
  main.py                  # App assembly: load data, wire services, CORS
  config.py                # All settings
  data/loader.py           # CSV loading, whitespace stripping, FK validation
  data/repositories.py     # Skill/Role/Employee/Course repositories
  services/gap_service.py  # Stage B: skill-gap calculation
  services/recommendation_service.py  # Stage C+D: ranking + unmatched gaps
  services/embeddings.py   # Tier 2: embedding engine (optional)
  models/schemas.py        # Pydantic response models (API contract)
  api/routes.py            # The 4 endpoints (3 lookups + on-the-fly compute)
tests/                     # pytest suite incl. Stage-F sanity checks
```
