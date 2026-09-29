# Vivaran-VQE — Project Overview

**AI-enabled Skill Intelligence & Learning Platform for India's Official Statistical System**
Smart India Hackathon 2026 · Problem Statement **SIH26101** · Team EdTech

---

## 1. The problem and our answer

**Problem statement (SIH26101):** build an AI-enabled learning platform that
1. finds each official's **competency gaps**,
2. **recommends personalised training** through the iGOT Karmayogi ecosystem, and
3. **generates quizzes / MCQs from learning material**, to strengthen capacity building in the Official Statistical System.

**Vivaran-VQE** does all three in one web platform:

| Need | How Vivaran-VQE answers it |
|---|---|
| Know what each official is missing | Compares the skills a **designation requires** with the skills the official has **proven**, giving skill gaps |
| Recommend the right training | Builds a **personal learning roadmap** of courses that close those gaps, fitted to the official's field of work |
| Check that learning actually happened | Turns each course's **video lecture into an AI-generated, fact-checked quiz**. Passing it **verifies** the skill and updates the gaps |
| Keep people learning | Flashcards, streaks, XP, levels and badges |
| Let humans stay in control | **Trainers** guide students, upload courses and verify AI questions. **Admins** oversee everything with analytics, reports and an audit log |

This closes the loop: **gap → recommendation → learning → quiz → verified skill → smaller gap**.

---

## 2. Architecture at a glance

```
                        ┌────────────────────────────────────────────┐
   Browser  ───────────▶│  System 3 · Vivaran-VQE web app (Flask, :8080) │
 (Learner / Trainer /   │  auth · roles · dashboards · roadmap ·      │
  Admin)                │  courses · quizzes · progress · analytics   │
                        │  SQLite: vivaran.db                         │
                        └──────────┬───────────────────┬──────────────┘
                     HTTP (JSON)   │                   │  in-process Python import
                                   ▼                   ▼
          ┌──────────────────────────────┐   ┌─────────────────────────────────┐
          │ System 1 · Recommendation     │   │ System 2 · MCQ Generator         │
          │ Engine (FastAPI, :8000)       │   │ (utils/ pipeline, no server)     │
          │ skill gaps · course ranking · │   │ yt-dlp → FFmpeg → Whisper →      │
          │ quiz-verified skills ·        │   │ Groq LLM MCQs → validate →       │
          │ workforce analytics           │   │ fact-check                       │
          │ Datasets 1–5 (CSV)            │   └─────────────────────────────────┘
          └──────────────────────────────┘
```

| System | Folder | Tech | Role |
|---|---|---|---|
| **System 1** | `System-1 Recommandation_Engine/` | Python, FastAPI, pandas, Pydantic | The gap-analysis and recommendation engine, and the owner of the workforce datasets |
| **System 2** | `System-2 MCQ_Generator/` | Python, yt-dlp, FFmpeg, OpenAI Whisper, Groq (LLM), LangChain | Turns a video into multiple-choice questions and fact-checks them against the transcript |
| **System 3** | `System-3 Complete_Website/` | Python, Flask, Jinja2, SQLite, plain HTML/CSS/JS | The platform people actually use. It ties Systems 1 and 2 together |

**Design principle:** System 3 does **not** reimplement Systems 1 or 2. It calls System 1's existing HTTP API and imports System 2's existing pipeline functions. Neither of those systems' code was modified.

---

## 3. System 1 — Recommendation Engine

### Data it owns (CSV files in `datasets/`)
| Dataset | Contents |
|---|---|
| Dataset-1 Skill Taxonomy | 16 skills (S001–S016) in 4 categories: Technical, Statistical, Digital Governance, Behavioural-Managerial. Levels 1–3 |
| Dataset-2 Required Competency | ~60 roles (designation + department), each needing 12–15 skills at a **required level** with a **priority weight** (1–3) |
| Dataset-3 Employee Profiles | 805 synthetic officials, with self-rated and quiz-verified skill levels |
| Dataset-4 Course Catalogue | 784 iGOT-style courses tagged with the skills they teach and a target level |
| Dataset-5 Real Profiles | Grows at runtime: every learner who registers on Vivaran-VQE is added here |

### The core rule
```
current level = quiz-verified level if it exists, else self-rated level, else 0
gap          = required level − current level        (only gaps > 0 count)
```
Gaps are ranked by size, then by priority weight.

