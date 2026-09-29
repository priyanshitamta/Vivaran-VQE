"""
Interactive employee intake for System 1 - the Recommendation Engine.

This script asks you for EVERY parameter of an employee profile (matching
Dataset-3 fields), one at a time, with examples and full lists available via
'?' where appropriate. When you finish, it:

  1. Validates the input (role exists, skill levels 1-3).
  2. Computes skill gaps (Stage B).
  3. Recommends courses to close those gaps (Stage C).
  4. Reports unmatched gaps (Stage D).
  5. Saves the profile to Dataset-5 for research purposes.

Usage
-----
The server must be running first (Terminal 1)::

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Then run this script (Terminal 2)::

    python interactive_intake.py

Answer each question. Press Enter with no text to skip optional fields.
Type '?' at prompts that support it to see the full list of valid choices.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

import httpx

BASE = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ask(question: str, example: str = "", hint_list: bool = False) -> str:
    """Ask one question, optionally showing an example and '?' hint."""
    prompt = f"\n? {question}"
    if example:
        prompt += f"\n  (Example: {example}"
        if hint_list:
            prompt += " | Type '?' for full list"
        prompt += ")"
    prompt += "\n> "
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n(Stopping.)")
        sys.exit(0)
    return answer


def ask_int(question: str, example: str = "") -> int | None:
    """Ask for an integer; returns None when skipped."""
    raw = ask(question, example, hint_list=False)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"  !! '{raw}' is not a number - using 0.")
        return 0


def ask_with_list(
    client: httpx.Client,
    question: str,
    example: str,
    fetch_list_fn,
    display_fn,
) -> str:
    """Ask a question; if the user types '?', show the full list and ask again."""
    while True:
        answer = ask(question, example, hint_list=True)
        if answer == "?":
            print("\nAvailable options:")
            items = fetch_list_fn(client)
            for item in items:
                print(f"  {display_fn(item)}")
            continue
        return answer


def fetch_unique_values(client: httpx.Client, field: str) -> list[str]:
    """Fetch the distinct values for one profile field from the live engine.

    Uses /meta/unique-values; falls back to the role catalogue for
    designation/department when that endpoint is unavailable (e.g. the
    server has not been restarted since it was added).
    """
    values: set[str] = set()
    try:
        resp = client.get("/meta/unique-values")
        if resp.status_code == 200:
            values.update(resp.json().get(field, []) or [])
    except Exception:  # noqa: BLE001
        pass
    if field in ("designation", "department") and not values:
        try:
            roles = client.get("/meta/roles").json()
            for role in roles:
                value = role.get(field)
                if value:
                    values.add(str(value).strip())
        except Exception:  # noqa: BLE001
            pass
    return sorted(v for v in values if v and str(v).strip())


def ask_skills(client: httpx.Client, question: str, example: str) -> dict[str, int]:
    """Collect skill_id:level pairs one at a time, with the full skill list
    shown on '?'."""
    skills = client.get("/meta/skills").json()
    catalogue = {s["skill_id"]: s["skill_name"] for s in skills}

    print(f"\n? {question}")
    print(f"  (Example: {example} | Type '?' to see all 16 skills)")

    result: dict[str, int] = {}
    while True:
        sid = ask("  Enter a skill_id (or press Enter to finish)", "S001").upper()
        if not sid:
            break
        if sid == "?":
            print("\n  Available skills:")
            for s_id in sorted(catalogue):
                print(f"    {s_id}  {catalogue[s_id]}")
            continue
        if sid not in catalogue:
            print(f"  !! '{sid}' is not valid - type '?' to see the list.")
            continue
        level = ask_int(f"  Level for {sid} ({catalogue[sid]})", "1, 2, or 3")
        if level is None or not (1 <= level <= 3):
            print("  !! Level must be 1, 2, or 3 - skipping this skill.")
            continue
        result[sid] = level
        print(f"  -> {sid} = level {level} recorded.")
    return result


def ask_courses(client: httpx.Client) -> list[str]:
    """Collect course_ids (previous trainings), with the full catalogue shown on '?'."""
    print("\n? Previous trainings (courses already completed)")
    print("  (Example: C119,C684 - comma separated, or Enter to skip)")
    print("  Type '?' to see all 784 courses")

    while True:
        raw = input("> ").strip()
        if not raw:
            return []
        if raw == "?":
            try:
                courses = client.get("/meta/courses").json()
            except Exception:  # noqa: BLE001
                print("  (Course list not available via API - enter ids directly)")
                continue
            print("\n  All courses (784 total):")
            for c in courses[:50]:  # show first 50 to avoid overwhelming
                print(f"    {c['course_id']}  {c['course_title']}")
            print("  ... (and 734 more)")
            continue
        return [x.strip().upper() for x in raw.split(",") if x.strip()]


def show_results(body: dict) -> None:
    """Pretty-print the engine's response."""
    emp = body["employee"]
    print("\n" + "=" * 70)
    print(f"EMPLOYEE: {emp['name']} ({emp['employee_id']})")
    print(f"  {emp['designation']} | {emp['department']}")
    print("=" * 70)

    print(f"\n-- YOUR SKILL GAPS ({len(body['gaps'])} total) --")
    if not body["gaps"]:
        print("  (No gaps - you already meet all requirements for this role!)")
    for g in body["gaps"]:
        print(
            f"  {g['skill_name']} ({g['skill_id']}): "
            f"level {g['current_level']} -> {g['required_level']} "
            f"(gap {g['gap']}, priority {g['priority_weight']})"
        )

    print(f"\n-- RECOMMENDED COURSES ({len(body['recommendations'])} total) --")
    if not body["recommendations"]:
        print("  (No recommendations - either no gaps or no matching courses)")
    for i, rec in enumerate(body["recommendations"], 1):
        skills = ", ".join(rec["matched_skills"])
        print(
            f"\n  {i}. {rec['course_title']} [{rec['course_id']}]"
            f"\n     Target Level {rec['target_level']} | Score {rec['score']:.3f} | "
            f"{rec['mode']} | {rec['duration_minutes']} min"
            f"\n     Covers: {skills}"
        )

    if body["unmatched_gaps"]:
        print(f"\n-- GAPS WITH NO AVAILABLE COURSES ({len(body['unmatched_gaps'])}) --")
        for u in body["unmatched_gaps"]:
            print(f"  ! {u['skill_name']}: {u['message']}")

    print("\n" + "=" * 70)
    print("Your information has been stored in the database for research purposes.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main intake flow
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        client = httpx.Client(base_url=BASE, timeout=30)
        client.get("/health")
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach the engine at {BASE}: {exc}")
        print("Start it first with:  python -m uvicorn app.main:app --port 8000")
        sys.exit(1)

    print("=" * 70)
    print("System 1 - Recommendation Engine | Interactive Employee Intake")
    print("=" * 70)
    print("\nI'll ask you for every parameter of the employee profile.")
    print("Skip optional fields by pressing Enter.")
    print("Type '?' at prompts that support it to see full lists.\n")

    # --- 1. Identity (no '?' option for these) -----------------------------
    employee_id = ask("Employee ID (auto-assigned if blank)", "E809")
    name = ask("Full name", "Priya Sharma")

    # --- 2. Designation + department (cascading filter) --------------------
    # Designation: show all unique designations from Dataset-2
    designation = ask_with_list(
        client,
        "Designation",
        "Director General",
        lambda c: sorted({
            r["designation"]
            for r in c.get("/meta/roles").json()
            if r.get("designation")
        }),
        lambda d: d,
    )

    # Department: show ONLY departments that pair with the chosen designation
    def fetch_departments_for_designation(c: httpx.Client) -> list[str]:
        roles = c.get("/meta/roles").json()
        return sorted({
            r["department"]
            for r in roles
            if r.get("designation", "").strip().lower() == designation.strip().lower()
            and r.get("department")
        })

    department = ask_with_list(
        client,
        f"Department (for {designation})",
        "NSO",
        fetch_departments_for_designation,
        lambda d: d,
    )

    # Derive role_id from the (designation, department) pair
    roles = client.get("/meta/roles").json()
    role_id = None
    for r in roles:
        if (
            r["designation"].strip().lower() == designation.strip().lower()
            and r["department"].strip().lower() == department.strip().lower()
        ):
            role_id = r["role_id"]
            break

    if not role_id:
        print(
            f"\n!! No role matches designation='{designation}' + department='{department}'"
        )
        print("   Using the first role (R001) as fallback.")
        role_id = "R001"

    # --- 3. Work context (with '?' lists) ----------------------------------
    current_assignment = ask_with_list(
        client,
        "Current assignment (what they work on)",
        "National Accounts Division",
        lambda c: fetch_unique_values(c, "current_assignment"),
        lambda a: a,
    )

    educational_qualifications = ask_with_list(
        client,
        "Educational qualifications",
        "M.Sc. Statistics",
        lambda c: fetch_unique_values(c, "educational_qualifications"),
        lambda q: q,
    )

    work_experience_years = ask_int("Work experience (years)", "10") or 0

    # --- 4. Skills (with '?' options) --------------------------------------
    self_rated_skills = ask_skills(
        client, "Self-rated skills (your own assessment)", "S001:2;S002:1"
    )

    quiz_verified_skills = ask_skills(
        client,
        "Quiz-verified skills (confirmed by quiz - optional)",
        "S001:2",
    )

    # --- 5. Previous trainings (with '?' course list) ----------------------
    previous_trainings = ask_courses(client)

    # --- 6. Send to engine and show results --------------------------------
    payload = {
        "name": name or None,
        "role_id": role_id,
        "designation": designation or None,
        "department": department or None,
        "current_assignment": current_assignment or None,
        "educational_qualifications": educational_qualifications or None,
        "work_experience_years": work_experience_years or None,
        "previous_trainings": previous_trainings,
        "self_rated_skills": self_rated_skills,
        "quiz_verified_skills": quiz_verified_skills,
    }
    if employee_id:
        payload["employee_id"] = employee_id

    print("\n" + "-" * 70)
    print("Computing gaps and recommendations...")
    print("-" * 70)

    resp = client.post("/employees/register", json=payload)
    print(f"HTTP {resp.status_code}")

    if resp.status_code != 201:
        print("\nERROR from engine:")
        detail = resp.json().get("detail", resp.text)
        if isinstance(detail, list):
            for err in detail:
                print(f"  - {err.get('msg', err)}")
        else:
            print(f"  {detail}")
        sys.exit(1)

    show_results(resp.json())


if __name__ == "__main__":
    main()
