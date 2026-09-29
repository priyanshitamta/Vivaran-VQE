"""
Vivaran-VQE - warm the MCQ cache before a demo.

Runs System 2's real pipeline (video -> audio -> Whisper transcript -> LLM
MCQs -> fact-check) for courses ahead of time and stores the questions in the
quiz_bank table, so learners get their quiz instantly instead of waiting
minutes. Already-cached courses are skipped.

    python pregenerate_quizzes.py                 # every course on any learner's roadmap
    python pregenerate_quizzes.py C073 DOC004     # specific course ids
    python pregenerate_quizzes.py --all           # every course in the catalogue (slow!)

Run it with the same interpreter as the web app (the System 2 venv).
"""

from __future__ import annotations

import sys

import courses
import db
import s2_bridge
import settings


def targets(args: list[str]) -> list[str]:
    if "--all" in args:
        return [c["course_id"] for c in courses.all_courses()]
    if args:
        return args
    ids: list[str] = []
    for learner in db.list_learners_with_profiles():
        sub = db.latest_submission(learner["id"])
        for rec in (sub or {}).get("recommendations") or []:
            if rec["course_id"] not in ids:
                ids.append(rec["course_id"])
    return ids


def main() -> int:
    db.init_db()
    ids = targets(sys.argv[1:])
    print(f"{len(ids)} course(s) to check")
    for cid in ids:
        course = courses.get(cid)
        if not course or not course["videos"]:
            print(f"  skip {cid}: no such course or no video")
            continue
        if any(db.get_bank(cid, v) for v in course["videos"]):
            print(f"  cached {cid}: {course['title']}")
            continue
        for video in course["videos"]:
            print(f"  generating {cid}: {course['title']}  <- {video}")
            try:
                questions, _ = s2_bridge.generate_quiz(
                    video, topic=course["title"], num_questions=settings.QUIZ_QUESTION_COUNT)
            except Exception as err:  # noqa: BLE001
                print(f"    failed: {err}")
                continue
            if questions:
                db.save_bank(cid, video, questions)
                print(f"    saved {len(questions)} questions")
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
