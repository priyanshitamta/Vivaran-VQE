"""
Vivaran-VQE (System 3) - SQLite persistence layer.

Three tables back the whole web app:
  users          - auth only (username + password hash). System 1/2 know nothing
                   about usernames, so this is the only account data S3 tracks.
  submissions    - one row per employee intake form the logged-in user submits
                   through System 1, storing a snapshot of what was submitted
                   plus the exact recommendations System 1 returned (the profile
                   page renders history from these snapshots).
  quiz_attempts  - one row per "Take a quiz" run: which course of which
                   submission, the random Dataset-6 source link, generation
                   status, the question bank once ready, and the grade once the
                   quiz is submitted.

No ORM - plain sqlite3. The DB file lives next to this package.
"""

from __future__ import annotations

import json
import os
import sqlite3

import envcompat  # noqa: F401  (maps old ANTAHAI_* env vars to VIVARAN_*)
from contextlib import closing
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
def _default_db_path() -> str:
    """vivaran.db, but keep using an existing antahai.db (data from before the rename)."""
    new = os.path.join(BASE_DIR, "vivaran.db")
    old = os.path.join(BASE_DIR, "antahai.db")
    return old if (not os.path.exists(new) and os.path.exists(old)) else new


DB_PATH = os.environ.get("VIVARAN_DB", _default_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                employee_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role_id TEXT NOT NULL,
                designation TEXT NOT NULL,
                department TEXT NOT NULL,
                current_assignment TEXT,
                educational_qualifications TEXT,
                work_experience_years INTEGER,
                previous_trainings TEXT NOT NULL DEFAULT '[]',
                self_rated_skills TEXT NOT NULL DEFAULT '{}',
                quiz_verified_skills TEXT NOT NULL DEFAULT '{}',
                recommendations TEXT NOT NULL DEFAULT '[]',
                submitted_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quiz_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                course_id TEXT NOT NULL,
                course_title TEXT NOT NULL,
                source_link TEXT,
                status TEXT NOT NULL DEFAULT 'generating',
                error TEXT,
                quiz TEXT,
                answers TEXT,
                score INTEGER,
                total INTEGER,
                accuracy REAL,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );

            -- Learner platform (extends the tables above; nothing is dropped).
            CREATE TABLE IF NOT EXISTS learner_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                role_id TEXT NOT NULL,
                designation TEXT NOT NULL,
                department TEXT NOT NULL,
                area_of_experience TEXT NOT NULL,
                s1_employee_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS enrollments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                course_id TEXT NOT NULL,
                course_title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                progress INTEGER NOT NULL DEFAULT 0,
                video_watched INTEGER NOT NULL DEFAULT 0,
                best_score INTEGER,
                best_total INTEGER,
                best_percentage REAL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, course_id)
            );

            -- Trainers: specialisations = JSON list of System-1 skill ids.
            CREATE TABLE IF NOT EXISTS trainer_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                specialisations TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Courses uploaded by trainers (merged into the catalogue).
            CREATE TABLE IF NOT EXISTS trainer_courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trainer_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                designations TEXT NOT NULL DEFAULT '[]',
                skills TEXT NOT NULL DEFAULT '[]',
                videos TEXT NOT NULL DEFAULT '[]',
                target_level INTEGER NOT NULL DEFAULT 1,
                duration_minutes INTEGER,
                created_at TEXT NOT NULL
            );

            -- Learner learning events (streaks / XP): video watched, quiz taken, ...
            CREATE TABLE IF NOT EXISTS learner_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL,            -- video | quiz
                course_id TEXT,
                created_at TEXT NOT NULL
            );

            -- Who did what (admin actions, approvals, reassignment, ...).
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            );

            -- Admin-unpublished courses (kept, but never recommended/assignable).
            CREATE TABLE IF NOT EXISTS course_flags (
                course_id TEXT PRIMARY KEY,
                unpublished INTEGER NOT NULL DEFAULT 0,
                by_user_id INTEGER REFERENCES users(id),
                updated_at TEXT NOT NULL
            );

            -- Peer-review history of trainer-uploaded courses.
            CREATE TABLE IF NOT EXISTS course_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_row_id INTEGER NOT NULL REFERENCES trainer_courses(id),
                action TEXT NOT NULL,          -- submitted | approved | rejected | resubmitted
                by_user_id INTEGER REFERENCES users(id),
                note TEXT,
                created_at TEXT NOT NULL
            );

            -- Trainer edits to a learner's recommended courses.
            -- action = 'add' (pinned into the roadmap) | 'remove' (never recommended again)
            CREATE TABLE IF NOT EXISTS course_overrides (
                user_id INTEGER NOT NULL REFERENCES users(id),
                course_id TEXT NOT NULL,
                action TEXT NOT NULL,
                trainer_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, course_id)
            );

            -- Flashcard self-assessment per learner card ("know" / "review").
            CREATE TABLE IF NOT EXISTS flashcard_status (
                user_id INTEGER NOT NULL REFERENCES users(id),
                course_id TEXT NOT NULL,
                card_key TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, course_id, card_key)
            );

            -- Generated MCQs cached per (course, video) so a course's quiz is
            -- only generated once (no repeated downloads / Whisper / LLM calls).
            CREATE TABLE IF NOT EXISTS quiz_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT NOT NULL,
                video_url TEXT NOT NULL,
                questions TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (course_id, video_url)
            );
            """
        )
        _migrate(conn)
        conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent column migrations for DBs created by older builds."""
    users = _columns(conn, "users")
    if "email" not in users:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "role" not in users:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'learner'")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users(email) WHERE email IS NOT NULL"
    )
    users = _columns(conn, "users")
    for col, ddl in (("active", "INTEGER NOT NULL DEFAULT 1"),
                     ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
                     ("is_primary", "INTEGER NOT NULL DEFAULT 0")):
        if col not in users:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    # Exactly one primary admin: the earliest admin, if none is marked yet.
    if not conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND is_primary = 1").fetchone():
        conn.execute("UPDATE users SET is_primary = 1 WHERE id = "
                     "(SELECT MIN(id) FROM users WHERE role = 'admin')")
    tc = _columns(conn, "trainer_courses")
    if "status" not in tc:
        # Courses uploaded before peer review existed stay live (approved).
        conn.execute("ALTER TABLE trainer_courses ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
    for col in ("reviewed_by", "reviewed_at", "review_note"):
        if col not in tc:
            conn.execute(f"ALTER TABLE trainer_courses ADD COLUMN {col} {'INTEGER' if col == 'reviewed_by' else 'TEXT'}")
    bank = _columns(conn, "quiz_bank")
    if "demo" not in bank:
        conn.execute("ALTER TABLE quiz_bank ADD COLUMN demo INTEGER NOT NULL DEFAULT 0")
    profiles = _columns(conn, "learner_profiles")
    if "trainer_id" not in profiles:
        conn.execute("ALTER TABLE learner_profiles ADD COLUMN trainer_id INTEGER REFERENCES users(id)")
    attempts = _columns(conn, "quiz_attempts")
    if "passed" not in attempts:
        conn.execute("ALTER TABLE quiz_attempts ADD COLUMN passed INTEGER")
    if "bank_id" not in attempts:
        conn.execute("ALTER TABLE quiz_attempts ADD COLUMN bank_id INTEGER")


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def create_user(username: str, password_hash: str, email: str | None = None,
                role: str = "learner") -> int:
    with closing(connect()) as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, email, role, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, password_hash, email, role, _now()),
        )
        conn.commit()
        return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email,)
        ).fetchone()
    return dict(row) if row else None


