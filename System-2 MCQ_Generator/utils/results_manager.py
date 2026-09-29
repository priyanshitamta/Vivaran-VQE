import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

# Sits next to app.py, same folder level as UPLOAD_FOLDER.
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results.db")


def init_db(db_path=None):
    """Create the quiz_attempts table if it doesn't exist yet. Safe to call every time."""
    path = db_path or DB_PATH
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                course_name TEXT NOT NULL,
                attempt_type TEXT NOT NULL,      -- 'lecture' or 'final'
                lecture_number INTEGER,          -- NULL for the final quiz
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                accuracy REAL NOT NULL,
                details TEXT NOT NULL,           -- JSON: per-question breakdown
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_quiz_result(user_id, course_name, attempt_type, lecture_number, questions, answers, db_path=None):
    """
    Score one submitted quiz batch and persist it.

    questions: the list of question dicts the user was just shown (that lecture's
               batch, or the final-quiz batch) -- NOT the whole question bank.
    answers:   dict of {str(question_id): selected_option} covering THOSE questions.

    Returns the summary dict that was stored, so the caller can use it immediately.
    """
    path = db_path or DB_PATH
    init_db(path)  # cheap no-op once the table exists

    details = []
    score = 0

    for question in questions:
        qid = str(question["id"])
        selected = answers.get(qid)
        correct = question.get("answer", "")
        is_correct = bool(selected) and selected.lower() == correct.lower()
        if is_correct:
            score += 1

        details.append({
            "question_id": question["id"],
            "question": question.get("question", ""),
            "selected_answer": selected,
            "correct_answer": correct,
            "is_correct": is_correct,
            "verification_status": question.get("verification_status", "unverified"),
        })

    total = len(questions)
    accuracy = round((score / total) * 100, 2) if total else 0.0

    row = (
        user_id,
        course_name,
        attempt_type,
        lecture_number,
        score,
        total,
        accuracy,
        json.dumps(details),
        datetime.now(timezone.utc).isoformat(),
    )

    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO quiz_attempts
                (user_id, course_name, attempt_type, lecture_number, score, total, accuracy, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        conn.commit()

    return {
        "attempt_type": attempt_type,
        "lecture_number": lecture_number,
        "score": score,
        "total": total,
        "accuracy": accuracy,
        "details": details,
    }


def get_progress(user_id, course_name, db_path=None):
    """All attempts for one user + course, oldest first -- the raw timeline."""
    path = db_path or DB_PATH
    init_db(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT attempt_type, lecture_number, score, total, accuracy, details, created_at
            FROM quiz_attempts
            WHERE user_id = ? AND course_name = ?
            ORDER BY created_at ASC
            """,
            (user_id, course_name),
        ).fetchall()

    return [
        {
            "attempt_type": r["attempt_type"],
            "lecture_number": r["lecture_number"],
            "score": r["score"],
            "total": r["total"],
            "accuracy": r["accuracy"],
            "details": json.loads(r["details"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_improvement_summary(user_id, course_name, db_path=None):
    """
    A simple end-of-course improvement signal: accuracy on the first lecture
    quiz vs. the last lecture quiz, plus the final quiz if one was taken.
    """
    attempts = get_progress(user_id, course_name, db_path)
    lecture_attempts = [a for a in attempts if a["attempt_type"] == "lecture"]
    final_attempts = [a for a in attempts if a["attempt_type"] == "final"]

    summary = {
        "lecture_count": len(lecture_attempts),
        "first_lecture_accuracy": lecture_attempts[0]["accuracy"] if lecture_attempts else None,
        "last_lecture_accuracy": lecture_attempts[-1]["accuracy"] if lecture_attempts else None,
        "final_quiz_accuracy": final_attempts[-1]["accuracy"] if final_attempts else None,
        "improvement": None,
    }

    if summary["first_lecture_accuracy"] is not None and summary["last_lecture_accuracy"] is not None:
        summary["improvement"] = round(
            summary["last_lecture_accuracy"] - summary["first_lecture_accuracy"], 2
        )

    return summary

