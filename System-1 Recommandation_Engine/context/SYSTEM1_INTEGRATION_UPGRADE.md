# Vivaran-VQE — System 1 Integration Upgrade: Required Additions

## Read this before touching your existing code

You have already built System 1 — the Recommendation Engine (data loading, gap calculation, course matching, and the core API endpoints). This document does **not** ask you to rebuild anything. It tells you what's **missing** for your existing work to plug cleanly into the rest of the project: System 2 (AI Quiz Generator, built by teammates) and System 3 (the full-stack web app, built by teammates). Treat everything below as a punch list of additions/adjustments on top of what already exists — not a new spec to build from scratch.

**Project:** Vivaran-VQE — SIH 2026, Team EdTech, Problem Statement SIH26101

---

## 1. Where your existing work sits in the bigger picture

Quick reminder of the three systems, so the additions below make sense:

- **System 1 (you)** — employee_id in → skill gaps + recommended courses out.
- **System 2 (teammates)** — uploaded document in → AI-generated quiz out; also produces quiz-attempt results that should raise an employee's verified skill level.
- **System 3 (teammates)** — the actual website. Calls your API for the Employee Dashboard and the Admin Dashboard, calls System 2's API for quizzes, owns login/auth and enrollment tracking.

Your code becomes a service that System 3 calls repeatedly, for different employees, at different times, sometimes right after System 2 has changed something about an employee's skills. The additions below exist because a "build it once, run it once" script is not the same thing as "a service other code can call safely, over and over, with changing data."

---

## 2. Required additions — go through these one at a time

### Addition 1 — Confirm you're reading skill levels fresh, not cached at startup
**Why:** Later, when System 2 verifies a quiz result, it updates an employee's `quiz_verified_skills`. Your gap calculation must pick that up on the very next call — not require a restart, not use an in-memory snapshot taken once at boot.
**What to check/change:** If your data loading layer reads all 4 CSVs once into memory at startup and never re-reads them, that's fine for the static reference datasets (Dataset 1, 2, 4 — these don't change at runtime). But Dataset 3 (employee profiles) is the one that will change. Make sure your gap-calculation function re-reads or re-fetches the specific employee's current `self_rated_skills`/`quiz_verified_skills` at call time, not from a stale in-memory copy taken at startup. If you're currently loading everything once into a single global DataFrame and never refreshing it, add a re-read step (or swap Dataset 3's loading to a per-request fetch) before calling this done.

### Addition 2 — Confirm your API can be called for one employee cheaply, and also in a loop
**Why:** The Employee Dashboard calls you once, for one person. The Admin Dashboard needs an org-wide rollup — which means System 3 will likely call your `/employees/{id}/recommendations` endpoint once per employee, across all 805 employees, to build aggregate charts.
**What to check/change:** Time a single call. If it's fast (sub-second), you're fine — no changes needed, this addition is just a verification step, not necessarily new code. If your endpoint reloads/reprocesses the full datasets from disk on every single call, that will be too slow across 805 sequential calls — add a startup-time load for the static datasets (1, 2, 4) so only the per-employee lookup is done per call, not a full CSV re-parse.

### Addition 3 — Add an explicit "batch" endpoint (new addition, likely doesn't exist yet)
**Why:** Rather than making System 3 loop 805 individual HTTP calls for the Admin Dashboard, give it one efficient way to get everyone's gaps/recommendations at once.
**What to add:**
```
GET /employees/recommendations/batch
  → returns an array of { employee_id, gaps, recommendations } for ALL employees
```
This is new work, not a modification of existing endpoints — add it alongside what you have, don't replace your per-employee endpoint.

### Addition 4 — Verify your "no courses available" response shape is actually distinguishable by a frontend
**Why:** System 3 needs to render this differently from a normal recommendation (e.g., a warning banner instead of a course card).
**What to check/change:** Confirm your response includes a clear `status` field (e.g. `"status": "no_courses_available"`) on any gap entry with zero course matches — not just an empty `recommendations` array with no explanation. If you already return a message but no machine-readable `status` field, add one — free text alone forces the frontend to string-match your message, which is fragile.

### Addition 5 — Double check your JSON key names match the dataset column names exactly
**Why:** Teammates on System 3 will bind your JSON directly to UI fields and will assume names like `skill_id`, `skill_name`, `current_level`, `required_level`, `gap`, `course_id`, `course_title` — matching the CSV columns they already know from the dataset docs.
**What to check/change:** Diff your actual response payload against this list. Rename anything that drifted (e.g., if you used `skillId` camelCase instead of `skill_id` snake_case, or `courseName` instead of `course_title`) — small naming mismatches are the most common integration break and the cheapest to fix now versus after teammates start wiring against you.

### Addition 6 — Add a lightweight `/health` endpoint if you don't have one
**Why:** System 3 (and whoever sets up deployment/demo environment) needs a fast way to confirm your service is up before wiring real calls into the frontend, especially during hackathon demo setup under time pressure.
**What to add:**
```
GET /health
  → { "status": "ok" }
```
Trivial, but missing this is a common last-minute demo-day scramble — add it now while you have time.

### Addition 7 — Do NOT add: enrollment/completion tracking, quiz logic, or auth
**Why this is worth stating explicitly:** It can be tempting to "future proof" by adding a `status: enrolled/completed` field, login checks, or a stub for quiz scores, since you now know the full picture. Don't. That state lives in System 3's database and System 2's quiz service respectively. If you build a shadow version of it inside System 1, you'll create two sources of truth that teammates will have to reconcile later — worse than not having it at all. Leave a code comment noting where such a field *could* be accepted as an input in the future (e.g., an optional `exclude_completed_course_ids` parameter on the recommendations endpoint), but don't implement the tracking itself.

---

## 3. Quick self-check before calling this "integration-ready"

Go through each — if any is "no," that addition still needs work:

- [ ] Does a changed `quiz_verified_skills` value for an employee get picked up on the next call, without restarting the service?
- [ ] Does a single per-employee call run fast enough to be called ~800 times in sequence without timing out?
- [ ] Does the new batch endpoint exist and return all employees in one call?
- [ ] Does every zero-course-coverage gap include a machine-readable `status` field, not just a message string?
- [ ] Do all JSON response keys match the dataset column names exactly (no camelCase drift, no renamed fields)?
- [ ] Does `/health` exist and return quickly?
- [ ] Have you confirmed you have NOT added enrollment, quiz, or auth logic of your own?

Once all seven are checked, System 1 is ready for teammates to build System 2 and System 3 against without needing you to make further changes on short notice.
