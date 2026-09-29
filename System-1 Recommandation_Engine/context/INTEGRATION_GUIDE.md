# Full-Stack Integration Guide — System 1 (Approach A)

## What changed

You asked whether the system can handle **new employees** who do not exist in the 805-person Dataset-3 CSV — i.e., when a person walks in with their own skill levels and target role, can the system compute their gaps and recommend courses on the fly?

**The original build** (per CLAUDE.md) only worked for pre-loaded employees E001–E805. A request for an unknown employee_id returned `404`.

**The upgrade** adds **Approach A** — a `POST /employees/compute` endpoint that accepts a complete employee profile in the request body (no CSV row needed) and returns the same Stage B/C/D outputs plus the profile echo in one response.

---

## The new endpoint

### `POST /employees/compute`

**Purpose**: On-the-fly gap calculation + course recommendations for a person who does NOT need to exist in Dataset-3.

**Use case**: The full-stack frontend's intake form — a new official picks their role, enters their skill levels (self-rated + quiz-verified where a quiz exists), and gets back their gaps, ranked course recommendations, and the unmatched-gap report.

**Request body** (`NewEmployeeInput` schema):

```json
{
  "employee_id": "E-NEW-001",        // optional; defaults to "NEW-<role_id>"
  "name": "Aarti Sharma",            // optional
  "role_id": "R012",                 // required; must exist in Dataset-2
  "designation": "...",              // optional; falls back to the role's designation
  "department": "...",               // optional; falls back to the role's department
  "current_assignment": null,        // optional
  "educational_qualifications": null, // optional
  "work_experience_years": null,     // optional, 0-80
  "previous_trainings": [],          // list of course_ids (optional)
  "self_rated_skills": {             // skill_id -> level (1-3)
    "S001": 2,
    "S002": 2,
    "S010": 1
  },
  "quiz_verified_skills": {          // skill_id -> level (1-3), optional
    "S001": 2
  }
}
```

**Query parameter**: `top_n` (default 5, range 1–50) — number of course recommendations to return.

**Response** (`ComputeResponse` schema):

```json
{
  "employee": {                       // echoed back — frontend sees what was computed from
    "employee_id": "E-NEW-001",
    "name": "Aarti Sharma",
    "role_id": "R012",
    "designation": "...",             // filled from role meta if omitted in request
    "department": "...",
    "current_assignment": null,
    "educational_qualifications": null,
    "work_experience_years": null,
    "previous_trainings": [],
    "self_rated_skills": {"S001": 2, "S002": 2, "S010": 1},
    "quiz_verified_skills": {"S001": 2}
  },
  "gaps": [                           // Stage B output — sorted largest-gap-first
    {
      "skill_id": "S002",
      "skill_name": "Communication",
      "current_level": 2,
      "required_level": 3,
      "gap": 1,
      "priority_weight": 3
    }
  ],
  "recommendations": [                // Stage C output — ranked courses
    {
      "course_id": "C002",
      "course_title": "Flood-Disaster Response Training",
      "description": "...",
      "target_level": 3,
      "duration_minutes": 3600,
      "mode": "instructor-led",
      "provider": "NDRF",
      "language": "English/Hindi",
      "matched_skills": ["S002", "S003"],
      "score": 1.0
    }
  ],
  "unmatched_gaps": [                 // Stage D output — gaps with zero course coverage
    {
      "skill_id": "S014",
      "skill_name": "Cloud Computing",
      "status": "no_courses_available",
      "message": "No courses currently available for Cloud Computing. This gap has been flagged for the training team."
    }
  ]
}
```

**Validation** (returns `422` on failure):
- `role_id` must exist in Dataset-2 (R001–R060)
- Every `skill_id` in `self_rated_skills` / `quiz_verified_skills` must exist in Dataset-1 (S001–S016)
- Skill levels must be 1–3

**Behavior**:
- Quiz-verified levels are preferred over self-rated (same precedence as the lookup endpoints)
- The exact same Stage B/C/D pipeline runs — identical output to the GET endpoints for the same inputs
- `designation` / `department` fall back to the role's own values when omitted

---

## Code changes

### 1. **Refactored services to separate lookup from computation**

**`app/services/gap_service.py`**:
- Existing `compute_gaps(employee_id: str)` now calls new `compute_gaps_for_employee(employee: dict)`
- The `_for_employee` variant is the shared core — works on any employee dict (pre-loaded or on-the-fly)

