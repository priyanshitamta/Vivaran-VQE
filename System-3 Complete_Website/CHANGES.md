# Vivaran-VQE — Learner Platform Update

## 1. How the project worked before

| Part | What it is |
|---|---|
| **System 1** (`System-1 Recommandation_Engine`) | FastAPI on :8000. Loads Datasets 1–5 (skills, role requirements, employees, 784-course catalogue). Computes skill gaps (required − current level, priority-weighted) and ranks courses. Also has `/employees/{id}/skills` (quiz → verified skill hook) and `/analytics/*` (admin workforce stats). |
| **System 2** (`System-2 MCQ_Generator`, not in the upload) | Its `utils/` pipeline: yt-dlp download → FFmpeg audio → Whisper transcript → Groq LLM MCQs → validation / fact-check. It has no API. |
| **System 3** (`System-3 Complete_Website`) | Flask on :8080, SQLite `antahai.db` (`users`, `submissions`, `quiz_attempts`). One kind of user: log in, fill the System 1 intake form for an employee, get the top 5 courses, then "Take a quiz" feeds a **random** Dataset-6 YouTube link into System 2 in-process (`s2_bridge.py`) on a background thread. |

## 2. What changed (summary)

- **Three roles** (Learner / Trainer / Admin) with tabs on the login page, a separate dashboard for each role, and **backend** authorization (`@role_required`). The wrong role gets 403 even if they type the URL; JSON APIs return 401 or 403.
- **Learner registration**: username, **@gov.in email (validated in the browser and on the server)**, password, **designation** (reuses System 1's Dataset-2 designation and department list, stored as `role_id`), and **area of experience**. The learner is signed in and sent to the dashboard.
- **Secure passwords**: werkzeug salted hashes (scrypt or pbkdf2). Old accounts still log in, and their hash is upgraded automatically on first login.
- **Recommendations**: System 1 computes the role gaps. Every course is then scored on gap coverage, area of experience, and designation match, and completed courses are excluded. The number of courses is **dynamic** (it stops when the gaps are covered, between 3 and 6).
- **Clickable roadmap**: START → Course 1 → … → COMPLETED. Every step links to its course page and shows its status and progress.
- **Course page**: description, target designation, skills, embedded video(s), Start/Continue, progress bar, quiz section, attempt history, and completion status.
- **System 1 → System 2 integration**: the course's **own video URL** goes into the existing `s2_bridge.generate_quiz` pipeline. If one video fails, the next is tried. MCQs are **cached per (course, video)** so each video is processed only once.
- **Results**: total questions, correct, wrong, score, percentage, and Passed/Not passed. The pass mark is configurable and defaults to 60%.
- **Progress** is stored in the database: Start 10% → video watched 50% → quiz taken 75% → passed 100% (completed). A pass also sends the course's skills to System 1 as quiz-verified (`POST /employees/{id}/skills`).
- **Trainer dashboard**: learners and their progress, courses in use, and quiz results. The original "Assess an employee" intake flow is kept for trainers and admins.
- **Admin dashboard**: learners, trainers, courses, enrollments and completions, quiz results, pass rate, recommendations per learner, a form to create trainer and admin accounts, a catalogue reload button, and System 1 workforce analytics.
- Friendly error pages and messages; no stack traces reach the browser.

## 3. Course document (source of truth)

Put the document at **`data/courses.csv`**, **`data/courses.xlsx`** or **`data/courses.json`**, or set `VIVARAN_COURSES=path`. Then restart, or click **Reload course catalogue** on the admin dashboard. Column names are matched loosely. For example:

| Accepted headers (any of) | Used for |
|---|---|
| Course Name / Title / course_title | name |
| Description / Summary | description |
| Category / Domain | category |
| Designation / Target Role / Audience (`;`-separated, or `All`) | designation match |
| Skills / Skill Tags (S001… or skill names) | gap matching (inferred from the text if missing) |
| Video Links / Video / Link / URL (several per cell OK) | System 2 input |

**Until the document is added**, the platform uses System 1's Dataset-4 catalogue, and each course's video comes from Dataset-6. Each course keeps the same video every time, so its quiz can be cached. The UI labels these videos as placeholders.

## 4. Files

| File | Change |
|---|---|
| `app.py` | Roles and authorization, new register and login, learner dashboard, onboarding, course pages, quiz → progress hook, trainer and admin dashboards, JSON APIs, error handlers, staff seeding. The original routes are kept. |
| `db.py` | New tables and additive migrations (below), plus query helpers. |
| `courses.py` **(new)** | Course catalogue loader for the course document, with the Dataset-4 fallback. |
| `recommender.py` **(new)** | Gaps (via System 1) → scoring → dynamic roadmap. |
| `s1_client.py` | Added `compute`, `update_skills` and `analytics`, all existing System 1 endpoints. |
| `settings.py` | Pass mark, roadmap bounds, email domain, `S1_ROOT`. |
| `run_vivaran.py` | A missing System 2 is now a warning, not a hard stop. |
| `create_user.py` **(new)** | Create trainer and admin accounts from the command line. |
| `pregenerate_quizzes.py` **(new)** | Warm the MCQ cache before a demo. |
| `test_learner_flow.py` **(new)** | End-to-end test (49 checks). |
| `templates/` | New: `dashboard`, `course`, `courses`, `onboarding`, `trainer`, `admin`, `_learner_tables`, `forbidden`, `error`. Updated: `base` (role navigation), `login` (role tabs), `_register_modal`, `landing`, `quiz_results`, `quiz_status`. |
| `static/css/style.css` | New components appended; same palette. |
| `static/js/register.js` | New registration fields and @gov.in validation. |

System 1 and System 2 code: **unchanged**.

## 5. Database (SQLite, migrated automatically on start; nothing dropped)

- `users` gains `email` (unique) and `role` (`learner` | `trainer` | `admin`; existing rows become learners).
- `learner_profiles` (new): `user_id`, `role_id`, `designation`, `department`, `area_of_experience`, `s1_employee_id`.
- `enrollments` (new): `user_id`, `course_id`, `status`, `progress`, `video_watched`, `best_score`, `best_total`, `best_percentage`, `started_at`, `completed_at`.
- `quiz_bank` (new): cached MCQs per (`course_id`, `video_url`).
- `quiz_attempts` gains `passed` and `bank_id`. It already stored total, score and accuracy, which are reused as the quiz results.
- `submissions` is reused to store each learner's roadmap snapshot.

## 6. APIs

`POST /auth/register` · `POST /auth/login` (`/api/register` and `/login` still work) · `GET /api/learner/profile` · `GET /api/learner/recommendations` · `GET /api/learner/roadmap` · `GET /api/learner/progress` · `GET /api/learner/results` · `GET /api/courses` · `GET /api/courses/<id>` · `POST /api/courses/<id>/generate-quiz` · `GET /api/quiz/<id>` · `POST /api/quiz/<id>/submit` · `GET /quiz/<id>/status.json` · `GET /api/trainer/learners` · `GET /api/admin/overview`

No endpoint returns a password hash.

## 7. How the systems talk

```
register ──► System 1 POST /employees/register   (learner stored in Dataset-5 → admin analytics)
dashboard ─► System 1 POST /employees/compute    (role gaps, with quiz-verified skills)
          └► recommender.py scores catalogue → roadmap (stored in submissions)
course "Take the quiz" ─► quiz_bank hit? → instant
                       └► s2_bridge.generate_quiz(course video URL)   [System 2, background thread]
                            download → audio → Whisper → Groq MCQs → verify → quiz_attempts
submit ─► grade → pass? → enrollments = completed 100%
                        └► System 1 POST /employees/{id}/skills (quiz-verified levels)
```

If System 1 is down, the gaps are computed from its Dataset-2 CSV using the same formula, so the demo keeps working.

## 8. Testing

`VIVARAN_FAKE_QUIZ=1 python test_learner_flow.py` runs 49 checks, all passing. They cover registration and @gov.in validation, missing fields, duplicate email, hashing, the dashboard and roadmap, course start/watch/quiz using the course's own video, fail then retake then pass, progress, completed courses excluded from recommendations, logout/login persistence, invalid login, wrong-tab login, learner/trainer blocked from admin and other URLs, admin creating a trainer, and legacy accounts plus onboarding. The flow was also run against a stand-in for System 1's HTTP contract (register, compute, skills update, analytics), and video fallback and MCQ caching were checked with a stubbed System 2.

**Not tested here:** the real System 2 run (YouTube, Whisper and Groq). Its folder was not in the upload, and the sandbox has no YouTube access. The call into it is the same `s2_bridge.generate_quiz` call the old build used.

## 9. Run

```bash
# Windows, from the System-3 folder, with the System 2 venv (as before)
set VIVARAN_SECRET=any-long-random-string          # keeps sessions across restarts
set VIVARAN_ADMIN_USER=admin
set VIVARAN_ADMIN_PASSWORD=choose-a-password
set VIVARAN_TRAINER_USER=trainer
set VIVARAN_TRAINER_PASSWORD=choose-a-password
"..\System-2 MCQ_Generator\.venv\Scripts\python.exe" run_vivaran.py
# open http://127.0.0.1:8080
```

Optional:
- `python create_user.py --role trainer --username trainer2` creates another staff account.
- `python pregenerate_quizzes.py` caches the quizzes for everyone's roadmap before recording a demo.
- `set VIVARAN_PASS_THRESHOLD=70` changes the pass mark.
- `set VIVARAN_FAKE_QUIZ=1` serves canned questions offline (demo backup only).

---

## Update 2: Flashcards (learner)
- Decks are built from the learner's own graded quiz questions, with no new AI calls. A course's deck unlocks after its first quiz.
- Pages: dashboard "My Flashcards", a course page block, and `/courses/<id>/flashcards` (flip, previous/next, shuffle, Know it / Review again).
- DB: `flashcard_status`. APIs: `GET /api/flashcards/<course_id>`, `POST /api/flashcards/<course_id>/<card_key>`.

## Update 3: Trainer workspace
- **Registration:** the Register popup has a Learner/Trainer switch. Trainers pick one or more **specialisations** (System 1's 16 skills). Existing trainer accounts are asked for them at first login (`/trainer/onboarding`).
- **Assignment:** each learner has one trainer.
  - A new trainer takes up to 5 unassigned learners (`VIVARAN_TRAINER_BATCH`), matching ones first.
  - A new learner goes to the best-matching trainer with the fewest students, chosen at random among ties.
  - Trainers only see their own students, and this is enforced on the server.
  - Admin can reassign or unassign learners from the admin dashboard.
- **Collapsible sidebar** with Dashboard, Assigned courses, Uploaded courses, My profile and Assess an employee. It remembers whether it is open or closed.
- **Student page** (`/trainer/students/<id>`): profile, assigned courses with status, progress and best quiz score, **Remove**, **Add a course** (catalogue search), and quiz results.
  - Added courses show "📌 Added by your trainer" to the learner.
  - Removed courses never come back when the learner refreshes their recommendations.
  - Completed courses can't be removed.
- **Uploaded courses:** title, description, category, target designations, skills, level, duration and **video links**. They join the catalogue (IDs `TRN001`…) and quizzes come from the same MCQ generator.
- DB: `trainer_profiles`, `trainer_courses`, `course_overrides`, and `learner_profiles.trainer_id`, all created automatically.
- APIs: `GET /api/trainer/students`, `GET /api/trainer/students/<id>`, `POST /api/trainer/students/<id>/courses` with `{action: add|remove, course_id}`.
- Tests: `test_trainer_flow.py` (44 checks) and `test_learner_flow.py` (65 checks).


## Update 4: Orange theme, fuller trainer dashboard, Verify MCQs
- **Theme on every page:** iGOT orange (#E85F1C) is the main colour, backgrounds are warm, headings are deep brown, and blue is kept for small tags only. Text, tiles and page widths are larger.
- **Trainer dashboard:**
  - orange welcome banner, 5 headline tiles
  - student list with search
  - "Questions to review" call-out and top courses
  - side panels: Needs attention, Recent activity, My uploaded courses
- **Verify MCQs** (sidebar item with a pending-count badge):
  - A trainer reviews AI questions **only for courses they uploaded**.
  - "Generate questions now" creates them before any learner takes the quiz.
  - Each question can be approved, rejected or edited (question, options, correct answer, explanation), or reset to pending. There is also "Approve all" and "Regenerate".
  - Filters: Pending, Approved, Rejected, Flagged by AI, All.
- Learners are never blocked. Rejected questions are removed from all later quizzes and flashcards, and edits replace the original.
- `VIVARAN_FAKE_QUIZ=1` now saves its sample questions as *demo* sets. These are only visible in that mode, so they never mix with real ones.
- DB: `quiz_bank.demo` column (added automatically). Review state is stored with each question in `quiz_bank`.
- Tests: `test_trainer_flow.py` (61 checks) and `test_learner_flow.py` (65 checks).


## Update 5: Peer approval of uploaded courses
- A new upload is saved as **Pending approval** and stays **out of the catalogue**: learners can't see it, it can't be assigned, and it isn't recommended until it is approved.
- **Reviewers** are any other trainer who shares at least one skill with the course. They work from an open queue, and the first decision wins.
  - If no such trainer exists, the **admin** reviews it.
  - The admin can approve or reject any pending course.
  - Uploaders can never approve their own courses.
- **Trainer pages:**
  - **Course approvals** sidebar item (with badge) and a "Courses to approve" dashboard panel. Each course shows details, skills (★ marks ones the reviewer shares), designations and embedded videos, with **Approve & publish** or **Reject** (a reason is required).
  - The uploader's **Uploaded courses** page shows 🟡/🟢/🔴 status, the reviewer's reason, **Edit & resubmit**, and the review history.
  - MCQs can be generated and verified **before** approval. Staff can preview a pending course page; learners get 404.
- DB: `trainer_courses.status/reviewed_by/reviewed_at/review_note` and a `course_reviews` history table. Created automatically; earlier uploads count as approved.
- Tests: `test_trainer_flow.py` has 83 checks.


## Update 6: Admin workspace
The admin now has a collapsible sidebar like the trainer, with 10 sections:
1. **Overview:** banner, tiles, and CSS charts (registrations and quiz outcomes over the last 14 days, learners by department, pass-rate donut), plus Needs action and Recent activity panels.
2. **Learners:** search and filters (department, trainer, status). The detail page lets admin change the trainer, add or remove courses, see quiz results, deactivate the account or reset the password.
3. **Trainers:** workload bars, students' average progress, uploads, reviews done, **Auto-balance** and **Assign unassigned learners**. The detail page shows students, uploads and reviews.
   - Deactivating a trainer moves their students to other active trainers.
4. **Course approvals:** every pending course (filter "needs admin") plus the full approval history.
5. **Skill-gap analytics:** computed over System 1's Dataset-3 workforce (805 officials) or platform learners, using System 1's gap rule. It shows the most common gaps, a department × skill heatmap, and gaps with **no course** (e.g. Cloud Computing).
6. **Courses:** the full catalogue with recommended, enrolled, completed and pass-rate figures, filters, **Unpublish/Publish** (unpublished courses are never recommended or assignable), and Reload catalogue.
7. **Quiz quality:** pass rate per course, most-missed questions, AI-flagged questions, and verification coverage of uploads.
8. **Reports:** CSV downloads for learners, quiz results, enrollments, coverage by department, and trainers.
9. **Audit log:** every significant action (approvals, reassignments, deactivations, password resets, admin changes, unpublishing, downloads, MCQ reviews, …) with a filter.
10. **Staff accounts:**
    - **At most 3 admins.** Only the **main admin** (the first/env admin) can add an admin, by @gov.in email, and gets a one-time temporary password. They can also remove admins.
    - Deactivate/reactivate and reset password for any trainer or learner.
    - Temporary passwords must be changed at next login.

**Everyone:** a **Change password** page (in the avatar menu). The beta "passwords can't be changed" notice is removed. Deactivated accounts can't log in.
DB (added automatically): `users.active / must_change_password / is_primary`, `audit_log`, `course_flags`.
Tests: `test_admin_flow.py` (66 checks), plus the trainer (83) and learner (65) suites.


## Update 7: Learner workspace (same sidebar UI as Trainer/Admin)
- **Sidebar:** Dashboard · My roadmap · My courses · Recommended · Flashcards · Quiz results · My skills · Course catalogue · My profile. Badges show courses in progress, courses not started and cards to review.
- **Dashboard:**
  - orange banner with a progress ring and a "Continue / Start" button, plus 5 tiles
  - Continue learning, Up next on your roadmap, and latest quiz results
  - side panels: My trainer, Flashcards, My skills snapshot
- **New pages:** `/learn/roadmap`, `/learn/courses`, `/learn/recommended`, `/learn/flashcards`, `/learn/results`, `/learn/skills` (every skill the role needs, current vs required), `/learn/profile` (details, trainer, change password).
- The course, quiz, quiz-result, flashcard, catalogue and change-password pages open inside the learner sidebar for learners.
- Layout only: recommendations, quizzes and progress logic are unchanged.


## Update 8: Course catalogue
- **Filter by skill:** chips for all 16 skills, grouped by category, each showing how many courses teach it. Pick one or more skills to see courses that teach any of them.
- Also: free-text search (title, description, category, skill, ID) and a level filter, which all combine.
- **2 courses per row** (1 on narrow screens) with larger cards: description, skill chips, level, duration, mode, provider, video count, and a status badge (On your roadmap / In progress / Completed).
- **Scroll normally:** 20 courses load at a time and more load as you scroll (`/courses?page=N&partial=1`).


## Update 9: My progress (streaks, XP, badges)
- New learner sidebar page **My progress** (`/learn/progress`). It is personal only, with no leaderboard.
- **Streak:** a day counts when the learner **finishes a video lecture** or **takes a quiz** (in IST, `VIVARAN_TZ`). The streak stays alive until midnight. The page shows current and longest streak and a 12-week activity calendar.
- **XP and levels:** start +10, video +20, quiz +10 (max 3 per course), first pass +50, 100% +25, complete +100, flashcard mastered +5. There are 7 levels, Beginner → Champion.
- **9 badges:** First Step, First Pass, Perfect Score, 3/7/30-day streaks, 5 courses, Flashcard Master, Roadmap Champion. Each earned badge shows its date, and there is a recent-achievements timeline.
- Everything is derived from existing data, so past activity counts. New video completions are logged in a `learner_activity` table (created automatically).
- **Where it shows up:**
  - dashboard banner chips (🔥 streak, XP/level, badges)
  - trainer dashboard "Streak" column, plus streak/XP/badges on the student page
  - admin Learners list (streak, XP) and learner page (streak, longest, XP, badges)
- Tests: `test_progress.py` (26 checks).


## Update 10: Renamed to Vivaran-VQE
- The platform is now **Vivaran-VQE** (*Vivaran Validation, Questionnaire & Evaluation* Portal) everywhere: page titles, header, landing and login (with the full name), emails and messages, docs, code comments, CSV file names (`vivaran_*.csv`).
- **Settings** are now `VIVARAN_*` environment variables. Old `ANTAHAI_*` variables still work (`envcompat.py`).
- **Launcher** is `run_vivaran.py`. `run_antahai.py` is kept as a shortcut to it.
- **Database:** new installs use `vivaran.db`. An existing `antahai.db` is picked up automatically, so no data is lost.