def update_password_hash(user_id: int, password_hash: str) -> None:
    """Used to transparently upgrade legacy hashes on successful login."""
    with closing(connect()) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        conn.commit()


def set_user_email(user_id: int, email: str) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
        conn.commit()


def list_users(role: str | None = None) -> list[dict]:
    """Users WITHOUT password hashes (safe to hand to templates / JSON)."""
    sql = "SELECT id, username, email, role, created_at, active, is_primary, must_change_password FROM users"
    params: tuple = ()
    if role:
        sql += " WHERE role = ?"
        params = (role,)
    with closing(connect()) as conn:
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]


def get_user_by_id(user_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# submissions
# ---------------------------------------------------------------------------


def create_submission(user_id: int, data: dict) -> int:
    """Persist one intake submission + the recommendations S1 returned."""
    with closing(connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions (
                user_id, employee_id, name, role_id, designation, department,
                current_assignment, educational_qualifications, work_experience_years,
                previous_trainings, self_rated_skills, quiz_verified_skills,
                recommendations, submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                data["employee_id"],
                data.get("name", ""),
                data.get("role_id", ""),
                data.get("designation", ""),
                data.get("department", ""),
                data.get("current_assignment"),
                data.get("educational_qualifications"),
                data.get("work_experience_years"),
                json.dumps(data.get("previous_trainings", [])),
                json.dumps(data.get("self_rated_skills", {})),
                json.dumps(data.get("quiz_verified_skills", {})),
                json.dumps(data.get("recommendations", [])),
                _now(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def _submission_row(row: sqlite3.Row) -> dict:
    s = dict(row)
    for col in ("previous_trainings", "self_rated_skills",
                "quiz_verified_skills", "recommendations"):
        try:
            s[col] = json.loads(s[col] or "null") or ([] if col.endswith("s") or col == "recommendations" else {})
        except json.JSONDecodeError:
            s[col] = {} if "skills" in col else []
    return s


def get_submission(submission_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
    return _submission_row(row) if row else None


def list_submissions(user_id: int) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE user_id = ? ORDER BY submitted_at DESC",
            (user_id,),
        ).fetchall()
    return [_submission_row(r) for r in rows]


# ---------------------------------------------------------------------------
# quiz_attempts
# ---------------------------------------------------------------------------


def create_attempt(user_id: int, submission_id: int, course_id: str,
                   course_title: str, source_link: str | None = None) -> int:
    with closing(connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO quiz_attempts (
                user_id, submission_id, course_id, course_title, source_link,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'generating', ?)
            """,
            (user_id, submission_id, course_id, course_title, source_link, _now()),
        )
        conn.commit()
        return cur.lastrowid


def set_attempt_ready(attempt_id: int, quiz: list[dict]) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE quiz_attempts SET status = 'ready', quiz = ?, error = NULL WHERE id = ?",
            (json.dumps(quiz), attempt_id),
        )
        conn.commit()


def set_attempt_error(attempt_id: int, message: str) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE quiz_attempts SET status = 'error', error = ?, finished_at = ? "
            "WHERE id = ?",
            (message, _now(), attempt_id),
        )
        conn.commit()


def get_attempt(attempt_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
    return dict(row) if row else None


def grade_attempt(attempt_id: int, answers: dict, quiz: list[dict]) -> dict:
    """Score a finished quiz (S2 convention: case-insensitive full-string match)."""
    details, score, total, accuracy = _score_answers(answers, quiz)

    with closing(connect()) as conn:
        conn.execute(
            """
            UPDATE quiz_attempts
            SET status = 'graded', answers = ?, score = ?, total = ?, accuracy = ?,
                quiz = ?, finished_at = ?
            WHERE id = ?
            """,
            (json.dumps(answers), score, total, accuracy, json.dumps(quiz),
             _now(), attempt_id),
        )
        conn.commit()

    return {"score": score, "total": total, "accuracy": accuracy, "details": details}


def _score_answers(answers: dict, quiz: list[dict]) -> tuple[list[dict], int, int, float]:
    """Shared scorer used by grade_attempt() and graded_review()."""
    details = []
    score = 0
    for q in quiz:
        qid = str(q["id"])
        selected = answers.get(qid, "")
        correct = q.get("answer", "")
        is_correct = bool(selected) and str(selected).lower() == str(correct).lower()
        if is_correct:
            score += 1
        details.append({
            "question_id": q["id"],
            "question": q.get("question", ""),
            "options": q.get("options", []),
            "selected_answer": selected,
            "correct_answer": correct,
            "is_correct": is_correct,
            "verification_status": q.get("verification_status", "unverified"),
            "explanation": q.get("explanation", ""),
        })

    total = len(quiz)
    accuracy = round((score / total) * 100, 2) if total else 0.0
    return details, score, total, accuracy


def graded_review(attempt_id: int) -> dict | None:
    """Recompute a graded attempt's per-question review from stored columns."""
    attempt = get_attempt(attempt_id)
    if not attempt or attempt.get("status") != "graded":
        return None
    try:
        quiz = json.loads(attempt["quiz"]) if attempt.get("quiz") else []
        answers = json.loads(attempt["answers"]) if attempt.get("answers") else {}
    except json.JSONDecodeError:
        return None
    details, score, total, accuracy = _score_answers(answers, quiz)
    return {"score": score, "total": total, "accuracy": accuracy, "details": details}


def list_attempts_for_submission(submission_id: int) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM quiz_attempts WHERE submission_id = ? ORDER BY id",
            (submission_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_attempts_for_user(user_id: int) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM quiz_attempts WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_attempts() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT a.*, u.username FROM quiz_attempts a
            JOIN users u ON u.id = a.user_id ORDER BY a.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def set_attempt_passed(attempt_id: int, passed: bool) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE quiz_attempts SET passed = ? WHERE id = ?", (1 if passed else 0, attempt_id))
        conn.commit()


def set_attempt_bank(attempt_id: int, bank_id: int) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE quiz_attempts SET bank_id = ? WHERE id = ?", (bank_id, attempt_id))
        conn.commit()


def list_submissions_all() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM submissions ORDER BY submitted_at DESC").fetchall()
    return [_submission_row(r) for r in rows]


def latest_submission(user_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return _submission_row(row) if row else None


# ---------------------------------------------------------------------------
# learner_profiles
# ---------------------------------------------------------------------------


def upsert_learner_profile(user_id: int, role_id: str, designation: str, department: str,
                           area_of_experience: str, s1_employee_id: str | None = None) -> None:
    now = _now()
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO learner_profiles (user_id, role_id, designation, department,
                area_of_experience, s1_employee_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                role_id = excluded.role_id, designation = excluded.designation,
                department = excluded.department,
                area_of_experience = excluded.area_of_experience,
                s1_employee_id = COALESCE(excluded.s1_employee_id, learner_profiles.s1_employee_id),
                updated_at = excluded.updated_at
            """,
            (user_id, role_id, designation, department, area_of_experience,
             s1_employee_id, now, now),
        )
        conn.commit()


def get_learner_profile(user_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM learner_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def set_learner_s1_id(user_id: int, s1_employee_id: str) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE learner_profiles SET s1_employee_id = ? WHERE user_id = ?",
                     (s1_employee_id, user_id))
        conn.commit()


def list_learners_with_profiles() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.email, u.created_at, u.active, p.designation, p.department,
                   p.area_of_experience, p.role_id, p.s1_employee_id, p.trainer_id,
                   t.username AS trainer_username
            FROM users u LEFT JOIN learner_profiles p ON p.user_id = u.id
            LEFT JOIN users t ON t.id = p.trainer_id
            WHERE u.role = 'learner' ORDER BY u.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# enrollments (course progress)
# ---------------------------------------------------------------------------


def get_enrollment(user_id: int, course_id: str) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM enrollments WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        ).fetchone()
    return dict(row) if row else None


def list_enrollments(user_id: int | None = None) -> list[dict]:
    with closing(connect()) as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT e.*, u.username FROM enrollments e JOIN users u ON u.id = e.user_id "
                "ORDER BY e.updated_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM enrollments WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def start_enrollment(user_id: int, course_id: str, course_title: str, progress: int) -> dict:
    """Create (or return) the enrollment row; progress never goes backwards."""
    now = _now()
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO enrollments (user_id, course_id, course_title, status, progress,
                                     started_at, updated_at)
            VALUES (?, ?, ?, 'in_progress', ?, ?, ?)
            ON CONFLICT(user_id, course_id) DO NOTHING
            """,
            (user_id, course_id, course_title, progress, now, now),
        )
        conn.commit()
    return get_enrollment(user_id, course_id)


def bump_progress(user_id: int, course_id: str, progress: int, **fields) -> None:
    """Raise progress to at least ``progress`` and set any extra columns given."""
    allowed = {"video_watched", "status", "completed_at", "best_score", "best_total",
               "best_percentage"}
    sets = ["progress = MAX(progress, ?)", "updated_at = ?"]
    params: list = [progress, _now()]
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(key)
        sets.append(f"{key} = ?")
        params.append(value)
    params += [user_id, course_id]
    with closing(connect()) as conn:
        conn.execute(
            f"UPDATE enrollments SET {', '.join(sets)} WHERE user_id = ? AND course_id = ?",
            params,
        )
        conn.commit()


# ---------------------------------------------------------------------------
# quiz_bank (MCQ cache per course video)
# ---------------------------------------------------------------------------


def _demo_mode() -> bool:
    return os.environ.get("VIVARAN_FAKE_QUIZ") == "1"


def get_bank(course_id: str, video_url: str) -> dict | None:
    """Cached MCQs for a course video. Demo (canned) sets are only visible in
    VIVARAN_FAKE_QUIZ=1 mode, so they never leak into a real run."""
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM quiz_bank WHERE course_id = ? AND video_url = ?"
            + ("" if _demo_mode() else " AND demo = 0"),
            (course_id, video_url),
        ).fetchone()
    return dict(row) if row else None


def get_bank_by_id(bank_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM quiz_bank WHERE id = ?", (bank_id,)).fetchone()
    if not row or (row["demo"] and not _demo_mode()):
        return None
    return dict(row)


def list_banks_for_course(course_id: str) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM quiz_bank WHERE course_id = ?" + ("" if _demo_mode() else " AND demo = 0")
            + " ORDER BY id", (course_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_bank(course_id: str, video_url: str, questions: list[dict], demo: bool = False) -> int:
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO quiz_bank (course_id, video_url, questions, created_at, demo)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(course_id, video_url) DO UPDATE SET
                questions = excluded.questions, created_at = excluded.created_at,
                demo = excluded.demo
            """,
            (course_id, video_url, json.dumps(questions), _now(), 1 if demo else 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM quiz_bank WHERE course_id = ? AND video_url = ?",
            (course_id, video_url),
        ).fetchone()
    return row[0]


def update_bank_questions(bank_id: int, questions: list[dict]) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE quiz_bank SET questions = ? WHERE id = ?", (json.dumps(questions), bank_id))
        conn.commit()


def list_bank() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, course_id, video_url, created_at FROM quiz_bank"
            + ("" if _demo_mode() else " WHERE demo = 0") + " ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def list_attempts_for_user_course(user_id: int, course_id: str) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM quiz_attempts WHERE user_id = ? AND course_id = ? ORDER BY id",
            (user_id, course_id),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# flashcard_status
# ---------------------------------------------------------------------------


def get_flashcard_status(user_id: int, course_id: str | None = None) -> dict:
    """{(course_id, card_key): status} for a learner (optionally one course)."""
    sql = "SELECT course_id, card_key, status FROM flashcard_status WHERE user_id = ?"
    params: list = [user_id]
    if course_id:
        sql += " AND course_id = ?"
        params.append(course_id)
    with closing(connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {(r["course_id"], r["card_key"]): r["status"] for r in rows}


def set_flashcard_status(user_id: int, course_id: str, card_key: str, status: str) -> None:
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO flashcard_status (user_id, course_id, card_key, status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, course_id, card_key) DO UPDATE SET
                status = excluded.status, updated_at = excluded.updated_at
            """,
            (user_id, course_id, card_key, status, _now()),
        )
        conn.commit()


def update_submission_recommendations(submission_id: int, recommendations: list[dict]) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE submissions SET recommendations = ? WHERE id = ?",
                     (json.dumps(recommendations), submission_id))
        conn.commit()


# ---------------------------------------------------------------------------
# trainers: profiles, learner assignment
# ---------------------------------------------------------------------------


def upsert_trainer_profile(user_id: int, specialisations: list[str]) -> None:
    now = _now()
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO trainer_profiles (user_id, specialisations, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                specialisations = excluded.specialisations, updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(specialisations), now, now),
        )
        conn.commit()


def get_trainer_profile(user_id: int) -> dict | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM trainer_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["specialisations"] = json.loads(out["specialisations"] or "[]")
    return out


def list_trainers_with_profiles() -> list[dict]:
    """Trainers + specialisations + current student count (no password hashes)."""
    with closing(connect()) as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.email, u.created_at, u.active, tp.specialisations,
                   (SELECT COUNT(*) FROM learner_profiles lp WHERE lp.trainer_id = u.id) AS students
            FROM users u LEFT JOIN trainer_profiles tp ON tp.user_id = u.id
            WHERE u.role = 'trainer' ORDER BY u.id
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["specialisations"] = json.loads(d["specialisations"] or "[]") if d["specialisations"] else []
        d["has_profile"] = r["specialisations"] is not None
        out.append(d)
    return out


def set_learner_trainer(learner_id: int, trainer_id: int | None) -> None:
    with closing(connect()) as conn:
        conn.execute("UPDATE learner_profiles SET trainer_id = ? WHERE user_id = ?",
                     (trainer_id, learner_id))
        conn.commit()


def list_trainer_students(trainer_id: int) -> list[dict]:
    return [l for l in list_learners_with_profiles() if l.get("trainer_id") == trainer_id]


def list_unassigned_learners() -> list[dict]:
    return [l for l in list_learners_with_profiles()
            if l.get("role_id") and not l.get("trainer_id")]


# ---------------------------------------------------------------------------
# trainer-uploaded courses
# ---------------------------------------------------------------------------


def create_trainer_course(trainer_id: int, data: dict) -> int:
    with closing(connect()) as conn:
        cur = conn.execute(
            """
            INSERT INTO trainer_courses (trainer_id, title, description, category, designations,
                skills, videos, target_level, duration_minutes, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (trainer_id, data["title"], data.get("description", ""), data.get("category", ""),
             json.dumps(data.get("designations", [])), json.dumps(data.get("skills", [])),
             json.dumps(data.get("videos", [])), int(data.get("target_level") or 1),
             data.get("duration_minutes"), _now()),
        )
        row_id = cur.lastrowid
        conn.execute(
            "INSERT INTO course_reviews (course_row_id, action, by_user_id, note, created_at) "
            "VALUES (?, 'submitted', ?, NULL, ?)", (row_id, trainer_id, _now()))
        conn.commit()
        return row_id


def list_trainer_courses(trainer_id: int | None = None, status: str | None = None) -> list[dict]:
    """Uploaded courses (any status unless ``status`` given), newest first."""
    sql = ("SELECT c.*, u.username AS trainer_username, r.username AS reviewer_username "
           "FROM trainer_courses c JOIN users u ON u.id = c.trainer_id "
           "LEFT JOIN users r ON r.id = c.reviewed_by")
    where, params = [], []
    if trainer_id is not None:
        where.append("c.trainer_id = ?")
        params.append(trainer_id)
    if status is not None:
        where.append("c.status = ?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        with closing(connect()) as conn:
            rows = conn.execute(sql + " ORDER BY c.id DESC", tuple(params)).fetchall()
    except sqlite3.OperationalError:  # table not created yet (init_db not run)
        return []
    out = []
    for r in rows:
        d = dict(r)
        for col in ("designations", "skills", "videos"):
            d[col] = json.loads(d[col] or "[]")
        d["course_id"] = f"TRN{d['id']:03d}"
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# course_overrides (trainer add/remove on a learner's recommendations)
# ---------------------------------------------------------------------------


def set_course_override(user_id: int, course_id: str, action: str, trainer_id: int | None) -> None:
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO course_overrides (user_id, course_id, action, trainer_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, course_id) DO UPDATE SET
                action = excluded.action, trainer_id = excluded.trainer_id,
                created_at = excluded.created_at
            """,
            (user_id, course_id, action, trainer_id, _now()),
        )
        conn.commit()


def get_course_overrides(user_id: int) -> dict[str, str]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT course_id, action FROM course_overrides WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return {r["course_id"]: r["action"] for r in rows}



def get_trainer_course(row_id: int) -> dict | None:
    return next((c for c in list_trainer_courses() if c["id"] == row_id), None)


def review_trainer_course(row_id: int, action: str, by_user_id: int, note: str | None) -> None:
    """approve | reject (reviewer/admin) or resubmit (uploader)."""
    status = {"approved": "approved", "rejected": "rejected", "resubmitted": "pending"}[action]
    with closing(connect()) as conn:
        if action == "resubmitted":
            conn.execute("UPDATE trainer_courses SET status = 'pending', reviewed_by = NULL, "
                         "reviewed_at = NULL, review_note = NULL WHERE id = ?", (row_id,))
        else:
            conn.execute("UPDATE trainer_courses SET status = ?, reviewed_by = ?, reviewed_at = ?, "
                         "review_note = ? WHERE id = ?", (status, by_user_id, _now(), note, row_id))
        conn.execute("INSERT INTO course_reviews (course_row_id, action, by_user_id, note, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", (row_id, action, by_user_id, note, _now()))
        conn.commit()


def update_trainer_course(row_id: int, data: dict) -> None:
    with closing(connect()) as conn:
        conn.execute(
            """
            UPDATE trainer_courses SET title = ?, description = ?, category = ?, designations = ?,
                skills = ?, videos = ?, target_level = ?, duration_minutes = ? WHERE id = ?
            """,
            (data["title"], data.get("description", ""), data.get("category", ""),
             json.dumps(data.get("designations", [])), json.dumps(data.get("skills", [])),
             json.dumps(data.get("videos", [])), int(data.get("target_level") or 1),
             data.get("duration_minutes"), row_id),
        )
        conn.commit()


def list_course_reviews(row_id: int | None = None, by_user_id: int | None = None) -> list[dict]:
    sql = ("SELECT r.*, u.username AS by_username, c.title AS course_title FROM course_reviews r "
           "LEFT JOIN users u ON u.id = r.by_user_id JOIN trainer_courses c ON c.id = r.course_row_id")
    where, params = [], []
    if row_id is not None:
        where.append("r.course_row_id = ?")
        params.append(row_id)
    if by_user_id is not None:
        where.append("r.by_user_id = ?")
        params.append(by_user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with closing(connect()) as conn:
        rows = conn.execute(sql + " ORDER BY r.id DESC", tuple(params)).fetchall()
    return [dict(r) for r in rows]



# ---------------------------------------------------------------------------
# account admin
# ---------------------------------------------------------------------------


def set_user_fields(user_id: int, **fields) -> None:
    allowed = {"active", "must_change_password", "is_primary", "password_hash", "role"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(k)
        sets.append(f"{k} = ?")
        params.append(v)
    with closing(connect()) as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*params, user_id))
        conn.commit()


def delete_user(user_id: int) -> None:
    """Only used to remove an extra admin (admins own no learner data)."""
    with closing(connect()) as conn:
        conn.execute("UPDATE audit_log SET actor_id = NULL WHERE actor_id = ?", (user_id,))
        conn.execute("UPDATE course_reviews SET by_user_id = NULL WHERE by_user_id = ?", (user_id,))
        conn.execute("UPDATE course_flags SET by_user_id = NULL WHERE by_user_id = ?", (user_id,))
        conn.execute("UPDATE trainer_courses SET reviewed_by = NULL WHERE reviewed_by = ?", (user_id,))
        conn.execute("UPDATE course_overrides SET trainer_id = NULL WHERE trainer_id = ?", (user_id,))
        conn.execute("DELETE FROM submissions WHERE user_id = ? AND id NOT IN "
                     "(SELECT submission_id FROM quiz_attempts)", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


def audit(actor_id: int | None, action: str, target: str = "", detail: str = "") -> None:
    with closing(connect()) as conn:
        conn.execute("INSERT INTO audit_log (actor_id, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                     (actor_id, action, target, detail, _now()))
        conn.commit()


def list_audit(limit: int = 300, action: str | None = None, actor_id: int | None = None) -> list[dict]:
    sql = "SELECT a.*, u.username AS actor, u.role AS actor_role FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id"
    where, params = [], []
    if action:
        where.append("a.action = ?")
        params.append(action)
    if actor_id:
        where.append("a.actor_id = ?")
        params.append(actor_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with closing(connect()) as conn:
        rows = conn.execute(sql + " ORDER BY a.id DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in rows]


def audit_actions() -> list[str]:
    with closing(connect()) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]


def unpublished_courses() -> dict[str, dict]:
    try:
        with closing(connect()) as conn:
            rows = conn.execute("SELECT * FROM course_flags WHERE unpublished = 1").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["course_id"]: dict(r) for r in rows}


def set_unpublished(course_id: str, unpublished: bool, by_user_id: int) -> None:
    with closing(connect()) as conn:
        conn.execute(
            """
            INSERT INTO course_flags (course_id, unpublished, by_user_id, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(course_id) DO UPDATE SET unpublished = excluded.unpublished,
                by_user_id = excluded.by_user_id, updated_at = excluded.updated_at
            """,
            (course_id, 1 if unpublished else 0, by_user_id, _now()),
        )
        conn.commit()



def log_activity(user_id: int, kind: str, course_id: str | None = None) -> None:
    with closing(connect()) as conn:
        conn.execute("INSERT INTO learner_activity (user_id, kind, course_id, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, kind, course_id, _now()))
        conn.commit()


def list_activity(user_id: int) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM learner_activity WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def list_flashcard_marks(user_id: int) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM flashcard_status WHERE user_id = ?", (user_id,)).fetchall()
    return [dict(r) for r in rows]