### Endpoints System 3 uses
| Endpoint | Used for |
|---|---|
| `GET /meta/roles`, `/meta/skills` | Designation list and skill names |
| `POST /employees/register` | Saves a new learner into Dataset-5 |
| `POST /employees/compute` | Computes a learner's gaps (with their verified skills) |
| `POST /employees/{id}/skills` | After a quiz pass, **raises the learner's quiz-verified skill levels** |
| `GET /analytics/combined` | Workforce analytics |

If System 1 is not running, System 3 applies **the same gap rule locally** to System 1's read-only CSVs, so the app keeps working in a demo.

---

## 4. System 2 — MCQ Generator (the AI pipeline)

```
Course video URL
   │  yt-dlp            download the video
   ▼
Video file
   │  FFmpeg            extract the audio track
   ▼
Audio
   │  Whisper           speech → transcript
   ▼
Transcript
   │  Groq LLM          write multiple-choice questions from the transcript only
   ▼
Draft MCQs
   │  validate          check structure: 4 options, one correct answer, …
   │  verify / fact-check  compare each question with the transcript → verified / flagged
   ▼
Final MCQs  (question, options, answer, explanation, source snippet, verification_status)
```

**How System 3 calls it** (`s2_bridge.py`):
- It imports System 2's `utils` directly, because System 2 has no API. It loads System 2's `.env` (for the Groq key) and runs the pipeline from System 2's own folder.
- It runs in a **background thread**, since generation takes minutes. The browser polls a status URL and opens the quiz when it's ready.
- A process-wide lock makes sure only one video is processed at a time.
- It tries each of a course's video links in turn if one fails, and reports errors in plain words ("video is private", "transcription failed", …).
- **Caching:** generated questions are saved per course video in `quiz_bank`, so each video is downloaded, transcribed and sent to the LLM **only once**. Later learners get the saved questions, reshuffled.
- **Offline demo mode:** `VIVARAN_FAKE_QUIZ=1` serves sample questions instead of calling the pipeline. They're saved as *demo* sets that are hidden in real runs.

---

## 5. System 3 — the Vivaran-VQE platform

### 5.1 Roles and access
| Role | How the account is created | Home |
|---|---|---|
| **Learner** (government official) | Self-registers: username, **@gov.in email** (validated), password, **designation**, **area of experience** | `/dashboard` |
| **Trainer** | Self-registers with one or more **specialisations** (from the 16 skills), or is created by an admin | `/trainer` |
| **Admin** | The **main admin** comes from environment variables at start-up. The main admin can add **at most 2 more** admins by @gov.in email (**3 in total**) | `/admin` |

- One login page with **Learner / Trainer / Admin** tabs.
- **Role-based access is enforced on the server** for every page and API. Typing another role's URL gives "Access denied" (403).
- Passwords are **hashed** (werkzeug scrypt/pbkdf2 with a random salt). Old-format hashes are upgraded automatically.
- **Change password** is available to everyone. Admin **password resets** give a one-time temporary password that must be changed at the next login.
- **Deactivated** accounts can't log in, and are signed out immediately if already logged in.
- Every page uses the same design: iGOT-orange theme, collapsible sidebar, and a mobile-friendly layout.

### 5.2 Learner experience
**Sidebar pages:** Dashboard · My roadmap · My courses · Recommended · Flashcards · Quiz results · My progress · My skills · Course catalogue · My profile

1. **Registration** creates the account and profile, registers the learner in System 1, **assigns a trainer**, and builds their first **roadmap**.
2. **Dashboard:** banner with a progress ring, streak, XP and a "Continue" button. It also shows tiles, continue learning, the next roadmap steps, latest results, the learner's trainer, flashcards and a skill snapshot.
3. **Roadmap:** START → Course 1 → … → COMPLETED. Every step is clickable and shows its status and progress.
4. **Course page:** description, target designation, skills, embedded video(s), **Start / Continue**, a progress bar, the quiz section, attempt history and flashcards.
   - Progress milestones: **start 10% → video watched 50% → quiz taken 75% → quiz passed 100% (completed)**.
5. **Quiz:** questions generated from **that course's video** by System 2.
   - The result shows total, correct, wrong, score, percentage and **Passed / Not passed**. The pass mark defaults to 60% (`VIVARAN_PASS_THRESHOLD`).
   - A per-question review shows the correct answers, explanations and the AI's verification status.
   - A pass **completes the course** and sends the course's skills to System 1 as **quiz-verified**, which shrinks the learner's gaps.
