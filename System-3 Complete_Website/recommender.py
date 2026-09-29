"""
Vivaran-VQE (System 3) - learner recommendation + roadmap.

Pipeline for one learner:

  1. Skill gaps for the learner's designation (System 1).
     We call System 1's existing ``POST /employees/compute`` with the
     learner's role and the skills already verified by passed course quizzes.
     System 1 owns the gap maths (required level - current level, with
     priority weights). If System 1 is down we run the same formula locally
     over its read-only Dataset-2 so the dashboard never breaks mid-demo.

  2. Score every course in the catalogue (courses.py) the learner has NOT
     completed:
        gap coverage   sum(gap x priority_weight) of role gaps the course closes
        field fit      +2 per skill shared with the learner's Area of Experience
        designation    +6 if the course targets the learner's designation
        continuity     +4 if the learner already started it (keep it in the path)

  3. Pick the path greedily: take the best course, mark its gap skills as
     covered, re-score the rest on what is still uncovered, repeat. It stops
     once the gaps are covered (at least ROADMAP_MIN, at most ROADMAP_MAX), so
     the number of courses is decided by the learner's gaps and the catalogue,
     not a fixed top-N.

  4. Order the path as a roadmap: foundation level first, then the higher
     levels; within a level, the course closing the most critical gaps first.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import courses
import s1_client
import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Roles (System 1 Dataset-2) - designation lives here
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _dataset2() -> list[dict]:
    return courses._read_csv(courses.S1_DATASETS / "Dataset-2_Required_Competency.csv")


def roles() -> list[dict]:
    """[{role_id, designation, department}] - System 1 first, CSV fallback."""
    try:
        return s1_client.meta_roles()
    except (s1_client.S1Unavailable, s1_client.S1Error):
        seen: dict[str, dict] = {}
        for row in _dataset2():
            seen.setdefault(row["role_id"], {
                "role_id": row["role_id"],
                "designation": row["designation"],
                "department": row["department"],
            })
        return sorted(seen.values(), key=lambda r: r["role_id"])


def role(role_id: str) -> dict | None:
    return next((r for r in roles() if r.get("role_id") == role_id), None)


def _local_gaps(role_id: str, verified: dict[str, int]) -> list[dict]:
    gaps = []
    for row in _dataset2():
        if row["role_id"] != role_id:
            continue
        required = int(row["required_level"])
        current = verified.get(row["skill_id"], 0)
        if required - current > 0:
            gaps.append({
                "skill_id": row["skill_id"],
                "skill_name": courses.skill_name(row["skill_id"]),
                "current_level": current,
                "required_level": required,
                "gap": required - current,
                "priority_weight": int(row["priority_weight"]),
            })
    gaps.sort(key=lambda g: (-g["gap"], -g["priority_weight"], g["skill_id"]))
    return gaps


def verified_skills(completed: list[dict]) -> dict[str, int]:
    """Skill levels proven by passed course quizzes (course target level)."""
    levels: dict[str, int] = {}
    for enrollment in completed:
        course = courses.get(enrollment["course_id"])
        if not course:
            continue
        for sid in course["skills"]:
            if sid in courses.skills():
                levels[sid] = max(levels.get(sid, 0), course["target_level"])
    return levels


def skill_gaps(profile: dict, completed: list[dict]) -> tuple[list[dict], str]:
    """(gaps, engine) for the learner's role. engine = 'system1' | 'local'."""
    verified = verified_skills(completed)
    payload = {
        "employee_id": profile.get("s1_employee_id") or None,
        "role_id": profile["role_id"],
        "designation": profile.get("designation"),
        "department": profile.get("department"),
        "quiz_verified_skills": verified,
        "previous_trainings": [e["course_id"] for e in completed
                               if e["course_id"].startswith("C")],
    }
    try:
        result = s1_client.compute(payload, top_n=1)
        return result.get("gaps") or [], "system1"
    except (s1_client.S1Unavailable, s1_client.S1Error) as err:
        logger.warning("System 1 compute unavailable (%s) - using local gap calc", err)
        return _local_gaps(profile["role_id"], verified), "local"


# ---------------------------------------------------------------------------
# Scoring + roadmap
# ---------------------------------------------------------------------------

def _designation_match(course: dict, profile: dict) -> bool:
    targets = [t.lower() for t in course.get("designations") or []]
    if not targets:
        return False
    if any(t in ("all", "all officers", "all employees", "everyone") for t in targets):
        return True
    mine = [(profile.get("designation") or "").lower(), (profile.get("department") or "").lower()]
    return any(m and (m in t or t in m) for t in targets for m in mine)


def _score(course: dict, open_gaps: dict[str, dict], area_skills: set[str],
           profile: dict, started: set[str]) -> tuple[float, list[str]]:
    covers = [s for s in course["skills"] if s in open_gaps]
    gap_score = sum(open_gaps[s]["gap"] * open_gaps[s]["priority_weight"] for s in covers)
    field_fit = 2 * len(area_skills.intersection(course["skills"]))
    desig = 6 if _designation_match(course, profile) else 0
    continuity = 4 if course["course_id"] in started else 0
    # Tiny tie-breakers: prefer real descriptions and courses that have a video
    # (a course without a video cannot produce a quiz).
    quality = (0.3 if len(course.get("description") or "") > 60 else 0) + \
              (0.5 if course.get("videos") else 0)
    return gap_score + field_fit + desig + continuity + quality, covers


def _reason(course: dict, covers: list[str], area: str, profile: dict) -> str:
    parts = []
    if covers:
        parts.append("Closes your role gap in " + ", ".join(courses.skill_name(s) for s in covers))
    if _designation_match(course, profile):
        parts.append(f"designed for {profile.get('designation')}")
    field = set(courses.AREAS_OF_EXPERIENCE.get(area, [])).intersection(course["skills"])
    if field and not covers:
        parts.append(f"fits your {area} background")
    return "; ".join(parts) or "Relevant to your designation and field"