**`app/services/recommendation_service.py`**:
- Existing `recommend(employee_id: str, top_n)` now calls new `recommend_for_employee(employee: dict, top_n)`
- Same pattern — the `_for_employee` variant is the shared core

This means the GET lookup endpoints (for E001–E805) and the POST compute endpoint (for new employees) run **byte-identical** logic.

### 2. **Added repository accessors**

**`app/data/repositories.py`**:
- `RoleRepository.get_meta(role_id)` — returns `{designation, department}` for a role
- `RoleRepository.all_roles()` — returns every known `role_id`, sorted (useful for role pickers)

### 3. **Two new schemas**

**`app/models/schemas.py`**:
- `NewEmployeeInput` — the intake request body
- `ComputeResponse` — the full response (employee echo + gaps + recommendations + unmatched_gaps)

### 4. **The new route**

**`app/api/routes.py`**:
- `POST /employees/compute` — validates inputs, builds a parsed employee dict, calls the `_for_employee` services, returns `ComputeResponse`

### 5. **Full test coverage**

**`tests/test_compute_endpoint.py`** — 10 new tests:
- Profile echo correctness (including default `employee_id` when omitted)
- Already-qualified employee → no gaps, no recommendations
- Quiz-verified precedence
- S014 unmatched-gap handling (Stage D)
- **Consistency guarantee**: same inputs as a stored employee → byte-identical output to the GET endpoints
- `top_n` respected
- 422 validation (unknown role_id, unknown skill_id, out-of-range level)

---

## Running the tests

The full suite now has **32 tests** (22 original + 10 new):

```bash
python -m pytest -v
```

All tests should pass. The key guarantee is `test_compute_matches_stored_employee` — it proves the on-the-fly path and the lookup path produce identical results for the same person.

---

## Example curl request

```bash
curl -X POST "http://localhost:8000/employees/compute?top_n=5" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Aarti Sharma",
    "role_id": "R012",
    "self_rated_skills": {"S001": 2, "S002": 2, "S010": 1},
    "quiz_verified_skills": {"S001": 2}
  }'
```

---

## Integration with your full-stack frontend

### Typical flow

1. **User lands on the intake form**
   - Form has:
     - Role picker (dropdown or autocomplete) — you can pre-populate options by calling `GET /health` (it returns `employees_loaded`, `courses_loaded` counts) or by enumerating roles from a static Dataset-2 export
     - 16 skill sliders/inputs (S001–S016) for self-rating (1–3)
     - Optionally: quiz-verified overrides (if your frontend has quizzes)

2. **User submits the form**
   - Frontend sends `POST /employees/compute` with the payload
   - Backend returns the complete response in <200ms (Tier 1) or <500ms (Tier 2 with embeddings)

3. **Frontend displays the result screen**
   - **Profile summary** (from `response.employee`) — shows what was computed from
   - **Gaps table** (from `response.gaps`) — "You need to go from level X to level Y in these skills"
   - **Recommended courses** (from `response.recommendations`) — ranked cards with course details, score, matched skills
   - **Unmatched gaps alert** (from `response.unmatched_gaps`) — "No courses available yet for Cloud Computing — we've flagged this"

4. **Optional: persist the result in your system's database**
   - The response gives you everything you need to store a "recommendation session" record in your own DB
   - System 1 stays stateless — it does not persist new employees, and every call recomputes from scratch

---

## What did NOT change

- The 3 GET lookup endpoints (`/employees/{employee_id}/gaps`, `/recommendations`, `/profile`) still work exactly as before — they serve the analytics/admin view over the full 805-person workforce
- The 4 CSV datasets are still loaded at startup — no POST endpoint to add new employees to the in-memory dataset (that was never in scope)
- All Stage A/B/C/D/E/F requirements from CLAUDE.md are still met
- All 22 original tests still pass

---

## Summary

**Before**: System 1 was a query-only API for the 805 pre-loaded employees. A request for an unknown employee_id returned 404.

**After**: System 1 supports **both**:
- **Batch analytics mode** (GET endpoints) — look up gaps/recommendations for any of the 805 stored employees
- **Live intake mode** (POST endpoint) — compute gaps/recommendations on the fly for a brand-new person who doesn't exist in the CSV

The POST endpoint is the integration point for your full-stack frontend's user-facing intake flow. The GET endpoints remain for admin dashboards, analytics, and bulk reporting.