6. **Flashcards:** built from the learner's own quiz questions. A deck unlocks after the first quiz.
   - Flip cards, mark **Know it / Review again**.
   - Missed and "review" cards come first. Marks are saved.
7. **My progress:**
   - **Streak:** a day counts when the learner finishes a video lecture or takes a quiz.
   - Longest streak and a 12-week activity calendar.
   - **XP and 7 levels** (Beginner → Champion) and **9 badges**, with a recent-achievements timeline.
8. **My skills:** every skill the role needs, showing the verified level against the required level.
9. **Course catalogue:** search, level filter and **filter by skill** (multi-select chips with counts). Two courses per row, loading more as you scroll.

### 5.3 How recommendations are made (`recommender.py`)
1. **Gaps:** from System 1 (`/employees/compute`), using the learner's quiz-verified skills.
2. **Score every course** the learner hasn't completed:

   | Factor | Points |
   |---|---|
   | Gap coverage | Σ (gap size × priority weight) for each gap skill the course teaches |
   | Field fit | +2 per skill shared with the learner's **area of experience** |
   | Designation | +6 if the course targets the learner's designation |
   | Continuity | +4 if the learner already started it |
   | Tie-breakers | has a video, has a real description |

3. **Greedy path building:** pick the best course, mark its gap skills as covered, re-score the rest, and repeat. It stops when the gaps are covered, with **between 3 and 6 courses**. The number of courses depends on the learner.
4. **Order:** foundation level first, then the most critical gaps.
5. **Never recommended:** completed courses, courses a trainer removed, and admin-unpublished courses. Trainer-added courses are always included.

### 5.4 Trainer experience
**Sidebar pages:** Dashboard · Assigned courses · Uploaded courses · Course approvals · Verify MCQs · My profile · Assess an employee

- **Student assignment** (one trainer per learner):
  - A new trainer takes up to **5** unassigned learners, the best field matches first.
  - A new learner goes to the best-matching trainer with the **fewest** students.
- **Dashboard:** banner, 5 tiles, student list with search and 🔥 streaks, questions to review, top courses, needs attention (failed or not started), recent activity, and the trainer's uploads.
- **Student page:**
  - The student's courses with status, progress and best score, plus streak, XP and badges.
  - **Add a course** (catalogue search) or **Remove** one. Added courses show "📌 Added by your trainer". Removed ones never come back, and completed ones can't be removed.
- **Uploaded courses:** title, description, category, designations, skills, level, duration and **video links**.
  - Each upload goes into **peer review** first (see 5.6).
  - Rejected uploads can be **edited and resubmitted**.
- **Verify MCQs**, only for the trainer's own uploads:
  - **Generate questions now**, then **Approve / Edit / Reject / Approve all / Regenerate**.
  - Filters: Pending, Approved, Rejected, Flagged by AI.
  - Learners are never blocked. **Rejected questions disappear from later quizzes and flashcards, and edits replace the original.**
- **Assess an employee:** the original System 1 intake form, kept from the first build.

### 5.5 Admin experience
**Sidebar pages:** Overview · Learners · Trainers · Course approvals · Skill-gap analytics · Courses · Quiz quality · Reports · Audit log · Staff accounts

| Section | What it does |
|---|---|
| Overview | Charts (registrations and quiz outcomes over 14 days, learners by department, pass-rate ring), needs-action panel, recent activity |
| Learners | Search and filters. Learner page: change trainer, add/remove courses, quiz results, streak/XP/badges, deactivate, reset password |
| Trainers | Workload bars, **Auto-balance**, **Assign unassigned learners**. Deactivating a trainer moves their students automatically |
| Course approvals | Every pending upload, "needs admin" filter, **admin override**, full history |
| Skill-gap analytics | Over the **805-official workforce** (System 1 Dataset-3) or platform learners: most common gaps, a department × skill **heatmap**, and gaps with **no course** (e.g. Cloud Computing) |
| Courses | Catalogue with usage and pass rates, **Unpublish/Publish**, reload the catalogue |
| Quiz quality | Pass rate per course, **most-missed questions**, AI-flagged questions, verification coverage |
| Reports | CSV downloads: learners, quiz results, enrollments, coverage by department, trainers |
| Audit log | Who did what and when (approvals, reassignments, resets, deactivations, downloads, MCQ reviews, …) |
| Staff accounts | Admins (max 3, main admin only), trainers, learner accounts: activate/deactivate, reset password |