def build_roadmap(profile: dict, enrollments: list[dict],
                  overrides: dict[str, str] | None = None) -> dict:
    """Recommend a learning path for a learner profile.

    ``enrollments`` are the learner's rows from db.list_enrollments (both
    completed and in-progress). Completed courses are never recommended again.
    ``overrides`` ({course_id: 'add'|'remove'}) are the learner's trainer's
    edits: added courses are always on the path, removed ones never are.
    """
    overrides = overrides or {}
    removed = {cid for cid, a in overrides.items() if a == "remove"}
    import db  # admin-unpublished courses are never recommended
    removed |= set(db.unpublished_courses())
    added = [cid for cid, a in overrides.items() if a == "add"]
    completed = [e for e in enrollments if e["status"] == "completed"]
    completed_ids = {e["course_id"] for e in completed}
    started = {e["course_id"] for e in enrollments if e["status"] != "completed"}
    area = profile.get("area_of_experience") or ""
    area_skills = set(courses.AREAS_OF_EXPERIENCE.get(area, []))

    gaps, engine = skill_gaps(profile, completed)
    open_gaps = {g["skill_id"]: g for g in gaps}
    candidates = [c for c in courses.all_courses()
                  if c["course_id"] not in completed_ids and c["course_id"] not in removed]

    picked: list[dict] = []
    for cid in added:  # trainer-pinned courses first; they also cover gaps
        course = courses.get(cid)
        if not course or cid in completed_ids:
            continue
        covers = [s for s in course["skills"] if s in open_gaps]
        picked.append({**course, "covers": covers, "added_by_trainer": True,
                       "reason": "Added by your trainer",
                       "score": 0.0,
                       "gap_weight": sum(open_gaps[s]["gap"] * open_gaps[s]["priority_weight"]
                                         for s in covers)})
        for sid in covers:
            open_gaps.pop(sid, None)
        candidates = [c for c in candidates if c["course_id"] != cid]
    while candidates and len(picked) < settings.ROADMAP_MAX_COURSES:
        best, best_score, best_covers = None, 0.0, []
        for course in candidates:
            score, covers = _score(course, open_gaps, area_skills, profile, started)
            if score > best_score:
                best, best_score, best_covers = course, score, covers
        if best is None or best_score < 1:
            break
        if not best_covers and len(picked) >= settings.ROADMAP_MIN_COURSES:
            break  # gaps are covered and we already have a full path
        picked.append({**best, "covers": best_covers,
                       "reason": _reason(best, best_covers, area, profile),
                       "score": round(best_score, 2),
                       "gap_weight": sum(open_gaps[s]["gap"] * open_gaps[s]["priority_weight"]
                                         for s in best_covers)})
        for sid in best_covers:
            open_gaps.pop(sid, None)
        candidates = [c for c in candidates if c["course_id"] != best["course_id"]]

    # Roadmap order: foundation -> advanced, most critical first within a level.
    picked.sort(key=lambda c: (c["target_level"], -c["gap_weight"]))
    for step, course in enumerate(picked, start=1):
        course["step"] = step

    covered = {s for c in picked for s in c["covers"]}
    return {
        "courses": picked,
        "gaps": gaps,
        "uncovered_gaps": [g for g in gaps if g["skill_id"] not in covered],
        "engine": engine,
        "catalogue": courses.source_label(),
    }


def to_submission_recs(roadmap: dict) -> list[dict]:
    """Shape roadmap courses like System 1 recommendations so the existing
    ``submissions`` table / results + profile pages keep working unchanged."""
    return [
        {
            "course_id": c["course_id"],
            "course_title": c["title"],
            "description": c["description"],
            "provider": c.get("provider"),
            "mode": c.get("mode"),
            "duration_minutes": c.get("duration_minutes"),
            "language": c.get("language"),
            "target_level": c["target_level"],
            "matched_skills": c["covers"],
            "matched_skill_names": [courses.skill_name(s) for s in c["covers"]],
            "reason": c["reason"],
            "step": c["step"],
            "score": c["score"],
            "added_by_trainer": bool(c.get("added_by_trainer")),
        }
        for c in roadmap["courses"]
    ]


def course_to_rec(course: dict, step: int, reason: str, added_by_trainer: bool = False) -> dict:
    """One catalogue course in the stored-recommendation shape."""
    return {
        "course_id": course["course_id"],
        "course_title": course["title"],
        "description": course["description"],
        "provider": course.get("provider"),
        "mode": course.get("mode"),
        "duration_minutes": course.get("duration_minutes"),
        "language": course.get("language"),
        "target_level": course["target_level"],
        "matched_skills": list(course.get("skills") or []),
        "matched_skill_names": [courses.skill_name(s) for s in course.get("skills") or []],
        "reason": reason,
        "step": step,
        "score": 0.0,
        "added_by_trainer": added_by_trainer,
    }


# ---------------------------------------------------------------------------
# Trainer <-> learner matching
# ---------------------------------------------------------------------------

def learner_skill_needs(profile: dict) -> set[str]:
    """Skills a learner needs: their field's skills + their role's required skills."""
    needs = set(courses.AREAS_OF_EXPERIENCE.get(profile.get("area_of_experience") or "", []))
    needs |= {r["skill_id"] for r in _dataset2() if r["role_id"] == profile.get("role_id")}
    return needs


def match_score(trainer_specs: list[str], profile: dict) -> int:
    return len(set(trainer_specs) & learner_skill_needs(profile))