### 5.6 Peer approval of uploaded courses
```
Trainer uploads ─▶ PENDING (not in the catalogue; invisible to learners)
                     │
                     ├─ any OTHER trainer sharing ≥1 skill (open queue, first decision wins)
                     ├─ nobody shares the skills ─▶ ADMIN approves
                     └─ admin can always override
                     ▼
        APPROVED ─▶ live in the catalogue      REJECTED (reason required) ─▶ edit & resubmit
```
The uploader can generate and verify the course's MCQs **while it is still pending**, so it's quiz-ready the moment it's approved.

---

## 6. End-to-end flow (the complete loop)

```
Register (designation + field) ──▶ System 1: register + compute gaps
        │
        ▼
Roadmap (3–6 courses, ordered) ──▶ Course page ──▶ watch video (streak day)
        │                                              │
        │                                              ▼
        │                          Take quiz ──▶ System 2: video → audio → transcript
        │                                              → LLM MCQs → fact-check
        │                                              (cached; trainer-verified)
        │                                              ▼
        │                          Score ──▶ Passed? ──▶ course completed (100%)
        │                                              │
        │                                              ▼
        │                          System 1: quiz-verified skills raised ──▶ gaps shrink
        │                                              │
        └──────────── next roadmap / new recommendations ◀┘
                      + flashcards, XP, badges, streak
```

---

## 7. Data model (System 3 · `vivaran.db`, SQLite)

The database file is `vivaran.db`. An existing `antahai.db` from before the rename is picked up automatically, so no data is lost. All tables are created and migrated **automatically** at start-up. Changes only ever add things; nothing is dropped.

| Table | Purpose |
|---|---|
| `users` | Username, email, **password hash**, role, active, must_change_password, is_primary (main admin) |
| `learner_profiles` | Designation (System 1 role), department, area of experience, System 1 employee ID, **trainer_id** |
| `trainer_profiles` | Specialisations (skill IDs) |
| `submissions` | Each learner's **roadmap snapshot**, plus the original "assess an employee" records |
| `enrollments` | Per learner and course: status, progress %, video watched, best score, started/completed dates |
| `quiz_attempts` | Each quiz: source video, generated questions, answers, score, pass/fail |
| `quiz_bank` | **Cached MCQs** per (course, video), with each question's trainer review (approved/rejected/edited) |
| `flashcard_status` | Know it / Review again per card |
| `trainer_courses` | Uploaded courses and their approval status/reviewer/note |
| `course_reviews` | Approval history (submitted / approved / rejected / resubmitted) |
| `course_overrides` | Trainer/admin add/remove on a learner's courses |
| `course_flags` | Admin-unpublished courses |
| `learner_activity` | Learning events (video completed) used for streaks |
| `audit_log` | Every significant action |

**Course catalogue** (`courses.py`): loads a **course document** if one is present (`data/courses.csv/.xlsx/.json`, with loosely matched column names), otherwise System 1's Dataset-4 with Dataset-6 videos. It then adds **approved** trainer uploads. Each course has an ID, title, description, category, target designations, skills, video links, level, duration, mode, provider and language.

---

## 8. Main APIs (JSON)

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `POST /auth/login` |
| Learner | `GET /api/learner/profile`, `/api/learner/recommendations`, `/api/learner/roadmap`, `/api/learner/progress`, `/api/learner/results` |
| Courses & quizzes | `GET /api/courses`, `GET /api/courses/<id>`, `POST /api/courses/<id>/generate-quiz`, `GET /api/quiz/<id>`, `POST /api/quiz/<id>/submit`, `GET /quiz/<id>/status.json` |
| Flashcards | `GET /api/flashcards/<course_id>`, `POST /api/flashcards/<course_id>/<card>` |
| Trainer | `GET /api/trainer/students`, `GET /api/trainer/students/<id>`, `POST /api/trainer/students/<id>/courses` |
| Admin | `GET /api/admin/overview`, CSV reports at `/admin/reports/<name>.csv` |

No API ever returns a password or password hash.

---

## 9. Security and reliability
- Hashed passwords; server-side role checks on every route; a trainer only sees **their own** students.
- @gov.in email validation; input validation on every form.
- Secrets (Groq API key, session secret, admin password) live in **environment variables / `.env`**, never in code.
- Temporary passwords must be changed; deactivation takes effect immediately; there is a full audit log.
- Friendly error pages; no stack traces shown to users.
- Fallbacks: System 1 down → local gap calculation. A video fails → the next video is tried. AI unavailable → a clear message, or demo mode.
- MCQ caching avoids repeated downloads, transcription and LLM calls.

---

## 10. Project files (System 3)

| File | What it does |
|---|---|
| `app.py` | All routes: auth, learner, trainer, admin, quizzes, APIs |
| `db.py` | SQLite schema, migrations and queries |
| `courses.py` | Course catalogue loader (document / Dataset-4 / trainer uploads) |
| `recommender.py` | Gap retrieval, course scoring, roadmap building, trainer matching |
| `progress.py` | Streaks, XP, levels, badges, activity calendar |
| `s1_client.py` | HTTP client for System 1 |
| `s2_bridge.py` | In-process bridge to System 2's MCQ pipeline |
| `settings.py` | Paths, ports, pass mark, roadmap size, email domain |
| `run_vivaran.py` | One-command launcher (starts System 1, then System 3) |
| `create_user.py` | Create admin/trainer accounts from the terminal |
| `pregenerate_quizzes.py` | Warm the MCQ cache before a demo |
| `templates/` | 57 Jinja pages (learner, trainer, admin, shared) |
| `static/css/style.css` | The whole design system (orange theme, sidebar, charts, cards) |
| `test_*.py` | Automated end-to-end tests |
| `CHANGES.md` | Change log for every update |

---

## 11. Testing

| Suite | Checks | Covers |
|---|---|---|
| `test_learner_flow.py` | 79 | Registration and @gov.in validation, roadmap, catalogue filters, course → quiz → pass/fail → progress, flashcards, logout/login persistence, role blocking |
| `test_trainer_flow.py` | 83 | Trainer registration and assignment, add/remove courses, uploads, peer approval, MCQ verification, admin reassign |
| `test_admin_flow.py` | 66 | All admin sections, auto-balance, deactivation, password reset/change, admin limit, unpublish, reports, audit |
| `test_progress.py` | 26 | Streak maths, XP, levels, badges, dashboards showing progress |

Run any suite with: `VIVARAN_FAKE_QUIZ=1 python test_learner_flow.py`

The **real** System 2 run (YouTube, Whisper, Groq) happens on the team laptop. The tests use demo mode or a stand-in for the pipeline.

---

## 12. How to run it (macOS)

```bash
cd ~/Downloads/SIH/Antah-Ai-Platform-main/"System-3 Complete_Website"
source ~/Downloads/Antah-Ai-Platform-main/venv/bin/activate
export VIVARAN_SECRET="vivaran-sih-2026"
export VIVARAN_ADMIN_USER="admin"
export VIVARAN_ADMIN_PASSWORD="Admin@2026"
export VIVARAN_S1_PYTHON="$(which python)"
python run_vivaran.py
# open http://127.0.0.1:8080
```

| Setting (env var) | Default | Meaning |
|---|---|---|
| `VIVARAN_PASS_THRESHOLD` | 60 | Quiz pass mark (%) |
| `VIVARAN_FAKE_QUIZ` | off | `1` = sample questions (offline demo) |
| `VIVARAN_TRAINER_BATCH` | 5 | Learners a new trainer takes |
| `VIVARAN_ROADMAP_MIN` / `_MAX` | 3 / 6 | Roadmap size bounds |
| `VIVARAN_COURSES` | — | Path to the course document |
| `VIVARAN_TZ` | Asia/Kolkata | Time zone for streak days |

---

## 13. Known limits and next steps
- **Course document:** until the official course list (with its video links) is added, courses come from System 1's Dataset-4, with placeholder videos from Dataset-6.
- **Quiz generation speed:** the first quiz for a video takes a few minutes. Use `pregenerate_quizzes.py` before demos.
- **Uploads are video links only.** File uploads would need changes to System 2.
- **SQLite** is right for a prototype. A multi-user deployment would move to PostgreSQL and a production web server.
- Possible next steps: direct iGOT Karmayogi API integration, notifications/reminders for streaks, Hindi and regional-language quizzes, certificate generation.
