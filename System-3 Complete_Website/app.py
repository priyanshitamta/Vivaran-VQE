"""
Vivaran-VQE (System 3) - full-stack web shell tying Systems 1 & 2 together.

Routes / auth / page flow only - the recommendation pipeline runs in
System 1 (called over HTTP via ``s1_client``) and the quiz pipeline runs
in-process against System 2's utils via ``s2_bridge``. Our own SQLite DB
(``db``) keeps auth, a submission snapshot per intake, and one row per quiz
attempt.

Page map (post-login pages extend templates/base.html for the shared
header/info dropdown):
    /                      landing (pre-login)
    /login                 login (pre-login)
    (modal)                register, POST /api/register
    /recommendation        employee intake form  -> POST -> S1 register
    /recommendation/results/<id>   top-5 courses + "Take a quiz" per course
    /quiz/<id>             quiz-taking (S2-generated) or status while generating
    /quiz/<id>/results     score / analysis page
    /profile               history for this username (trainer/admin intake history)

Learner platform (role-based: learner / trainer / admin):
    /dashboard             learner dashboard: profile, my learning, recommended, roadmap
    /onboarding            designation + area of experience (first login of legacy accounts)
    /courses/<id>          course page: video(s), progress, quiz (System 2)
    /courses/<id>/flashcards  flashcards from the learner's graded quiz questions
    /trainer               trainer dashboard (+ the original intake flow above)
    /admin                 admin dashboard (+ System 1 workforce analytics)
    /api/...               JSON APIs (see the API section at the bottom)
"""

from __future__ import annotations

import csv
import hashlib
import re
import hmac
import json
import logging
import os
import random
import secrets
import threading
from functools import lru_cache, wraps

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from werkzeug.security import check_password_hash, generate_password_hash

import courses
import db
import progress
import recommender
import s1_client
import s2_bridge
import settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [S3] %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("VIVARAN_SECRET", secrets.token_hex(24))
app.config["JSON_SORT_KEYS"] = False

RESULTS_LEAD = "On the basis of your current skill portfolio, here are your recommended courses."


# ---------------------------------------------------------------------------
# Context / auth helpers
# ---------------------------------------------------------------------------

@app.context_processor
def inject_user() -> dict:
    """Make ``current_user`` (username) available to every base.html page."""
    ctx = {
        "current_user": session.get("username", ""),
        "current_role": session.get("role", ""),
        "home_url": _home_url(session.get("role", "")) if "user_id" in session else "/",
    }
    if session.get("role") == "learner" and "user_id" in session:
        ctx["lnav"] = _learner_nav(int(session["user_id"]))
    return ctx


def _learner_nav(user_id: int) -> dict:
    """Small counts for the learner sidebar badges (cheap DB reads only)."""
    try:
        enrollments = db.list_enrollments(user_id)
        sub = db.latest_submission(user_id) or {}
        started = {e["course_id"] for e in enrollments}
        marks = db.get_flashcard_status(user_id)
        return {
            "in_progress": sum(1 for e in enrollments if e["status"] != "completed"),
            "recommended": sum(1 for r in sub.get("recommendations") or [] if r["course_id"] not in started),
            "review": sum(1 for v in marks.values() if v == "review"),
        }
    except Exception:  # noqa: BLE001 - the sidebar must never break a page
        return {"in_progress": 0, "recommended": 0, "review": 0}


ROLES = ("learner", "trainer", "admin")


def _home_url(role: str) -> str:
    return {
        "admin": url_for("admin_dashboard"),
        "trainer": url_for("trainer_dashboard"),
    }.get(role, url_for("learner_dashboard"))


def _new_password_hash(password: str) -> str:
    """Salted, slow hash (werkzeug scrypt/pbkdf2 with a random per-user salt)."""
    return generate_password_hash(password)


def _check_password(user: dict, password: str) -> bool:
    stored = user.get("password_hash") or ""
    if stored.startswith(("scrypt:", "pbkdf2:")):
        return check_password_hash(stored, password)
    # Legacy accounts from the first build: verify, then upgrade the hash.
    if _verify_password(user["username"], password, stored):
        db.update_password_hash(user["id"], _new_password_hash(password))
        return True
    return False


def _hash_password(username: str, password: str) -> str:
    """LEGACY (first build) - kept only so old hashes can still be verified."""
    salt = hashlib.sha256(f"antahai:{username}".encode("utf-8")).hexdigest()[:16]
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return f"{salt}${digest}"


def _verify_password(username: str, password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return hmac.compare_digest(candidate, digest)


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.is_json


PASSWORD_CHANGE_ALLOWED = {"change_password", "logout", "static"}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if _wants_json():
                return jsonify({"ok": False, "error": "Please log in."}), 401
            return redirect(url_for("login"))
        user = db.get_user_by_id(session["user_id"])
        if not user or not user.get("active", 1):
            session.clear()  # deactivated while logged in
            if _wants_json():
                return jsonify({"ok": False, "error": "This account has been deactivated."}), 401
            return redirect(url_for("login"))
        if user.get("must_change_password") and request.endpoint not in PASSWORD_CHANGE_ALLOWED:
            if _wants_json():
                return jsonify({"ok": False, "error": "Please set a new password first."}), 403
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*allowed: str):
    """Backend authorization: the session role must be one of ``allowed``.

    Enforced server-side on every protected route, so changing the URL does
    not get a learner into /admin (403), whatever the frontend shows.
    """
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("role") not in allowed:
                if _wants_json():
                    return jsonify({"ok": False, "error": "You do not have access to this."}), 403
                return render_template("forbidden.html"), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _require_user() -> int:
    return int(session.get("user_id", 0))


def _owned_submission(submission_id: int) -> dict:
    """Fetch a submission, aborting unless it belongs to the logged-in user."""
    submission = db.get_submission(submission_id)
    if not submission or submission["user_id"] != _require_user():
        abort(404)
    return submission


def _owned_attempt(attempt_id: int) -> dict:
    attempt = db.get_attempt(attempt_id)
    if not attempt or attempt["user_id"] != _require_user():
        abort(404)
    return attempt


def _json_body() -> dict | None:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return None


# ---------------------------------------------------------------------------
# Landing / auth
# ---------------------------------------------------------------------------

@app.route("/")
def landing():
    if "user_id" in session:
        return redirect(_home_url(session.get("role", "")))
    return render_template("landing.html", roles=_role_options(),
                           areas=list(courses.AREAS_OF_EXPERIENCE), skill_groups=_skill_groups())


def _role_options() -> list[dict]:
    """Designations for the register form (System 1's Dataset-2 roles)."""
    return sorted(recommender.roles(), key=lambda r: (r["designation"], r["department"]))


def _start_session(user: dict) -> None:
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user.get("role") or "learner"


@app.route("/login", methods=["GET", "POST"])
@app.route("/auth/login", methods=["POST"], endpoint="auth_login")
def login():
    if request.method == "GET" and "user_id" in session:
        return redirect(_home_url(session.get("role", "")))

    if request.method == "POST":
        body = _json_body() or request.form
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        wanted_role = (body.get("role") or "").strip().lower()
        if not username or not password:
            return jsonify({"ok": False, "error": "Please enter both username and password."}), 400
        user = db.get_user_by_username(username)
        if not user or not _check_password(user, password):
            return jsonify({"ok": False, "error": "Incorrect username or password. Please try again."}), 401
        if not user.get("active", 1):
            return jsonify({"ok": False, "error": "This account has been deactivated. Please contact the admin."}), 403
        role = user.get("role") or "learner"
        if wanted_role and wanted_role in ROLES and wanted_role != role:
            return jsonify({
                "ok": False,
                "error": f"This is a {role.title()} account. Please use the {role.title()} tab to log in.",
            }), 403
        _start_session(user)
        if user.get("must_change_password"):
            return jsonify({"ok": True, "role": role, "redirect": url_for("change_password")})
        return jsonify({"ok": True, "role": role, "redirect": _home_url(role)})

    return render_template("login.html", roles=_role_options(),
                           areas=list(courses.AREAS_OF_EXPERIENCE), skill_groups=_skill_groups())


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _validate_gov_email(email: str) -> str | None:
    """Error message, or None when ``email`` is a valid @gov.in address."""
    domain = settings.GOV_EMAIL_DOMAIN
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", email or ""):
        return "Please enter a valid email address."
    if email.rsplit("@", 1)[1].lower() != domain:
        return f"Please use your government email ID (for example rahul@{domain})."
    return None


@app.route("/api/register", methods=["POST"])
@app.route("/auth/register", methods=["POST"], endpoint="auth_register")
def api_register():
    """Self-registration for learners and trainers.

    learner: account + profile (designation, area) -> gets a trainer -> dashboard
    trainer: account + specialisations (skills) -> claims up to 5 learners -> trainer dashboard
    """
    body = _json_body()
    if not body:
        return jsonify({"ok": False, "error": "Invalid request."}), 400
    account_type = (body.get("account_type") or "learner").strip().lower()
    if account_type not in ("learner", "trainer"):
        return jsonify({"ok": False, "error": "Choose Learner or Trainer."}), 400
    username = (body.get("username") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    role_id = (body.get("role_id") or "").strip()
    area = (body.get("area_of_experience") or "").strip()

    if not USERNAME_RE.match(username):
        return jsonify({"ok": False, "error": "Username must be 3-32 letters, digits, dots, dashes or underscores."}), 400
    email_error = _validate_gov_email(email)
    if email_error:
        return jsonify({"ok": False, "field": "email", "error": email_error}), 400
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."}), 400
    if account_type == "trainer":
        specs = _clean_specialisations(body.get("specialisations"))
        if not specs:
            return jsonify({"ok": False, "field": "specialisations",
                            "error": "Pick at least one skill you specialise in."}), 400
        if db.get_user_by_username(username):
            return jsonify({"ok": False, "error": "Username already taken. Please log in or choose another."}), 409
        if db.get_user_by_email(email):
            return jsonify({"ok": False, "error": "An account with this email already exists."}), 409
        trainer_id = db.create_user(username, _new_password_hash(password), email=email, role="trainer")
        db.audit(trainer_id, "trainer_registered", username)
        db.upsert_trainer_profile(trainer_id, specs)
        _claim_batch(trainer_id)
        _start_session(db.get_user_by_id(trainer_id))
        return jsonify({"ok": True, "redirect": url_for("trainer_dashboard")})
    role_meta = recommender.role(role_id)
    if not role_meta:
        return jsonify({"ok": False, "field": "designation", "error": "Please choose your designation."}), 400
    if area not in courses.AREAS_OF_EXPERIENCE:
        return jsonify({"ok": False, "field": "area", "error": "Please choose your area of experience."}), 400
    if db.get_user_by_username(username):
        return jsonify({"ok": False, "error": "Username already taken. Please log in or choose another."}), 409
    if db.get_user_by_email(email):
        return jsonify({"ok": False, "error": "An account with this email already exists."}), 409

    user_id = db.create_user(username, _new_password_hash(password), email=email, role="learner")
    db.audit(user_id, "learner_registered", username)
    db.upsert_learner_profile(user_id, role_id, role_meta["designation"],
                              role_meta["department"], area)
    _register_in_system1(user_id, username)
    _assign_trainer_for(user_id)
    user = db.get_user_by_id(user_id)
    _start_session(user)
    _ensure_roadmap(user_id, rebuild=True)
    return jsonify({"ok": True, "redirect": url_for("learner_dashboard")})


def _register_in_system1(user_id: int, name: str) -> None:
    """Best effort: persist the learner in System 1 (Dataset-5) so admin
    analytics include them and quiz passes can raise their verified skills."""
    profile = db.get_learner_profile(user_id)
    if not profile or profile.get("s1_employee_id"):
        return
    try:
        result = s1_client.register_employee({
            "name": name,
            "role_id": profile["role_id"],
            "designation": profile["designation"],
            "department": profile["department"],
            "current_assignment": profile["area_of_experience"],
        }, top_n=1)
        db.set_learner_s1_id(user_id, result["employee"]["employee_id"])
    except (s1_client.S1Unavailable, s1_client.S1Error, KeyError) as err:
        logger.warning("Could not register learner %s in System 1: %s", user_id, err)


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    user = db.get_user_by_id(_require_user())
    forced = bool(user.get("must_change_password"))
    error = message = None
    if request.method == "POST":
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        confirm = request.form.get("confirm") or ""
        if not _check_password(user, current):
            error = "Your current (or temporary) password is not correct."
        elif len(new) < 8:
            error = "The new password must be at least 8 characters."
        elif new != confirm:
            error = "The two new passwords don't match."
        elif new == current:
            error = "Choose a password different from the current one."
        else:
            db.set_user_fields(user["id"], password_hash=_new_password_hash(new), must_change_password=0)
            db.audit(user["id"], "password_changed", user["username"])
            if forced:
                return redirect(_home_url(user.get("role", "")))
            message = "Password updated."
    return render_template("change_password.html", forced=forced, error=error, message=message)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ---------------------------------------------------------------------------
# Recommendation intake (System 1 page)
# ---------------------------------------------------------------------------

def _build_meta() -> dict | None:
    """Metadata for the intake form, or None if System 1 is unreachable."""
    try:
        roles = s1_client.meta_roles()
        skills = [
            {"skill_id": s.get("skill_id"), "skill_name": s.get("skill_name", "")}
            for s in s1_client.meta_skills()
            if s.get("skill_id")
        ]
        courses = s1_client.meta_courses_light()
        unique = s1_client.meta_unique_values()
    except (s1_client.S1Unavailable, s1_client.S1Error):
        return None
    return {
        "roles": roles,
        "skills": skills,
        "courses": courses,
        "assignments": unique.get("current_assignment") or [],
        "qualifications": unique.get("educational_qualifications") or [],
    }


@app.route("/recommendation", methods=["GET", "POST"])
@role_required("trainer", "admin")
def recommendation():
    if request.method == "POST":
        return _register_employee()

    meta = _build_meta()
    return render_template(
        "recommendation.html",
        meta=meta,
        s1_down=meta is None,
        s1_url=settings.S1_URL,
    )


def _register_employee():
    body = _json_body()
    if not body:
        return jsonify({"ok": False, "error": "Invalid request."}), 400

    designation = (body.get("designation") or "").strip()
    department = (body.get("department") or "").strip()
    role_id = (body.get("role_id") or "").strip()

    # Resolve role_id from designation + department if the client didn't send
    # it (mirrors System 1's frontend: role = designation/department pair).
    if not role_id and designation and department:
        try:
            for role in s1_client.meta_roles():
                if role.get("designation") == designation and role.get("department") == department:
                    role_id = role.get("role_id", "")
                    break
        except (s1_client.S1Unavailable, s1_client.S1Error):
            return jsonify({"ok": False, "error": "Cannot reach the recommendation engine."}), 503

    payload = {
        "name": (body.get("name") or "").strip(),
        "role_id": role_id,
        "designation": designation,
        "department": department,
        "current_assignment": (body.get("current_assignment") or "").strip() or None,
        "educational_qualifications": (body.get("educational_qualifications") or "").strip() or None,
        "work_experience_years": body.get("work_experience_years"),
        "self_rated_skills": body.get("self_rated_skills") or {},
        "quiz_verified_skills": {},
        "previous_trainings": body.get("previous_trainings") or [],
    }

    # Server-side presence checks (role_id must be non-empty / meaningful).
    if not payload["role_id"]:
        return jsonify({"ok": False, "error": "Please choose a designation and department."}), 400
    if not isinstance(payload["work_experience_years"], int):
        return jsonify({"ok": False, "error": "Work experience must be a whole number of years."}), 400

    try:
        result = s1_client.register_employee(payload, top_n=5)
    except s1_client.S1Unavailable as err:
        return jsonify({"ok": False, "error": str(err)}), 503
    except s1_client.S1Error as err:
        return jsonify({"ok": False, "error": f"The engine rejected this profile: {err}"}), 422

    employee = result["employee"]
    recommendations = result["recommendations"]

    # Enrich each recommendation with human skill names for the results /
    # profile pages (they render offline, without calling System 1 again).
    try:
        skill_names = {s["skill_id"]: s.get("skill_name", s["skill_id"]) for s in s1_client.meta_skills()}
    except (s1_client.S1Unavailable, s1_client.S1Error):
        skill_names = {}
    enriched = []
    for rec in recommendations:
        rec = dict(rec)
        rec["matched_skill_names"] = [
            skill_names.get(sid, sid) for sid in (rec.get("matched_skills") or [])
        ]
        enriched.append(rec)

    submission_id = db.create_submission(
        _require_user(),
        {
            "employee_id": employee["employee_id"],
            "name": employee.get("name") or "",
            "role_id": employee.get("role_id") or "",
            "designation": employee.get("designation") or "",
            "department": employee.get("department") or "",
            "current_assignment": employee.get("current_assignment"),
            "educational_qualifications": employee.get("educational_qualifications"),
            "work_experience_years": employee.get("work_experience_years"),
            "previous_trainings": employee.get("previous_trainings") or [],
            "self_rated_skills": employee.get("self_rated_skills") or {},
            "quiz_verified_skills": {},
            "recommendations": enriched,
        },
    )
    return jsonify({"ok": True, "redirect": url_for("submission_results", submission_id=submission_id)})


# ---------------------------------------------------------------------------
# Recommendation results page
# ---------------------------------------------------------------------------

def _course_rows(submission: dict) -> list[dict]:
    """[(course, latest_attempt)] for each recommendation, in recommendation order.

    Shared by the results and profile pages so both render one action per course
    from the same shape (latest_attempt is the newest quiz_attempts row).
    """
    attempts_by_course: dict[str, list[dict]] = {}
    for attempt in db.list_attempts_for_submission(submission["id"]):
        attempts_by_course.setdefault(attempt["course_id"], []).append(attempt)
    rows = []
    for rec in submission.get("recommendations") or []:
        attempts = attempts_by_course.get(rec.get("course_id")) or []
        rows.append({"course": rec, "latest_attempt": attempts[-1] if attempts else None})
    return rows


def _recommendation_summary(submission: dict) -> tuple[str, str]:
    """(lead, sub) copy shown above the course cards (matches System 1's tone)."""
    recs = submission.get("recommendations") or []
    lead = RESULTS_LEAD
    if not recs:
        return lead, "No matching courses were found for this profile."
    addressed = {s for rec in recs for s in (rec.get("matched_skills") or [])}
    n = len(addressed)
    designation = submission.get("designation") or "your role"
    department = submission.get("department") or "your department"
    noun = "skill gap" if n == 1 else "skill gaps"
    sub = (
        f"These courses address the {n} {noun} identified for your "
        f"{designation} role in {department}."
    )
    return lead, sub


@app.route("/recommendation/results/<int:submission_id>")
@role_required("trainer", "admin")
def submission_results(submission_id: int):
    submission = _owned_submission(submission_id)

    courses = _course_rows(submission)

    lead, sub = _recommendation_summary(submission)
    return render_template(
        "results.html",
        submission=submission,
        courses=courses,
        lead=lead,
        sub=sub,
    )


# ---------------------------------------------------------------------------
# Quiz: start (random Dataset-6 link -> background S2 pipeline)
# ---------------------------------------------------------------------------

def _random_dataset6_link() -> str:
    if not settings.DATASET6_CSV.exists():
        raise RuntimeError(f"Dataset-6 missing at {settings.DATASET6_CSV}")
    with open(settings.DATASET6_CSV, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        links = [row["Link"].strip() for row in reader if (row.get("Link") or "").strip()]
    if not links:
        raise RuntimeError("Dataset-6 contains no video links")
    return random.choice(links)


def _run_quiz_job(attempt_id: int, course_title: str, link: str,
                  course_id: str | None = None, fallbacks: list[str] | None = None) -> None:
    """Background worker: run S2's pipeline, persist ready/error to the DB.

    Tries the course's video links in order (``link`` then ``fallbacks``)
    and caches the generated MCQs in ``quiz_bank`` per (course, video), so a
    course's video is only ever downloaded / transcribed / sent to the LLM once.
    """
    links = [link] + [l for l in (fallbacks or []) if l != link]
    last_error = "Quiz generation failed."
    if course_id:  # any video of this course already turned into MCQs?
        for video in links:
            cached = db.get_bank(course_id, video)
            if cached:
                live = _live_questions(json.loads(cached["questions"]))
                if not live:
                    db.set_attempt_error(attempt_id, "All questions for this course were rejected by the "
                                                     "trainer. Please try again after they regenerate it.")
                    return
                db.set_attempt_ready(attempt_id, _shuffled(live))
                db.set_attempt_bank(attempt_id, cached["id"])
                return
    for video in links:
        questions, error = _generate_questions(video, course_title)
        if error:
            last_error = error
            continue
        if course_id:
            db.set_attempt_bank(attempt_id, db.save_bank(
                course_id, video, questions, demo=os.environ.get("VIVARAN_FAKE_QUIZ") == "1"))
        db.set_attempt_ready(attempt_id, questions)
        return
    db.set_attempt_error(attempt_id, last_error)


def _generate_questions(video: str, course_title: str) -> tuple[list[dict], str | None]:
    """Run System 2 on one video -> (questions, error message or None)."""
    if os.environ.get("VIVARAN_FAKE_QUIZ") == "1":
        # Offline demo/test hook: skip network + LLM and serve canned MCQs.
        return _canned_quiz(), None
    try:
        questions, _transcript = s2_bridge.generate_quiz(
            video, topic=course_title, num_questions=settings.QUIZ_QUESTION_COUNT
        )
    except Exception as error:  # noqa: BLE001 - reported to the user in plain words
        logger.exception("quiz generation failed (%s)", video)
        return [], _friendly_quiz_error(error)
    if not questions:
        return [], "No questions could be generated from this course's video."
    return questions, None


def _live_questions(questions: list[dict]) -> list[dict]:
    """Questions learners may see: everything except trainer-rejected ones
    (trainer edits are already applied in place)."""
    return [q for q in questions if (q.get("review") or {}).get("status") != "rejected"]


def _friendly_quiz_error(error: Exception) -> str:
    text = str(error) or error.__class__.__name__
    low = text.lower()
    if "unavailable" in low or "private" in low or "sign in" in low or "download" in low:
        return "The course video could not be downloaded (it may be private, removed or blocked)."
    if "whisper" in low or "transcri" in low or "audio" in low:
        return "The video's audio could not be transcribed."
    if "groq" in low or "api" in low or "rate" in low:
        return "The question generator (LLM) is unavailable right now. Please try again shortly."
    if "system 2" in low or "no module" in low:
        return "The MCQ generator (System 2) is not installed or configured on this server."
    return f"Quiz generation failed: {text[:200]}"


def _shuffled(questions: list[dict]) -> list[dict]:
    """Same cached questions, fresh order for a retake (answers match by text)."""
    out = []
    for q in random.sample(questions, len(questions)):
        q = dict(q)
        q["options"] = random.sample(list(q.get("options") or []), len(q.get("options") or []))
        out.append(q)
    for i, q in enumerate(out, start=1):
        q["id"] = i
    return out


def _canned_quiz() -> list[dict]:
    """Three static MCQs used when VIVARAN_FAKE_QUIZ=1 (offline demo/tests)."""
    return [
        {
            "id": 1,
            "question": "Which option best matches the lecture's main topic?",
            "options": ["Statistical methods", "Cooking recipes", "Vehicle repair", "Gardening"],
            "answer": "Statistical methods",
            "explanation": "Demo question - replace by running the real pipeline.",
            "verification_status": "verified",
        },
        {
            "id": 2,
            "question": "What is sampled in a sample survey?",
            "options": ["A subset of the population", "The entire population", "Only outliers", "Nothing"],
            "answer": "A subset of the population",
            "explanation": "A sample survey observes a subset and infers about the population.",
            "verification_status": "verified",
        },
        {
            "id": 3,
            "question": "Which is a measure of central tendency?",
            "options": ["Mean", "Range", "Variance", "Skewness"],
            "answer": "Mean",
            "explanation": "Mean, median and mode locate the centre of a distribution.",
            "verification_status": "verified",
        },
    ]


@app.route("/quiz/start", methods=["POST"])
@role_required("trainer", "admin")
def quiz_start():
    submission_id = int(request.form.get("submission_id") or 0)
    course_id = (request.form.get("course_id") or "").strip()
    submission = _owned_submission(submission_id)

    course = next(
        (c for c in submission.get("recommendations") or [] if c.get("course_id") == course_id),
        None,
    )
    if not course:
        abort(404)

    # Reuse an in-flight or ready-but-ungraded attempt for this course rather
    # than spawning a duplicate generation.
    for attempt in db.list_attempts_for_submission(submission_id):
        if attempt["course_id"] == course_id and attempt["status"] in ("generating", "ready"):
            return redirect(url_for("quiz_attempt", attempt_id=attempt["id"]))

    catalogue_course = courses.get(course_id)
    videos = list((catalogue_course or {}).get("videos") or [])
    try:
        link = videos[0] if videos else _random_dataset6_link()
    except RuntimeError as error:
        logger.error("Dataset-6 read failed: %s", error)
        return "Dataset-6 could not be read by the server.", 500

    course_title = course.get("course_title") or course_id
    attempt_id = db.create_attempt(
        _require_user(), submission_id, course_id, course_title, source_link=link
    )
    thread = threading.Thread(
        target=_run_quiz_job,
        args=(attempt_id, course_title, link, course_id, videos[1:]),
        name=f"quiz-{attempt_id}",
        daemon=True,
    )
    thread.start()
    return redirect(url_for("quiz_attempt", attempt_id=attempt_id))


@app.route("/quiz/<int:attempt_id>", methods=["GET", "POST"])
@login_required
def quiz_attempt(attempt_id: int):
    attempt = _owned_attempt(attempt_id)

    if attempt["status"] == "graded":
        return redirect(url_for("quiz_results", attempt_id=attempt_id))

    # Generation still running (or failed) - show the status/poll view.
    if attempt["status"] != "ready":
        return render_template("quiz_status.html", attempt=attempt)

    if request.method == "POST":
        try:
            quiz = json.loads(attempt["quiz"]) if attempt.get("quiz") else []
        except json.JSONDecodeError:
            abort(500)
        answers = {}
        for q in quiz:
            value = request.form.get(f"q_{q['id']}")
            if value:
                answers[str(q["id"])] = value
        result = db.grade_attempt(attempt_id, answers, quiz)
        _after_grading(attempt, result)
        return redirect(url_for("quiz_results", attempt_id=attempt_id))

    # Render only what the quiz-taker may see (correct answers stay hidden).
    try:
        quiz = json.loads(attempt["quiz"]) if attempt.get("quiz") else []
    except json.JSONDecodeError:
        abort(500)
    questions = [
        {"id": q.get("id"), "question": q.get("question", ""), "options": q.get("options", [])}
        for q in quiz
    ]
    return render_template("quiz.html", attempt=attempt, questions=questions)


@app.route("/quiz/<int:attempt_id>/status.json")
@login_required
def quiz_status_json(attempt_id: int):
    attempt = _owned_attempt(attempt_id)
    return jsonify({"status": attempt["status"], "error": attempt.get("error")})


@app.route("/quiz/<int:attempt_id>/results")
@login_required
def quiz_results(attempt_id: int):
    attempt = _owned_attempt(attempt_id)
    if attempt["status"] != "graded":
        return redirect(url_for("quiz_attempt", attempt_id=attempt_id))
    review = db.graded_review(attempt_id) or {
        "score": 0, "total": 0, "accuracy": 0.0, "details": []
    }
    is_learner = session.get("role") == "learner"
    passed = review["accuracy"] >= settings.PASS_THRESHOLD
    return render_template(
        "quiz_results.html",
        attempt=attempt,
        review=review,
        passed=passed,
        threshold=settings.PASS_THRESHOLD,
        is_learner=is_learner,
        enrollment=db.get_enrollment(attempt["user_id"], attempt["course_id"]) if is_learner else None,
        notice=None if is_learner else settings.ONE_ATTEMPT_NOTICE,
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@app.route("/profile")
@login_required
def profile():
    if session.get("role") == "learner":
        return redirect(url_for("learner_dashboard"))
    try:
        skill_names = {
            s["skill_id"]: s.get("skill_name", s["skill_id"])
            for s in s1_client.meta_skills()
        }
    except (s1_client.S1Unavailable, s1_client.S1Error):
        skill_names = {}

    submissions = []
    for submission in db.list_submissions(_require_user()):
        submissions.append({"submission": submission, "courses": _course_rows(submission)})

    return render_template(
        "profile.html",
        submissions=submissions,
        skill_names=skill_names,
    )


# ---------------------------------------------------------------------------
# Learner platform: roadmap, progress, grading hook
# ---------------------------------------------------------------------------

def _ensure_roadmap(user_id: int, rebuild: bool = False) -> dict | None:
    """The learner's current roadmap, stored as a ``submissions`` row (the
    same table the original intake flow uses, so quiz attempts keep their
    submission link). Rebuilt when asked, when missing, or once every course
    on it is completed - completed courses are never recommended again."""
    profile = db.get_learner_profile(user_id)
    if not profile:
        return None
    current = db.latest_submission(user_id)
    enrollments = db.list_enrollments(user_id)
    done = {e["course_id"] for e in enrollments if e["status"] == "completed"}
    if current and not rebuild:
        ids = [r.get("course_id") for r in current.get("recommendations") or []]
        if ids and not all(i in done for i in ids):
            return current
    roadmap = recommender.build_roadmap(profile, enrollments, db.get_course_overrides(user_id))
    user = db.get_user_by_id(user_id) or {}
    submission_id = db.create_submission(user_id, {
        "employee_id": profile.get("s1_employee_id") or f"L{user_id:04d}",
        "name": user.get("username", ""),
        "role_id": profile["role_id"],
        "designation": profile["designation"],
        "department": profile["department"],
        "current_assignment": profile["area_of_experience"],
        "previous_trainings": sorted(done),
        "self_rated_skills": {},
        "quiz_verified_skills": recommender.verified_skills(
            [e for e in enrollments if e["status"] == "completed"]),
        "recommendations": recommender.to_submission_recs(roadmap),
    })
    return db.get_submission(submission_id)


def _course_state(user_id: int, course_id: str) -> dict:
    enrollment = db.get_enrollment(user_id, course_id)
    attempts = db.list_attempts_for_user_course(user_id, course_id)
    latest = attempts[-1] if attempts else None
    graded = [a for a in attempts if a["status"] == "graded"]
    if enrollment and enrollment["status"] == "completed":
        status = "completed"
    elif enrollment:
        status = "in_progress"
    else:
        status = "not_started"
    return {"enrollment": enrollment, "attempts": attempts, "latest": latest,
            "graded": graded, "status": status,
            "progress": (enrollment or {}).get("progress", 0)}


def _after_grading(attempt: dict, result: dict) -> None:
    """Quiz -> progress: store pass/fail, update the enrollment, and (on a
    pass) tell System 1 which skills are now quiz-verified."""
    passed = result["accuracy"] >= settings.PASS_THRESHOLD
    db.set_attempt_passed(attempt["id"], passed)
    user = db.get_user_by_id(attempt["user_id"]) or {}
    if user.get("role") != "learner":
        return
    course = courses.get(attempt["course_id"]) or {}
    db.start_enrollment(user["id"], attempt["course_id"],
                        course.get("title") or attempt["course_title"], settings.PROGRESS_STARTED)
    enrollment = db.get_enrollment(user["id"], attempt["course_id"]) or {}
    best = enrollment.get("best_percentage")
    extra = {}
    if best is None or result["accuracy"] >= best:
        extra = {"best_score": result["score"], "best_total": result["total"],
                 "best_percentage": result["accuracy"]}
    if passed:
        db.bump_progress(user["id"], attempt["course_id"], settings.PROGRESS_COMPLETED,
                         status="completed", completed_at=db._now(), **extra)
        profile = db.get_learner_profile(user["id"]) or {}
        verified = {s: course["target_level"] for s in course.get("skills", [])
                    if s in courses.skills()}
        if profile.get("s1_employee_id") and verified:
            try:
                s1_client.update_skills(profile["s1_employee_id"], verified)
            except (s1_client.S1Unavailable, s1_client.S1Error) as err:
                logger.warning("System 1 skill update failed: %s", err)
    else:
        db.bump_progress(user["id"], attempt["course_id"], settings.PROGRESS_QUIZ_TAKEN, **extra)


def _dashboard_data(user_id: int) -> dict:
    profile = db.get_learner_profile(user_id)
    submission = _ensure_roadmap(user_id)
    enrollments = db.list_enrollments(user_id)
    by_course = {e["course_id"]: e for e in enrollments}
    roadmap = []
    for rec in (submission or {}).get("recommendations") or []:
        e = by_course.get(rec["course_id"])
        roadmap.append({**rec,
                        "status": e["status"] if e else "not_started",
                        "progress": e["progress"] if e else 0})
    completed = [e for e in enrollments if e["status"] == "completed"]
    in_progress = [e for e in enrollments if e["status"] != "completed"]
    recommended = [r for r in roadmap if r["status"] == "not_started"]
    results = [a for a in db.list_attempts_for_user(user_id) if a["status"] == "graded"]
    for a in results:
        a["passed_bool"] = (a.get("accuracy") or 0) >= settings.PASS_THRESHOLD
    return {"profile": profile, "submission": submission, "roadmap": roadmap,
            "completed": completed, "in_progress": in_progress,
            "recommended": recommended, "results": list(reversed(results))}


def _learner_page(user_id: int) -> dict:
    """Everything the learner pages share (profile, roadmap, courses, trainer)."""
    data = _dashboard_data(user_id)
    profile = data["profile"] or {}
    trainer = db.get_user_by_id(profile["trainer_id"]) if profile.get("trainer_id") else None
    tprofile = db.get_trainer_profile(trainer["id"]) if trainer else None
    roadmap = data["roadmap"]
    data.update({
        "user": db.get_user_by_id(user_id),
        "my_trainer": (trainer or {}).get("username"),
        "trainer_email": (trainer or {}).get("email"),
        "trainer_specs": (tprofile or {}).get("specialisations", []),
        "overall": round(sum(r["progress"] for r in roadmap) / len(roadmap)) if roadmap else 0,
        "threshold": settings.PASS_THRESHOLD,
        "skill_name": courses.skill_name,
    })
    return data


def _learner_guard():
    if not db.get_learner_profile(_require_user()):
        return redirect(url_for("onboarding"))
    return None


@app.route("/dashboard")
@role_required("learner")
def learner_dashboard():
    guard = _learner_guard()
    if guard:
        return guard
    if request.args.get("gaps") == "1":
        return redirect(url_for("learner_skills"))
    d = _learner_page(_require_user())
    cont = d["in_progress"][0] if d["in_progress"] else None  # most recently updated first
    up_next = [r for r in d["roadmap"] if r["status"] != "completed"][:3]
    decks = _flashcard_decks(_require_user())
    gaps, _engine = recommender.skill_gaps(d["profile"], d["completed"])
    passed = sum(1 for a in d["results"] if a["passed_bool"])
    avg = round(sum(a["accuracy"] or 0 for a in d["results"]) / len(d["results"])) if d["results"] else None
    return render_template("dashboard.html", cont=cont, up_next=up_next, decks=decks, gaps=gaps[:5],
                           prog=progress.brief(_require_user()),
                           gap_count=len(gaps), passed=passed, avg=avg, active="dashboard", **d)


@app.route("/learn/roadmap")
@role_required("learner")
def learner_roadmap():
    return _learner_guard() or render_template("learner_roadmap.html", active="roadmap",
                                               **_learner_page(_require_user()))


@app.route("/learn/courses")
@role_required("learner")
def learner_courses():
    tab = request.args.get("tab", "in_progress")
    return _learner_guard() or render_template("learner_courses.html", active="courses", tab=tab,
                                               **_learner_page(_require_user()))


@app.route("/learn/recommended")
@role_required("learner")
def learner_recommended():
    return _learner_guard() or render_template("learner_recommended.html", active="recommended",
                                               catalogue_source=courses.source_label(),
                                               **_learner_page(_require_user()))


@app.route("/learn/flashcards")
@role_required("learner")
def learner_flashcards():
    return _learner_guard() or render_template("learner_flashcards.html", active="flashcards",
                                               decks=_flashcard_decks(_require_user()),
                                               **_learner_page(_require_user()))


@app.route("/learn/results")
@role_required("learner")
def learner_results():
    d = _learner_page(_require_user())
    passed = sum(1 for a in d["results"] if a["passed_bool"])
    return _learner_guard() or render_template("learner_results.html", active="results", passed=passed, **d)


@app.route("/learn/skills")
@role_required("learner")
def learner_skills():
    guard = _learner_guard()
    if guard:
        return guard
    d = _learner_page(_require_user())
    gaps, engine = recommender.skill_gaps(d["profile"], d["completed"])
    verified = recommender.verified_skills(d["completed"])
    required = [r for r in recommender._dataset2() if r["role_id"] == d["profile"]["role_id"]]
    skills = []
    for r in sorted(required, key=lambda r: -int(r["priority_weight"])):
        cur = verified.get(r["skill_id"], 0)
        skills.append({"skill_id": r["skill_id"], "skill_name": courses.skill_name(r["skill_id"]),
                       "current": cur, "required": int(r["required_level"]),
                       "priority": int(r["priority_weight"]), "met": cur >= int(r["required_level"])})
    return render_template("learner_skills.html", active="skills", skills=skills, gaps=gaps, engine=engine, **d)


@app.route("/learn/progress")
@role_required("learner")
def learner_progress():
    guard = _learner_guard()
    if guard:
        return guard
    return render_template("learner_progress.html", active="progress", p=progress.summary(_require_user()),
                           xp_rules=progress.XP_RULES, levels=progress.LEVELS, **_learner_page(_require_user()))


@app.route("/learn/profile")
@role_required("learner")
def learner_profile():
    return _learner_guard() or render_template("learner_profile.html", active="profile",
                                               **_learner_page(_require_user()))


@app.route("/dashboard/refresh", methods=["POST"])
@role_required("learner")
def refresh_roadmap():
    _ensure_roadmap(_require_user(), rebuild=True)
    back = request.form.get("next") or ""
    return redirect(back if back.startswith("/learn/") or back == "/dashboard" else url_for("learner_dashboard"))


@app.route("/onboarding", methods=["GET", "POST"])
@role_required("learner")
def onboarding():
    """Legacy accounts (created before profiles existed) complete their profile."""
    user = db.get_user_by_id(_require_user())
    error = None
    if request.method == "POST":
        role_meta = recommender.role((request.form.get("role_id") or "").strip())
        area = (request.form.get("area_of_experience") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        if not user.get("email"):
            error = _validate_gov_email(email)
            if not error and db.get_user_by_email(email):
                error = "An account with this email already exists."
        if not error and not role_meta:
            error = "Please choose your designation."
        if not error and area not in courses.AREAS_OF_EXPERIENCE:
            error = "Please choose your area of experience."
        if not error:
            if not user.get("email"):
                db.set_user_email(user["id"], email)
            db.upsert_learner_profile(user["id"], role_meta["role_id"], role_meta["designation"],
                                      role_meta["department"], area)
            _register_in_system1(user["id"], user["username"])
            _assign_trainer_for(user["id"])
            _ensure_roadmap(user["id"], rebuild=True)
            return redirect(url_for("learner_dashboard"))
    return render_template("onboarding.html", user=user, roles=_role_options(),
                           areas=list(courses.AREAS_OF_EXPERIENCE), error=error,
                           domain=settings.GOV_EMAIL_DOMAIN)


# ---------------------------------------------------------------------------
# Course page (+ System 2 quiz for the course's own video)
# ---------------------------------------------------------------------------

def _course_or_404(course_id: str) -> dict:
    course = courses.get(course_id)
    if not course:
        abort(404)
    return course


CATALOGUE_PAGE = 20


@app.route("/courses")
@login_required
def course_list():
    """Catalogue: free-text search + skill filter (any of the selected skills),
    20 courses per chunk; the page loads further chunks as you scroll."""
    raw_q = (request.args.get("q") or "").strip()
    q = raw_q.lower()
    picked = [s_ for s_ in request.args.getlist("skill") if s_ in courses.skills()]
    level = request.args.get("level") or ""
    page = max(1, int(request.args.get("page", "1") or 1)) if (request.args.get("page") or "1").isdigit() else 1
    unpub = set(db.unpublished_courses())
    base = [c for c in courses.all_courses() if c["course_id"] not in unpub or session.get("role") == "admin"]
    counts = {sid: 0 for sid in courses.skills()}
    items = []
    for c in base:
        if q and not (q in c["title"].lower() or q in c["description"].lower() or q in (c["category"] or "").lower()
                      or q in c["course_id"].lower() or any(q in n.lower() for n in c.get("skill_names") or [])):
            continue
        if level and str(c["target_level"]) != level:
            continue
        for sid in c["skills"]:
            if sid in counts:
                counts[sid] += 1  # counts reflect the search, before the skill filter
        if picked and not set(picked) & set(c["skills"]):
            continue
        items.append(c)
    # Most relevant first: courses matching more of the picked skills, then title.
    items.sort(key=lambda c: (-len(set(picked) & set(c["skills"])), c["title"].lower()))
    total = len(items)
    chunk = items[(page - 1) * CATALOGUE_PAGE: page * CATALOGUE_PAGE]
    has_more = page * CATALOGUE_PAGE < total
    status = {}
    if session.get("role") == "learner":
        for e in db.list_enrollments(_require_user()):
            status[e["course_id"]] = e["status"]
        for r in (db.latest_submission(_require_user()) or {}).get("recommendations") or []:
            status.setdefault(r["course_id"], "on_path")
    ctx = dict(items=chunk, total=total, q=raw_q, picked=picked, level=level, page=page, has_more=has_more,
               status=status, source=courses.source_label())
    if request.args.get("partial") == "1":  # infinite-scroll chunk
        return render_template("_course_cards.html", **ctx)
    groups = []
    for g in _skill_groups():
        groups.append({"category": g["category"],
                       "skills": [{**s_, "n": counts.get(s_["skill_id"], 0)} for s_ in g["skills"]]})
    return render_template("courses.html", groups=groups, **ctx)


@app.route("/courses/<course_id>")
@login_required
def course_page(course_id: str):
    course = courses.get(course_id)
    if not course and session.get("role") in ("trainer", "admin"):
        course = courses.get_any(course_id)  # staff may preview uploads awaiting approval
    if not course:
        abort(404)
    state = _course_state(_require_user(), course_id) if session.get("role") == "learner" else None
    submission = db.latest_submission(_require_user())
    rec = next((r for r in (submission or {}).get("recommendations") or []
                if r.get("course_id") == course_id), None)
    videos = [{"url": v, "embed": courses.youtube_embed(v)} for v in course["videos"]]
    deck = _flashcard_deck(_require_user(), course_id) if state else []
    return render_template("course.html", course=course, state=state, rec=rec, videos=videos,
                           deck_size=len(deck),
                           deck_mastered=sum(1 for c in deck if c["status"] == "know"),
                           threshold=settings.PASS_THRESHOLD,
                           is_learner=session.get("role") == "learner")


@app.route("/courses/<course_id>/start", methods=["POST"])
@role_required("learner")
def course_start(course_id: str):
    course = _course_or_404(course_id)
    db.start_enrollment(_require_user(), course_id, course["title"], settings.PROGRESS_STARTED)
    return redirect(url_for("course_page", course_id=course_id) + "#video")


@app.route("/courses/<course_id>/watched", methods=["POST"])
@role_required("learner")
def course_watched(course_id: str):
    course = _course_or_404(course_id)
    db.start_enrollment(_require_user(), course_id, course["title"], settings.PROGRESS_STARTED)
    already = (db.get_enrollment(_require_user(), course_id) or {}).get("video_watched")
    db.bump_progress(_require_user(), course_id, settings.PROGRESS_VIDEO_WATCHED, video_watched=1)
    if not already:
        db.log_activity(_require_user(), "video", course_id)  # counts toward the streak
    return redirect(url_for("course_page", course_id=course_id) + "#quiz")


def _start_course_quiz(user_id: int, course: dict) -> tuple[int | None, str | None]:
    """(attempt_id, error). Reuses an in-flight/ready attempt; cached MCQs are
    served instantly; otherwise System 2 runs in the background."""
    if not course["videos"]:
        return None, "This course has no video, so a quiz cannot be generated for it."
    for attempt in db.list_attempts_for_user_course(user_id, course["course_id"]):
        if attempt["status"] in ("generating", "ready"):
            return attempt["id"], None
    submission = _ensure_roadmap(user_id)
    if not submission:
        return None, "Please complete your profile first."
    db.start_enrollment(user_id, course["course_id"], course["title"], settings.PROGRESS_STARTED)
    link = course["videos"][0]
    attempt_id = db.create_attempt(user_id, submission["id"], course["course_id"],
                                   course["title"], source_link=link)
    threading.Thread(
        target=_run_quiz_job,
        args=(attempt_id, course["title"], link, course["course_id"], course["videos"][1:]),
        name=f"quiz-{attempt_id}", daemon=True,
    ).start()
    return attempt_id, None


@app.route("/courses/<course_id>/quiz", methods=["POST"])
@role_required("learner")
def course_quiz(course_id: str):
    course = _course_or_404(course_id)
    attempt_id, error = _start_course_quiz(_require_user(), course)
    if error:
        return render_template("error.html", message=error,
                               back=url_for("course_page", course_id=course_id)), 400
    return redirect(url_for("quiz_attempt", attempt_id=attempt_id))


# ---------------------------------------------------------------------------
# Flashcards (built from the learner's own graded course quizzes)
# ---------------------------------------------------------------------------

FLASH_STATUSES = ("know", "review")


def _card_key(question: str) -> str:
    norm = " ".join((question or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _bank_review_map(course_id: str) -> dict[str, dict]:
    """card key of each ORIGINAL question text -> its current reviewed version."""
    out: dict[str, dict] = {}
    for bank in db.list_banks_for_course(course_id):
        for q in json.loads(bank["questions"] or "[]"):
            original = (q.get("original") or {}).get("question") or q.get("question", "")
            out[_card_key(original)] = q
            out.setdefault(_card_key(q.get("question", "")), q)
    return out


def _flashcard_deck(user_id: int, course_id: str) -> list[dict]:
    """Every distinct question the learner has seen in a GRADED quiz for this
    course, as flashcards. Empty list = deck still locked (no quiz taken).

    Order: marked "review" -> missed in their latest attempt -> not yet
    marked -> marked "know". No new generation: reuses stored MCQs."""
    graded = [a for a in db.list_attempts_for_user_course(user_id, course_id)
              if a["status"] == "graded" and a.get("quiz")]
    reviewed = _bank_review_map(course_id)
    cards: dict[str, dict] = {}
    for attempt in graded:  # oldest -> newest, so the latest answer wins
        try:
            quiz = json.loads(attempt["quiz"])
            answers = json.loads(attempt["answers"]) if attempt.get("answers") else {}
        except json.JSONDecodeError:
            continue
        for q in quiz:
            key = _card_key(q.get("question", ""))
            if not key or not q.get("question"):
                continue
            selected = answers.get(str(q.get("id")), "")
            correct = str(selected).lower() == str(q.get("answer", "")).lower() and bool(selected)
            current = reviewed.get(key)
            if current is not None:
                if (current.get("review") or {}).get("status") == "rejected":
                    continue  # trainer rejected it: drop from flashcards
                if current.get("original"):  # trainer edited it: show the corrected card
                    q = {**q, "question": current["question"], "answer": current["answer"],
                         "explanation": current.get("explanation", "")}
            cards[key] = {
                "key": key,
                "question": q.get("question", ""),
                "answer": q.get("answer", ""),
                "explanation": q.get("explanation", ""),
                "missed": not correct,
            }
    marks = db.get_flashcard_status(user_id, course_id)
    for card in cards.values():
        card["status"] = marks.get((course_id, card["key"]), "")

    def rank(card: dict) -> int:
        if card["status"] == "review":
            return 0
        if card["status"] == "know":
            return 3
        return 1 if card["missed"] else 2
    return sorted(cards.values(), key=rank)


def _flashcard_decks(user_id: int) -> list[dict]:
    """Dashboard tiles: one per course with at least one graded quiz."""
    course_ids: list[str] = []
    for a in db.list_attempts_for_user(user_id):
        if a["status"] == "graded" and a["course_id"] not in course_ids:
            course_ids.append(a["course_id"])
    decks = []
    for cid in course_ids:
        cards = _flashcard_deck(user_id, cid)
        if not cards:
            continue
        course = courses.get(cid) or {}
        title = course.get("title") or next(
            (a["course_title"] for a in db.list_attempts_for_user_course(user_id, cid)), cid)
        decks.append({
            "course_id": cid, "title": title, "total": len(cards),
            "mastered": sum(1 for c in cards if c["status"] == "know"),
            "review": sum(1 for c in cards if c["status"] == "review"),
        })
    return decks


@app.route("/courses/<course_id>/flashcards")
@role_required("learner")
def course_flashcards(course_id: str):
    cards = _flashcard_deck(_require_user(), course_id)
    course = courses.get(course_id)
    title = (course or {}).get("title") or next(
        (a["course_title"] for a in db.list_attempts_for_user_course(_require_user(), course_id)), None)
    if not title:
        abort(404)
    return render_template("flashcards.html", course_id=course_id, title=title, cards=cards,
                           mastered=sum(1 for c in cards if c["status"] == "know"))


@app.route("/api/flashcards/<course_id>")
@role_required("learner")
def api_flashcards(course_id: str):
    cards = _flashcard_deck(_require_user(), course_id)
    if not cards:
        return jsonify({"ok": False, "locked": True,
                        "error": "Take this course's quiz to unlock its flashcards."}), 403
    return jsonify({"ok": True, "cards": cards,
                    "mastered": sum(1 for c in cards if c["status"] == "know")})


@app.route("/api/flashcards/<course_id>/<card_key>", methods=["POST"])
@role_required("learner")
def api_flashcard_mark(course_id: str, card_key: str):
    status = ((_json_body() or {}).get("status") or "").strip()
    if status not in FLASH_STATUSES:
        return jsonify({"ok": False, "error": "status must be 'know' or 'review'"}), 400
    cards = _flashcard_deck(_require_user(), course_id)
    if not any(c["key"] == card_key for c in cards):
        return jsonify({"ok": False, "error": "Card not found in your deck."}), 404
    db.set_flashcard_status(_require_user(), course_id, card_key, status)
    cards = _flashcard_deck(_require_user(), course_id)
    return jsonify({"ok": True, "mastered": sum(1 for c in cards if c["status"] == "know"),
                    "total": len(cards)})


# ---------------------------------------------------------------------------
# Trainer dashboard
# ---------------------------------------------------------------------------

def _learner_rows() -> list[dict]:
    rows = []
    enrollments = db.list_enrollments()
    attempts = [a for a in db.list_all_attempts() if a["status"] == "graded"]
    for learner in db.list_learners_with_profiles():
        mine = [e for e in enrollments if e["user_id"] == learner["id"]]
        scores = [a["accuracy"] for a in attempts if a["user_id"] == learner["id"]]
        submission = db.latest_submission(learner["id"])
        rows.append({
            **learner,
            "completed": sum(1 for e in mine if e["status"] == "completed"),
            "in_progress": sum(1 for e in mine if e["status"] != "completed"),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "quizzes": len(scores),
            "recommended": len((submission or {}).get("recommendations") or []),
            "roadmap": [r.get("course_title") for r in (submission or {}).get("recommendations") or []],
        })
    return rows


def _course_rows_overview() -> list[dict]:
    """Courses that are in use (recommended / enrolled / quizzed)."""
    stats: dict[str, dict] = {}

    def row(cid: str, title: str) -> dict:
        return stats.setdefault(cid, {"course_id": cid, "title": title, "recommended": 0,
                                      "enrolled": 0, "completed": 0, "scores": []})
    for learner in db.list_learners_with_profiles():
        sub = db.latest_submission(learner["id"])
        for r in (sub or {}).get("recommendations") or []:
            row(r["course_id"], r.get("course_title", r["course_id"]))["recommended"] += 1
    for e in db.list_enrollments():
        r = row(e["course_id"], e["course_title"])
        r["enrolled"] += 1
        r["completed"] += 1 if e["status"] == "completed" else 0
    for a in db.list_all_attempts():
        if a["status"] == "graded":
            row(a["course_id"], a["course_title"])["scores"].append(a["accuracy"] or 0)
    cached = {b["course_id"] for b in db.list_bank()}
    out = []
    for r in stats.values():
        course = courses.get(r["course_id"]) or {}
        r["avg_score"] = round(sum(r["scores"]) / len(r["scores"]), 1) if r["scores"] else None
        r["attempts"] = len(r.pop("scores"))
        r["videos"] = len(course.get("videos") or [])
        r["quiz_cached"] = r["course_id"] in cached
        r["category"] = course.get("category", "")
        out.append(r)
    out.sort(key=lambda r: (-r["recommended"], -r["enrolled"], r["title"]))
    return out


# ---------------------------------------------------------------------------
# Trainer <-> learner assignment
# ---------------------------------------------------------------------------

TRAINER_BATCH_SIZE = int(os.environ.get("VIVARAN_TRAINER_BATCH", "5"))


def _assign_trainer_for(learner_id: int) -> int | None:
    """Give a learner one trainer: prefer matching specialisations, then the
    lightest load, then random among ties. None if no trainer exists yet."""
    profile = db.get_learner_profile(learner_id)
    if not profile or profile.get("trainer_id"):
        return (profile or {}).get("trainer_id")
    trainers = [t for t in db.list_trainers_with_profiles() if t["has_profile"] and t.get("active", 1)]
    if not trainers:
        return None
    scored = [(recommender.match_score(t["specialisations"], profile), t) for t in trainers]
    best_match = max(s for s, _ in scored)
    pool = [t for s, t in scored if s == best_match]
    lightest = min(t["students"] for t in pool)
    choice = random.choice([t for t in pool if t["students"] == lightest])
    db.set_learner_trainer(learner_id, choice["id"])
    return choice["id"]


def _claim_batch(trainer_id: int) -> int:
    """A new trainer takes up to TRAINER_BATCH_SIZE random unassigned learners
    (learners whose field/role matches the trainer's skills are picked first)."""
    tp = db.get_trainer_profile(trainer_id)
    if not tp:
        return 0
    pool = db.list_unassigned_learners()
    random.shuffle(pool)
    pool.sort(key=lambda l: -recommender.match_score(tp["specialisations"], l))
    for learner in pool[:TRAINER_BATCH_SIZE]:
        db.set_learner_trainer(learner["id"], trainer_id)
    return min(len(pool), TRAINER_BATCH_SIZE)


def _skill_groups() -> list[dict]:
    """The 16 System-1 skills grouped by category (specialisation picker)."""
    groups: dict[str, list[dict]] = {}
    for sid, skill in sorted(courses.skills().items()):
        groups.setdefault(skill.get("category") or "Other", []).append(
            {"skill_id": sid, "skill_name": skill.get("skill_name", sid)})
    return [{"category": k, "skills": v} for k, v in groups.items()]


def _clean_specialisations(values) -> list[str]:
    valid = courses.skills()
    return [v for v in dict.fromkeys(values or []) if v in valid]


# ---------------------------------------------------------------------------
# Trainer pages (sidebar layout: templates/trainer_base.html)
# ---------------------------------------------------------------------------

def trainer_profile_required(view):
    """Trainer routes need the trainer's specialisations first."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not db.get_trainer_profile(_require_user()):
            if _wants_json():
                return jsonify({"ok": False, "error": "Complete your trainer profile first."}), 409
            return redirect(url_for("trainer_onboarding"))
        return view(*args, **kwargs)
    return wrapped


def _my_student(learner_id: int) -> dict:
    """The learner, only if assigned to the logged-in trainer (else 404)."""
    student = next((s for s in db.list_trainer_students(_require_user()) if s["id"] == learner_id), None)
    if not student:
        abort(404)
    return student


def _student_detail(learner_id: int) -> dict:
    """Everything the trainer sees for one student."""
    submission = _ensure_roadmap(learner_id)
    enrollments = {e["course_id"]: e for e in db.list_enrollments(learner_id)}
    attempts = [a for a in db.list_attempts_for_user(learner_id) if a["status"] == "graded"]
    by_course: dict[str, list[dict]] = {}
    for a in attempts:
        by_course.setdefault(a["course_id"], []).append(a)
    rows, seen = [], set()

    def row(cid: str, title: str, rec: dict | None) -> dict:
        e = enrollments.get(cid)
        tries = by_course.get(cid, [])
        best = max(tries, key=lambda a: a["accuracy"] or 0) if tries else None
        return {
            "course_id": cid, "title": title, "on_path": rec is not None,
            "step": (rec or {}).get("step"), "added_by_trainer": bool((rec or {}).get("added_by_trainer")),
            "status": e["status"] if e else "not_started", "progress": e["progress"] if e else 0,
            "best": best, "attempts": len(tries),
        }
    for rec in (submission or {}).get("recommendations") or []:
        rows.append(row(rec["course_id"], rec.get("course_title", rec["course_id"]), rec))
        seen.add(rec["course_id"])
    for cid, e in enrollments.items():  # started/completed outside the current path
        if cid not in seen:
            rows.append(row(cid, e["course_title"], None))
    for a in attempts:
        a["passed_bool"] = (a.get("accuracy") or 0) >= settings.PASS_THRESHOLD
    done = [r for r in rows if r["status"] == "completed"]
    avg = [a["accuracy"] or 0 for a in attempts]
    return {
        "rows": rows, "attempts": list(reversed(attempts)),
        "completed": len(done),
        "overall": round(sum(r["progress"] for r in rows) / len(rows)) if rows else 0,
        "avg_score": round(sum(avg) / len(avg), 1) if avg else None,
        "submission": submission,
    }


def _students_overview(trainer_id: int) -> list[dict]:
    out = []
    for s in db.list_trainer_students(trainer_id):
        d = _student_detail(s["id"])
        out.append({**s, "prog": progress.brief(s["id"]), "courses": len(d["rows"]), "completed": d["completed"],
                    "overall": d["overall"], "avg_score": d["avg_score"],
                    "quizzes": len(d["attempts"])})
    return out


def _trainer_ctx(active: str) -> dict:
    user = db.get_user_by_id(_require_user())
    return {"trainer": user, "tprofile": db.get_trainer_profile(user["id"]), "active": active,
            "skill_name": courses.skill_name, "pending_reviews": _pending_reviews(user["id"]),
            "pending_approvals": len(_approval_queue(user["id"]))}


@app.route("/trainer/onboarding", methods=["GET", "POST"])
@role_required("trainer")
def trainer_onboarding():
    error = None
    if request.method == "POST":
        specs = _clean_specialisations(request.form.getlist("specialisations"))
        if not specs:
            error = "Pick at least one skill you specialise in."
        else:
            db.upsert_trainer_profile(_require_user(), specs)
            _claim_batch(_require_user())
            return redirect(url_for("trainer_dashboard"))
    return render_template("trainer_onboarding.html", groups=_skill_groups(), error=error,
                           selected=request.form.getlist("specialisations"))


@app.route("/trainer")
@role_required("trainer")
@trainer_profile_required
def trainer_dashboard():
    me = _require_user()
    students = _students_overview(me)
    quizzes = sum(s["quizzes"] for s in students)
    scored = [s["avg_score"] for s in students if s["avg_score"] is not None]
    stats = {
        "students": len(students),
        "avg_progress": round(sum(s["overall"] for s in students) / len(students)) if students else 0,
        "quizzes": quizzes,
        "avg_score": round(sum(scored) / len(scored)) if scored else None,
        "completed": sum(s["completed"] for s in students),
    }
    my_ids = {s["id"] for s in students}
    names = {s["id"]: s["username"] for s in students}
    graded = [a for a in db.list_all_attempts() if a["status"] == "graded" and a["user_id"] in my_ids]
    recent = []
    for a in graded[:6]:  # list_all_attempts is newest first
        recent.append({**a, "passed_bool": (a.get("accuracy") or 0) >= settings.PASS_THRESHOLD})
    attention = []
    for s_ in students:
        mine = [a for a in graded if a["user_id"] == s_["id"]]
        if mine and (mine[0].get("accuracy") or 0) < settings.PASS_THRESHOLD:
            attention.append({"id": s_["id"], "name": s_["username"],
                              "why": f"Failed the {mine[0]['course_title']} quiz ({mine[0]['accuracy']:.0f}%)"})
        elif s_["overall"] == 0:
            attention.append({"id": s_["id"], "name": s_["username"], "why": "Hasn't started any course yet"})
    assigned = _assigned_course_stats(me)
    uploads = []
    counts = {c["course_id"]: len(c["students"]) for c in assigned}
    for up in db.list_trainer_courses(me):
        qs = [q for b in db.list_banks_for_course(up["course_id"]) for q in json.loads(b["questions"] or "[]")]
        uploads.append({**up, "assigned": counts.get(up["course_id"], 0), "questions": len(qs),
                        "approval": up["status"],
                        "pending": sum(1 for q in qs if _q_status(q) == "pending")})
    approvals = _approval_queue(me)
    return render_template("trainer.html", students=students, stats=stats, recent=recent,
                           approvals=approvals[:4],
                           attention=attention[:6], top_courses=assigned[:5], uploads=uploads[:5],
                           uploads_total=len(uploads), names=names,
                           unassigned=len(db.list_unassigned_learners()), **_trainer_ctx("dashboard"))


@app.route("/trainer/claim", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_claim():
    n = _claim_batch(_require_user())
    flash_msg = f"{n} new student{'s' if n != 1 else ''} assigned to you." if n else "No unassigned learners right now."
    return redirect(url_for("trainer_dashboard", msg=flash_msg))


@app.route("/trainer/students/<int:learner_id>")
@role_required("trainer")
@trainer_profile_required
def trainer_student(learner_id: int):
    student = _my_student(learner_id)
    detail = _student_detail(learner_id)
    q = (request.args.get("q") or "").strip().lower()
    results = []
    if q:
        on = {r["course_id"] for r in detail["rows"] if r["on_path"] or r["status"] == "completed"}
        on |= set(db.unpublished_courses())
        for c in courses.all_courses():
            if c["course_id"] in on:
                continue
            if q in c["title"].lower() or q in c["course_id"].lower() or q in (c["category"] or "").lower() \
                    or any(q in n.lower() for n in c.get("skill_names") or []):
                results.append(c)
        results.sort(key=lambda c: (q not in c["title"].lower(), c.get("video_source") != "trainer", c["title"]))
        results = results[:25]
    return render_template("trainer_student.html", student=student, d=detail, q=request.args.get("q", ""),
                           prog=progress.summary(learner_id),
                           results=results, threshold=settings.PASS_THRESHOLD,
                           message=request.args.get("msg"), error=request.args.get("err"),
                           **_trainer_ctx("dashboard"))


def _path_edit(learner_id: int, course_id: str, action: str) -> str | None:
    """Add/remove a course on a student's current path. Returns an error or None."""
    course = courses.get(course_id)
    submission = _ensure_roadmap(learner_id)
    if not submission:
        return "This learner has not completed their profile yet."
    recs = list(submission.get("recommendations") or [])
    enrollment = db.get_enrollment(learner_id, course_id)
    if action == "add":
        if not course:
            return "Course not found."
        if enrollment and enrollment["status"] == "completed":
            return "This learner has already completed that course."
        if any(r["course_id"] == course_id for r in recs):
            return "That course is already on this learner's path."
        recs.append(recommender.course_to_rec(course, len(recs) + 1, "Added by your trainer", True))
    else:
        if enrollment and enrollment["status"] == "completed":
            return "Completed courses are part of the learner's history and can't be removed."
        if not any(r["course_id"] == course_id for r in recs):
            return "That course is not on this learner's path."
        recs = [r for r in recs if r["course_id"] != course_id]
        for i, r in enumerate(recs, start=1):
            r["step"] = i
    db.update_submission_recommendations(submission["id"], recs)
    db.set_course_override(learner_id, course_id, action, _require_user())
    learner = db.get_user_by_id(learner_id) or {}
    db.audit(_require_user(), "course_added_to_learner" if action == "add" else "course_removed_from_learner",
             learner.get("username", str(learner_id)), f"{course_id} {(course or {}).get('title', '')}".strip())
    return None


@app.route("/trainer/students/<int:learner_id>/courses", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_add_course(learner_id: int):
    _my_student(learner_id)
    cid = (request.form.get("course_id") or "").strip()
    err = _path_edit(learner_id, cid, "add")
    title = (courses.get(cid) or {}).get("title", cid)
    return redirect(url_for("trainer_student", learner_id=learner_id,
                            **({"err": err} if err else {"msg": f"Added “{title}”."})))


@app.route("/trainer/students/<int:learner_id>/courses/<course_id>/remove", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_remove_course(learner_id: int, course_id: str):
    _my_student(learner_id)
    err = _path_edit(learner_id, course_id, "remove")
    title = (courses.get(course_id) or {}).get("title", course_id)
    return redirect(url_for("trainer_student", learner_id=learner_id,
                            **({"err": err} if err else {"msg": f"Removed “{title}”."})))


def _assigned_course_stats(trainer_id: int) -> list[dict]:
    stats: dict[str, dict] = {}
    for s_ in db.list_trainer_students(trainer_id):
        for r in _student_detail(s_["id"])["rows"]:
            if not r["on_path"]:
                continue
            c = stats.setdefault(r["course_id"], {
                "course_id": r["course_id"], "title": r["title"], "students": [],
                "completed": 0, "in_progress": 0, "scores": [], "added": 0})
            c["students"].append(s_["username"])
            c["completed"] += r["status"] == "completed"
            c["in_progress"] += r["status"] == "in_progress"
            c["added"] += r["added_by_trainer"]
            if r["best"]:
                c["scores"].append(r["best"]["accuracy"] or 0)
    rows = []
    for c in stats.values():
        course = courses.get(c["course_id"]) or {}
        c["avg_score"] = round(sum(c["scores"]) / len(c["scores"]), 1) if c["scores"] else None
        c["category"] = course.get("category", "")
        c["uploaded"] = course.get("video_source") == "trainer"
        rows.append(c)
    rows.sort(key=lambda c: (-len(c["students"]), c["title"]))
    return rows


@app.route("/trainer/courses")
@role_required("trainer")
@trainer_profile_required
def trainer_assigned_courses():
    return render_template("trainer_courses.html", rows=_assigned_course_stats(_require_user()),
                           **_trainer_ctx("courses"))


@app.route("/trainer/uploads", methods=["GET", "POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_uploads():
    error = None
    form = request.form
    if request.method == "POST":
        data, error = _parse_course_form(form)
        if not error:
            db.create_trainer_course(_require_user(), data)
            db.audit(_require_user(), "course_uploaded", data["title"])
            who = "trainers in a similar field" if _eligible_reviewers_for(data["skills"], _require_user()) \
                else "the admin (no other trainer shares these skills yet)"
            return redirect(url_for("trainer_uploads",
                                    msg=f"“{data['title']}” submitted for approval by {who}. "
                                        "You can generate and verify its questions meanwhile."))
    mine = db.list_trainer_courses(_require_user())
    for c in mine:
        c["history"] = db.list_course_reviews(c["id"])
        c["needs_admin"] = c["status"] == "pending" and _needs_admin(c)
    return render_template("trainer_uploads.html", mine=mine, groups=_skill_groups(), error=error,
                           form=form if error else {}, selected=form.getlist("skills") if error else [],
                           message=request.args.get("msg"), **_trainer_ctx("uploads"))


def _parse_course_form(form) -> tuple[dict, str | None]:
    title = (form.get("title") or "").strip()
    videos = list(dict.fromkeys(re.findall(r"https?://[^\s,;|\"'<>]+", form.get("videos") or "")))
    skills = _clean_specialisations(form.getlist("skills"))
    level = form.get("target_level") or "1"
    duration = (form.get("duration_minutes") or "").strip()
    if len(title) < 3:
        return {}, "Please give the course a title."
    if not videos:
        return {}, "Add at least one video link (starting with http:// or https://)."
    if not skills:
        return {}, "Pick at least one skill this course teaches."
    if level not in ("1", "2", "3"):
        return {}, "Level must be 1, 2 or 3."
    if duration and not duration.isdigit():
        return {}, "Duration must be a whole number of minutes."
    return {
        "title": title,
        "description": (form.get("description") or "").strip(),
        "category": (form.get("category") or "").strip(),
        "designations": [d.strip() for d in re.split(r"[;,\n]", form.get("designations") or "") if d.strip()],
        "skills": skills, "videos": videos, "target_level": int(level),
        "duration_minutes": int(duration) if duration else None,
    }, None


def _my_upload(row_id: int) -> dict:
    row = db.get_trainer_course(row_id)
    if not row or row["trainer_id"] != _require_user():
        abort(404)
    return row


@app.route("/trainer/uploads/<int:row_id>/edit", methods=["GET", "POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_upload_edit(row_id: int):
    """Edit a pending or rejected upload; saving (re)submits it for approval."""
    row = _my_upload(row_id)
    if row["status"] == "approved":
        return redirect(url_for("trainer_uploads", msg="Approved courses are live and can't be edited."))
    error = None
    if request.method == "POST":
        data, error = _parse_course_form(request.form)
        if not error:
            db.update_trainer_course(row_id, data)
            if row["status"] == "rejected":
                db.review_trainer_course(row_id, "resubmitted", _require_user(), None)
                db.audit(_require_user(), "course_resubmitted", f"{row['course_id']} {data['title']}")
                msg = f"“{data['title']}” resubmitted for approval."
            else:
                msg = f"“{data['title']}” updated — still waiting for approval."
            return redirect(url_for("trainer_uploads", msg=msg))
    form = request.form if error else {
        "title": row["title"], "description": row["description"], "category": row["category"],
        "designations": ", ".join(row["designations"]), "videos": "\n".join(row["videos"]),
        "target_level": str(row["target_level"]), "duration_minutes": row["duration_minutes"] or "",
    }
    selected = request.form.getlist("skills") if error else row["skills"]
    return render_template("trainer_upload_edit.html", row=row, form=form, selected=selected, error=error,
                           groups=_skill_groups(), **_trainer_ctx("uploads"))


# ---------------------------------------------------------------------------
# Peer approval of uploaded courses
#   reviewers = other trainers sharing >= 1 skill with the course (open queue,
#   first decision wins); if nobody qualifies, the admin approves. Admin can
#   always override.
# ---------------------------------------------------------------------------

def _eligible_reviewers_for(skills: list[str], uploader_id: int) -> list[dict]:
    wanted = set(skills)
    return [t for t in db.list_trainers_with_profiles()
            if t["has_profile"] and t.get("active", 1) and t["id"] != uploader_id
            and wanted & set(t["specialisations"])]


def _approval_queue(trainer_id: int) -> list[dict]:
    tp = db.get_trainer_profile(trainer_id) or {"specialisations": []}
    mine = set(tp["specialisations"])
    out = []
    for row in db.list_trainer_courses(status="pending"):
        if row["trainer_id"] == trainer_id:
            continue
        shared = [s_ for s_ in row["skills"] if s_ in mine]
        if shared:
            out.append({**row, "shared": shared})
    return out


def _needs_admin(row: dict) -> bool:
    return not _eligible_reviewers_for(row["skills"], row["trainer_id"])


def _decide(row: dict, action: str, note: str) -> str | None:
    if row["status"] != "pending":
        return "This course has already been reviewed."
    if action not in ("approve", "reject"):
        return "Unknown action."
    if action == "reject" and len(note) < 5:
        return "Please give a reason when rejecting (at least a few words)."
    db.review_trainer_course(row["id"], "approved" if action == "approve" else "rejected",
                             _require_user(), note or None)
    db.audit(_require_user(), "course_approved" if action == "approve" else "course_rejected",
             f"{row['course_id']} {row['title']}", note or "")
    if action == "approve":
        courses.reload()
    return None


@app.route("/trainer/approvals")
@role_required("trainer")
@trainer_profile_required
def trainer_approvals():
    queue = _approval_queue(_require_user())
    for row in queue:
        row["video_embeds"] = [{"url": v, "embed": courses.youtube_embed(v)} for v in row["videos"]]
        row["qcount"] = sum(len(json.loads(b["questions"] or "[]"))
                            for b in db.list_banks_for_course(row["course_id"]))
    done = [r for r in db.list_course_reviews(by_user_id=_require_user())
            if r["action"] in ("approved", "rejected")][:10]
    return render_template("trainer_approvals.html", queue=queue, done=done,
                           message=request.args.get("msg"), error=request.args.get("err"),
                           **_trainer_ctx("approvals"))


@app.route("/trainer/approvals/<int:row_id>", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_approve(row_id: int):
    row = next((r for r in _approval_queue(_require_user()) if r["id"] == row_id), None)
    if not row:
        return redirect(url_for("trainer_approvals",
                                err="That course isn't in your approval queue (already reviewed, or not in your field)."))
    action = request.form.get("action", "")
    err = _decide(row, action, (request.form.get("note") or "").strip())
    if err:
        return redirect(url_for("trainer_approvals", err=err) + f"#c-{row_id}")
    verb = "approved — it is now live in the catalogue" if action == "approve" else "rejected and sent back to its uploader"
    return redirect(url_for("trainer_approvals", msg=f"“{row['title']}” {verb}."))


@app.route("/trainer/profile", methods=["GET", "POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_profile():
    error = message = None
    if request.method == "POST":
        specs = _clean_specialisations(request.form.getlist("specialisations"))
        if not specs:
            error = "Pick at least one skill."
        else:
            db.upsert_trainer_profile(_require_user(), specs)
            message = "Specialisations updated."
    ctx = _trainer_ctx("profile")
    return render_template("trainer_profile.html", groups=_skill_groups(), error=error, message=message,
                           students=len(db.list_trainer_students(_require_user())),
                           uploads=len(db.list_trainer_courses(_require_user())), **ctx)


# ---------------------------------------------------------------------------
# Verify MCQs (trainer reviews AI questions of the courses THEY uploaded)
# ---------------------------------------------------------------------------

_GEN_JOBS: dict[str, dict] = {}   # course_id -> {"status": "generating"|"error", "error": str}
_GEN_LOCK = threading.Lock()
REVIEW_FILTERS = ("pending", "approved", "rejected", "flagged", "all")


def _q_status(q: dict) -> str:
    return (q.get("review") or {}).get("status") or "pending"


def _my_upload_ids(trainer_id: int) -> list[str]:
    return [c["course_id"] for c in db.list_trainer_courses(trainer_id)]


def _pending_reviews(trainer_id: int) -> int:
    n = 0
    for cid in _my_upload_ids(trainer_id):
        for bank in db.list_banks_for_course(cid):
            n += sum(1 for q in json.loads(bank["questions"] or "[]") if _q_status(q) == "pending")
    return n


def _my_bank(bank_id: int) -> dict:
    bank = db.get_bank_by_id(bank_id)
    if not bank or bank["course_id"] not in _my_upload_ids(_require_user()):
        abort(404)
    return bank


def _generate_for_course(course_id: str) -> None:
    course = courses.get_any(course_id) or {}
    error = "This course has no video links."
    for video in course.get("videos") or []:
        questions, error = _generate_questions(video, course.get("title", course_id))
        if not error:
            db.save_bank(course_id, video, questions, demo=os.environ.get("VIVARAN_FAKE_QUIZ") == "1")
            with _GEN_LOCK:
                _GEN_JOBS.pop(course_id, None)
            return
    with _GEN_LOCK:
        _GEN_JOBS[course_id] = {"status": "error", "error": error}


@app.route("/trainer/verify")
@role_required("trainer")
@trainer_profile_required
def trainer_verify():
    f = request.args.get("f", "pending")
    f = f if f in REVIEW_FILTERS else "pending"
    only = request.args.get("course")
    blocks, counts = [], {k: 0 for k in REVIEW_FILTERS}
    for up in db.list_trainer_courses(_require_user()):
        cid = up["course_id"]
        if only and cid != only:
            continue
        sets = []
        for bank in db.list_banks_for_course(cid):
            qs = json.loads(bank["questions"] or "[]")
            items = []
            for idx, q in enumerate(qs):
                st = _q_status(q)
                flagged = (q.get("verification_status") or "").lower() == "flagged"
                counts[st] += 1
                counts["all"] += 1
                counts["flagged"] += flagged
                if f == "all" or f == st or (f == "flagged" and flagged):
                    items.append({"idx": idx, "q": q, "status": st, "flagged": flagged})
            sets.append({"bank": bank, "questions": items, "total": len(qs),
                         "approved": sum(1 for q in qs if _q_status(q) == "approved"),
                         "pending": sum(1 for q in qs if _q_status(q) == "pending")})
        with _GEN_LOCK:
            job = dict(_GEN_JOBS.get(cid) or {})
        blocks.append({"course": up, "sets": sets, "job": job})
    return render_template("trainer_verify.html", blocks=blocks, f=f, counts=counts, only=only,
                           message=request.args.get("msg"), error=request.args.get("err"),
                           **_trainer_ctx("verify"))


@app.route("/trainer/verify/<course_id>/generate", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_verify_generate(course_id: str):
    if course_id not in _my_upload_ids(_require_user()):
        abort(404)
    with _GEN_LOCK:
        if (_GEN_JOBS.get(course_id) or {}).get("status") == "generating":
            return redirect(url_for("trainer_verify", f="all", course=course_id))
        _GEN_JOBS[course_id] = {"status": "generating"}
    threading.Thread(target=_generate_for_course, args=(course_id,), daemon=True,
                     name=f"gen-{course_id}").start()
    return redirect(url_for("trainer_verify", f="all", course=course_id,
                            msg="Generating questions from the course video — this can take a few minutes."))


@app.route("/trainer/verify/<course_id>/status.json")
@role_required("trainer")
@trainer_profile_required
def trainer_verify_status(course_id: str):
    if course_id not in _my_upload_ids(_require_user()):
        abort(404)
    with _GEN_LOCK:
        job = dict(_GEN_JOBS.get(course_id) or {})
    return jsonify({"status": job.get("status") or ("ready" if db.list_banks_for_course(course_id) else "none"),
                    "error": job.get("error")})


@app.route("/trainer/verify/bank/<int:bank_id>/q/<int:idx>", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_verify_question(bank_id: int, idx: int):
    bank = _my_bank(bank_id)
    qs = json.loads(bank["questions"] or "[]")
    if not 0 <= idx < len(qs):
        abort(404)
    q = qs[idx]
    action = request.form.get("action")
    back = {"f": request.form.get("f", "pending"), "course": request.form.get("course") or None}
    me = session.get("username")
    if action in ("approve", "reject"):
        q["review"] = {"status": "approved" if action == "approve" else "rejected", "by": me, "at": db._now()}
    elif action == "reset":
        q.pop("review", None)
    elif action == "edit":
        question = (request.form.get("question") or "").strip()
        options = [(request.form.get(f"option_{i}") or "").strip() for i in range(4)]
        options = [o for o in options if o]
        try:
            correct = options[int(request.form.get("answer_index", "-1"))]
        except (ValueError, IndexError):
            correct = ""
        if len(question) < 5 or len(options) < 2 or not correct or len(set(o.lower() for o in options)) != len(options):
            return redirect(url_for("trainer_verify", err="Each question needs text, at least 2 different options "
                                    "and one marked correct.", **back) + f"#q-{bank_id}-{idx}")
        if "original" not in q:
            q["original"] = {k: q.get(k) for k in ("question", "options", "answer", "explanation")}
        q.update({"question": question, "options": options, "answer": correct,
                  "explanation": (request.form.get("explanation") or "").strip()})
        q["review"] = {"status": "approved", "by": me, "at": db._now(), "edited": True}
    else:
        abort(400)
    db.update_bank_questions(bank_id, qs)
    db.audit(_require_user(), f"mcq_{action}", bank["course_id"], (q.get("question") or "")[:120])
    return redirect(url_for("trainer_verify", **back) + f"#set-{bank_id}")


@app.route("/trainer/verify/bank/<int:bank_id>/approve-all", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def trainer_verify_approve_all(bank_id: int):
    bank = _my_bank(bank_id)
    qs = json.loads(bank["questions"] or "[]")
    n = 0
    for q in qs:
        if _q_status(q) == "pending":
            q["review"] = {"status": "approved", "by": session.get("username"), "at": db._now()}
            n += 1
    db.update_bank_questions(bank_id, qs)
    return redirect(url_for("trainer_verify", f=request.form.get("f", "pending"),
                            course=request.form.get("course") or None,
                            msg=f"Approved {n} question{'s' if n != 1 else ''}."))


@app.route("/api/trainer/students")
@role_required("trainer")
@trainer_profile_required
def api_trainer_students():
    return jsonify(_students_overview(_require_user()))


@app.route("/api/trainer/students/<int:learner_id>")
@role_required("trainer")
@trainer_profile_required
def api_trainer_student(learner_id: int):
    student = _my_student(learner_id)
    d = _student_detail(learner_id)
    return jsonify({"student": student, "courses": d["rows"], "completed": d["completed"],
                    "overall_progress": d["overall"], "avg_score": d["avg_score"],
                    "quiz_results": [{k: a[k] for k in ("course_id", "course_title", "score", "total",
                                                       "accuracy", "finished_at")} for a in d["attempts"]]})


@app.route("/api/trainer/students/<int:learner_id>/courses", methods=["POST"])
@role_required("trainer")
@trainer_profile_required
def api_trainer_edit_course(learner_id: int):
    _my_student(learner_id)
    body = _json_body() or {}
    action = body.get("action")
    if action not in ("add", "remove"):
        return jsonify({"ok": False, "error": "action must be 'add' or 'remove'"}), 400
    err = _path_edit(learner_id, (body.get("course_id") or "").strip(), action)
    return (jsonify({"ok": False, "error": err}), 400) if err else jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin workspace (sidebar layout: templates/admin_base.html)
# ---------------------------------------------------------------------------

MAX_ADMINS = 3  # the primary admin + 2 more


def _pct(n: float, d: float) -> float | None:
    return round(100 * n / d, 1) if d else None


def _admin_ctx(active: str) -> dict:
    me = db.get_user_by_id(_require_user())
    pending = db.list_trainer_courses(status="pending")
    return {"me": me, "active": active, "skill_name": courses.skill_name,
            "pending_courses_n": len(pending),
            "needs_admin_n": sum(1 for r in pending if _needs_admin(r)),
            "threshold": settings.PASS_THRESHOLD,
            "message": request.args.get("msg"), "error": request.args.get("err")}


def _days(n: int = 14) -> list[str]:
    from datetime import date, timedelta
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _graded_all() -> list[dict]:
    rows = [a for a in db.list_all_attempts() if a["status"] == "graded"]
    for a in rows:
        a["passed_bool"] = (a.get("accuracy") or 0) >= settings.PASS_THRESHOLD
    return rows


def _flagged_pending() -> int:
    n = 0
    for b in db.list_bank():
        full = db.get_bank_by_id(b["id"])
        for q in json.loads((full or {}).get("questions") or "[]"):
            if (q.get("verification_status") or "").lower() == "flagged" and _q_status(q) == "pending":
                n += 1
    return n


@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    learners = _learner_rows()
    trainers = db.list_trainers_with_profiles()
    enrollments = db.list_enrollments()
    graded = _graded_all()
    users = db.list_users()
    days = _days(14)
    reg = {d: {"learner": 0, "trainer": 0} for d in days}
    for u in users:
        d = (u.get("created_at") or "")[:10]
        if d in reg and u["role"] in ("learner", "trainer"):
            reg[d][u["role"]] += 1
    quiz = {d: {"passed": 0, "failed": 0} for d in days}
    for a in graded:
        d = (a.get("finished_at") or "")[:10]
        if d in quiz:
            quiz[d]["passed" if a["passed_bool"] else "failed"] += 1
    depts: dict[str, int] = {}
    for l in learners:
        if l.get("department"):
            depts[l["department"]] = depts.get(l["department"], 0) + 1
    dept_rows = sorted(depts.items(), key=lambda kv: -kv[1])[:8]
    passed = sum(1 for a in graded if a["passed_bool"])
    stats = {
        "learners": len(learners), "trainers": len(trainers),
        "courses": len(courses.all_courses()), "enrollments": len(enrollments),
        "completions": sum(1 for e in enrollments if e["status"] == "completed"),
        "quizzes": len(graded), "pass_rate": _pct(passed, len(graded)),
        "unassigned": len(db.list_unassigned_learners()),
        "inactive": sum(1 for u in users if not u.get("active", 1)),
        "flagged": _flagged_pending(),
    }
    return render_template(
        "admin_overview.html", stats=stats, days=days, reg=reg, quiz=quiz, dept_rows=dept_rows,
        reg_max=max([reg[d]["learner"] + reg[d]["trainer"] for d in days] + [1]),
        quiz_max=max([quiz[d]["passed"] + quiz[d]["failed"] for d in days] + [1]),
        dept_max=max([c for _, c in dept_rows] + [1]), passed=passed,
        activity=db.list_audit(limit=10), **_admin_ctx("overview"))


# ---------------- learners ----------------

@app.route("/admin/learners")
@role_required("admin")
def admin_learners():
    rows = _learner_rows()
    trainers = {t["id"]: t for t in db.list_trainers_with_profiles()}
    q = (request.args.get("q") or "").strip().lower()
    dept = request.args.get("department") or ""
    trainer = request.args.get("trainer") or ""
    status = request.args.get("status") or ""
    out = []
    for r in rows:
        if q and q not in " ".join(str(r.get(k) or "") for k in ("username", "email", "designation", "department", "area_of_experience")).lower():
            continue
        if dept and r.get("department") != dept:
            continue
        if trainer == "none" and r.get("trainer_id"):
            continue
        if trainer and trainer != "none" and str(r.get("trainer_id")) != trainer:
            continue
        if status == "not_started" and (r["in_progress"] or r["completed"]):
            continue
        if status == "in_progress" and not r["in_progress"]:
            continue
        if status == "completed" and not r["completed"]:
            continue
        if status == "inactive" and r.get("active", 1):
            continue
        out.append({**r, "prog": progress.brief(r["id"])})
    departments = sorted({r["department"] for r in rows if r.get("department")})
    return render_template("admin_learners.html", rows=out, total=len(rows), trainers=list(trainers.values()),
                           departments=departments, f={"q": request.args.get("q", ""), "department": dept,
                                                       "trainer": trainer, "status": status},
                           **_admin_ctx("learners"))


def _learner_or_404(learner_id: int) -> dict:
    learner = next((l for l in db.list_learners_with_profiles() if l["id"] == learner_id), None)
    if not learner:
        abort(404)
    return learner


@app.route("/admin/learners/<int:learner_id>")
@role_required("admin")
def admin_learner(learner_id: int):
    learner = _learner_or_404(learner_id)
    detail = _student_detail(learner_id) if learner.get("role_id") else {"rows": [], "attempts": [], "completed": 0, "overall": 0, "avg_score": None}
    q = (request.args.get("q") or "").strip().lower()
    results = []
    if q and learner.get("role_id"):
        on = {r["course_id"] for r in detail["rows"] if r["on_path"] or r["status"] == "completed"} | set(db.unpublished_courses())
        results = [c for c in courses.all_courses() if c["course_id"] not in on and (
            q in c["title"].lower() or q in c["course_id"].lower() or any(q in n.lower() for n in c.get("skill_names") or []))]
        results.sort(key=lambda c: (q not in c["title"].lower(), c["title"]))
        results = results[:25]
    return render_template("admin_learner.html", student=learner, d=detail, q=request.args.get("q", ""),
                           prog=progress.summary(learner_id) if learner.get("role_id") else None,
                           results=results, trainers=db.list_trainers_with_profiles(),
                           **_admin_ctx("learners"))


@app.route("/admin/learners/<int:learner_id>/courses", methods=["POST"])
@role_required("admin")
def admin_learner_add(learner_id: int):
    _learner_or_404(learner_id)
    cid = (request.form.get("course_id") or "").strip()
    err = _path_edit(learner_id, cid, "add")
    title = (courses.get(cid) or {}).get("title", cid)
    return redirect(url_for("admin_learner", learner_id=learner_id, **({"err": err} if err else {"msg": f"Added “{title}”."})))


@app.route("/admin/learners/<int:learner_id>/courses/<course_id>/remove", methods=["POST"])
@role_required("admin")
def admin_learner_remove(learner_id: int, course_id: str):
    _learner_or_404(learner_id)
    err = _path_edit(learner_id, course_id, "remove")
    title = (courses.get(course_id) or {}).get("title", course_id)
    return redirect(url_for("admin_learner", learner_id=learner_id, **({"err": err} if err else {"msg": f"Removed “{title}”."})))


@app.route("/admin/assign", methods=["POST"])
@role_required("admin")
def admin_assign_trainer():
    """Move a learner to another trainer (or leave them unassigned)."""
    back = request.form.get("next") or url_for("admin_learners")
    if not back.startswith("/admin"):
        back = url_for("admin_learners")
    sep = "&" if "?" in back else "?"
    try:
        learner_id = int(request.form.get("learner_id") or 0)
    except ValueError:
        learner_id = 0
    learner = next((l for l in db.list_learners_with_profiles() if l["id"] == learner_id), None)
    if not learner or not learner.get("role_id"):
        return redirect(back + sep + "err=That+learner+has+no+profile+yet.")
    raw = (request.form.get("trainer_id") or "").strip()
    trainer_id, who = None, "no trainer"
    if raw:
        trainer = next((t for t in db.list_trainers_with_profiles() if str(t["id"]) == raw and t.get("active", 1)), None)
        if not trainer:
            return redirect(back + sep + "err=Unknown+or+inactive+trainer.")
        trainer_id, who = trainer["id"], trainer["username"]
    db.set_learner_trainer(learner_id, trainer_id)
    db.audit(_require_user(), "learner_reassigned", learner["username"], f"to {who}")
    from urllib.parse import quote
    return redirect(back + sep + "msg=" + quote(f"{learner['username']} is now assigned to {who}."))


# ---------------- trainers ----------------

def _trainer_rows() -> list[dict]:
    out = []
    reviews = db.list_course_reviews()
    uploads = db.list_trainer_courses()
    for t in db.list_trainers_with_profiles():
        studs = _students_overview(t["id"])
        out.append({**t, "avg_progress": round(sum(s["overall"] for s in studs) / len(studs)) if studs else None,
                    "uploads": sum(1 for u in uploads if u["trainer_id"] == t["id"]),
                    "live_uploads": sum(1 for u in uploads if u["trainer_id"] == t["id"] and u["status"] == "approved"),
                    "reviews": sum(1 for r in reviews if r["by_user_id"] == t["id"] and r["action"] in ("approved", "rejected"))})
    return out


@app.route("/admin/trainers")
@role_required("admin")
def admin_trainers():
    rows = _trainer_rows()
    active = [t for t in rows if t.get("active", 1) and t["has_profile"]]
    total = sum(t["students"] for t in active) + len(db.list_unassigned_learners())
    target = -(-total // len(active)) if active else 0
    return render_template("admin_trainers.html", rows=rows, unassigned=len(db.list_unassigned_learners()),
                           target=target, **_admin_ctx("trainers"))


@app.route("/admin/trainers/<int:trainer_id>")
@role_required("admin")
def admin_trainer(trainer_id: int):
    t = next((r for r in _trainer_rows() if r["id"] == trainer_id), None)
    if not t:
        abort(404)
    return render_template("admin_trainer.html", t=t, students=_students_overview(trainer_id),
                           uploads=db.list_trainer_courses(trainer_id),
                           reviews=[r for r in db.list_course_reviews(by_user_id=trainer_id) if r["action"] in ("approved", "rejected")],
                           **_admin_ctx("trainers"))


def _balance() -> int:
    """Even out students across active trainers, moving the students who
    match their current trainer least to the best-matching lighter trainer."""
    moved = 0
    for l in db.list_unassigned_learners():
        if _assign_trainer_for(l["id"]):
            moved += 1
    trainers = [t for t in db.list_trainers_with_profiles() if t["has_profile"] and t.get("active", 1)]
    if len(trainers) < 2:
        return moved
    learners = [l for l in db.list_learners_with_profiles() if l.get("role_id")]
    load = {t["id"]: 0 for t in trainers}
    for l in learners:
        if l.get("trainer_id") in load:
            load[l["trainer_id"]] += 1
    target = -(-sum(load.values()) // len(trainers))
    specs = {t["id"]: t["specialisations"] for t in trainers}
    for tid in sorted(load, key=lambda k: -load[k]):
        mine = [l for l in learners if l.get("trainer_id") == tid]
        mine.sort(key=lambda l: recommender.match_score(specs[tid], l))
        for l in mine:
            if load[tid] <= target:
                break
            under = [t for t in load if load[t] < target and t != tid]
            if not under:
                break
            best = max(under, key=lambda t: (recommender.match_score(specs[t], l), -load[t]))
            db.set_learner_trainer(l["id"], best)
            load[tid] -= 1
            load[best] += 1
            moved += 1
    return moved


@app.route("/admin/trainers/balance", methods=["POST"])
@role_required("admin")
def admin_balance():
    n = _balance()
    db.audit(_require_user(), "trainers_auto_balanced", "", f"{n} learner(s) moved")
    return redirect(url_for("admin_trainers", msg=f"Auto-balance done — {n} learner{'s' if n != 1 else ''} moved."))


@app.route("/admin/trainers/assign-unassigned", methods=["POST"])
@role_required("admin")
def admin_assign_unassigned():
    n = sum(1 for l in db.list_unassigned_learners() if _assign_trainer_for(l["id"]))
    db.audit(_require_user(), "unassigned_learners_assigned", "", f"{n} learner(s)")
    return redirect(url_for("admin_trainers", msg=f"{n} learner{'s' if n != 1 else ''} assigned to trainers."
                            if n else "No learners could be assigned (no unassigned learners, or no active trainers)."))


# ---------------- account actions (deactivate / reset) ----------------

def _account_guard(target: dict) -> str | None:
    me = db.get_user_by_id(_require_user())
    if target["id"] == me["id"]:
        return "You can't do that to your own account."
    if target["role"] == "admin" and target.get("is_primary"):
        return "The main admin account can't be changed."
    if target["role"] == "admin" and not me.get("is_primary"):
        return "Only the main admin can manage other admins."
    return None


def _back(default: str) -> str:
    nxt = request.form.get("next") or ""
    return nxt if nxt.startswith("/admin") else default


def _with(url: str, **params) -> str:
    from urllib.parse import urlencode
    return url + ("&" if "?" in url else "?") + urlencode(params)


@app.route("/admin/users/<int:user_id>/active", methods=["POST"])
@role_required("admin")
def admin_set_active(user_id: int):
    target = db.get_user_by_id(user_id) or abort(404)
    back = _back(url_for("admin_staff"))
    err = _account_guard(target)
    if err:
        return redirect(_with(back, err=err))
    make_active = request.form.get("active") == "1"
    db.set_user_fields(user_id, active=1 if make_active else 0)
    note = ""
    if target["role"] == "trainer" and not make_active:
        studs = db.list_trainer_students(user_id)
        for s_ in studs:
            db.set_learner_trainer(s_["id"], None)
        moved = sum(1 for s_ in studs if _assign_trainer_for(s_["id"]))
        note = f" {moved} of {len(studs)} student(s) moved to other trainers."
    db.audit(_require_user(), "account_reactivated" if make_active else "account_deactivated",
             target["username"], note.strip())
    return redirect(_with(back, msg=f"{target['username']} {'reactivated' if make_active else 'deactivated'}.{note}"))


def _temp_password() -> str:
    return secrets.token_urlsafe(8)[:10] + "7a"


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@role_required("admin")
def admin_reset_password(user_id: int):
    target = db.get_user_by_id(user_id) or abort(404)
    err = _account_guard(target)
    if err:
        return redirect(_with(_back(url_for("admin_staff")), err=err))
    temp = _temp_password()
    db.set_user_fields(user_id, password_hash=_new_password_hash(temp), must_change_password=1)
    db.audit(_require_user(), "password_reset", target["username"])
    return render_template("admin_temp_password.html", who=target, temp=temp, back=_back(url_for("admin_staff")),
                           title="Password reset", **_admin_ctx("staff"))


# ---------------- approvals ----------------

@app.route("/admin/approvals")
@role_required("admin")
def admin_approvals():
    show = request.args.get("show", "all")
    queue = []
    for r in db.list_trainer_courses(status="pending"):
        r = {**r, "needs_admin": _needs_admin(r),
             "reviewers": len(_eligible_reviewers_for(r["skills"], r["trainer_id"])),
             "video_embeds": [{"url": v, "embed": courses.youtube_embed(v)} for v in r["videos"]],
             "qcount": sum(len(json.loads(b["questions"] or "[]")) for b in db.list_banks_for_course(r["course_id"]))}
        if show == "admin" and not r["needs_admin"]:
            continue
        queue.append(r)
    return render_template("admin_approvals.html", queue=queue, show=show,
                           history=db.list_course_reviews()[:60], **_admin_ctx("approvals"))


@app.route("/admin/courses/<int:row_id>/review", methods=["POST"])
@role_required("admin")
def admin_review_course(row_id: int):
    row = db.get_trainer_course(row_id)
    if not row:
        abort(404)
    action = request.form.get("action", "")
    err = _decide(row, action, (request.form.get("note") or "").strip())
    if err:
        return redirect(url_for("admin_approvals", err=err) + f"#c-{row_id}")
    return redirect(url_for("admin_approvals", msg=f"“{row['title']}” {'approved — now live' if action == 'approve' else 'rejected'}."))


# ---------------- skill-gap analytics ----------------

@lru_cache(maxsize=1)
def _workforce_employees() -> tuple:
    """Dataset-3 synthetic workforce (System 1, read-only) as (role, dept, levels)."""
    def unpack(v: str) -> dict:
        out = {}
        for part in (v or "").split(";"):
            if ":" in part:
                k, lvl = part.split(":", 1)
                try:
                    out[k.strip()] = int(lvl)
                except ValueError:
                    pass
        return out
    try:
        rows = courses._read_csv(courses.S1_DATASETS / "Dataset-3_Syn_Employee_Profiles.csv")
    except OSError:
        return ()
    out = []
    for r in rows:
        levels = unpack(r.get("self_rated_skills"))
        levels.update(unpack(r.get("quiz_verified_skills")))  # quiz-verified wins (System 1 rule)
        out.append((r.get("role_id"), r.get("department") or "Unknown", levels))
    return tuple(out)


def _gap_analysis(segment: str) -> dict:
    if segment == "platform":
        people = []
        for l in db.list_learners_with_profiles():
            if not l.get("role_id"):
                continue
            done = [e for e in db.list_enrollments(l["id"]) if e["status"] == "completed"]
            people.append((l["role_id"], l.get("department") or "Unknown", recommender.verified_skills(done)))
    else:
        people = list(_workforce_employees())
    req: dict[str, list] = {}
    for row in recommender._dataset2():
        req.setdefault(row["role_id"], []).append(row)
    skill_ids = sorted(courses.skills())
    per_skill = {s_: {"people": 0, "gap_sum": 0} for s_ in skill_ids}
    dept_people: dict[str, int] = {}
    dept_skill: dict[str, dict[str, int]] = {}
    total_gaps = 0
    for role_id, dept, levels in people:
        dept_people[dept] = dept_people.get(dept, 0) + 1
        for r in req.get(role_id, []):
            gap = int(r["required_level"]) - levels.get(r["skill_id"], 0)
            if gap > 0 and r["skill_id"] in per_skill:
                per_skill[r["skill_id"]]["people"] += 1
                per_skill[r["skill_id"]]["gap_sum"] += gap
                dept_skill.setdefault(dept, {}).setdefault(r["skill_id"], 0)
                dept_skill[dept][r["skill_id"]] += 1
                total_gaps += 1
    unpublished = set(db.unpublished_courses())
    coverage = {s_: sum(1 for c in courses.all_courses() if s_ in c["skills"] and c["course_id"] not in unpublished)
                for s_ in skill_ids}
    n = len(people)
    skills_rows = sorted(({"skill_id": s_, "skill_name": courses.skill_name(s_),
                           "category": (courses.skills().get(s_) or {}).get("category", ""),
                           "people": v["people"], "pct": _pct(v["people"], n) or 0,
                           "avg_gap": round(v["gap_sum"] / v["people"], 2) if v["people"] else 0,
                           "courses": coverage[s_]} for s_, v in per_skill.items()),
                         key=lambda r: -r["people"])
    top_depts = sorted(dept_people, key=lambda d: -dept_people[d])[:8]
    heat = [{"dept": d, "n": dept_people[d],
             "cells": [round(100 * dept_skill.get(d, {}).get(s_["skill_id"], 0) / dept_people[d])
                       for s_ in skills_rows]} for d in top_depts]
    heat_max = max([v for h in heat for v in h["cells"]] + [1])
    return {"n": n, "skills": skills_rows, "heat": heat, "heat_max": heat_max,
            "uncovered": [r for r in skills_rows if r["people"] and r["courses"] == 0],
            "avg_gaps": round(total_gaps / n, 2) if n else 0,
            "max_people": max([r["people"] for r in skills_rows] + [1])}


@app.route("/admin/skill-gaps")
@role_required("admin")
def admin_skill_gaps():
    segment = request.args.get("segment", "workforce")
    segment = segment if segment in ("workforce", "platform") else "workforce"
    return render_template("admin_skill_gaps.html", g=_gap_analysis(segment), segment=segment,
                           **_admin_ctx("gaps"))


# ---------------- staff accounts + extra admins ----------------

@app.route("/admin/staff")
@role_required("admin")
def admin_staff():
    admins = db.list_users("admin")
    return render_template("admin_staff.html", admins=admins, trainers=db.list_trainers_with_profiles(),
                           learners=db.list_users("learner"), max_admins=MAX_ADMINS,
                           can_add_admin=bool(db.get_user_by_id(_require_user()).get("is_primary")) and len(admins) < MAX_ADMINS,
                           **_admin_ctx("staff"))


@app.route("/admin/users", methods=["POST"])
@role_required("admin")
def admin_create_user():
    """Create a TRAINER account (admins are added by email, see admin_add_admin)."""
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower() or None
    password = request.form.get("password") or ""
    if not USERNAME_RE.match(username) or len(password) < 8:
        return redirect(url_for("admin_staff", err="Username 3-32 chars and password of at least 8 characters required."))
    if email and _validate_gov_email(email):
        return redirect(url_for("admin_staff", err=_validate_gov_email(email)))
    if db.get_user_by_username(username) or (email and db.get_user_by_email(email)):
        return redirect(url_for("admin_staff", err="That username or email is already in use."))
    db.create_user(username, _new_password_hash(password), email=email, role="trainer")
    db.audit(_require_user(), "trainer_created", username)
    return redirect(url_for("admin_staff", msg=f"Trainer account '{username}' created. They pick their specialisations at first login."))


@app.route("/admin/admins", methods=["POST"])
@role_required("admin")
def admin_add_admin():
    me = db.get_user_by_id(_require_user())
    if not me.get("is_primary"):
        return redirect(url_for("admin_staff", err="Only the main admin can add admins."))
    if len(db.list_users("admin")) >= MAX_ADMINS:
        return redirect(url_for("admin_staff", err=f"The platform already has the maximum of {MAX_ADMINS} admins."))
    email = (request.form.get("email") or "").strip().lower()
    err = _validate_gov_email(email)
    if err:
        return redirect(url_for("admin_staff", err=err))
    if db.get_user_by_email(email):
        return redirect(url_for("admin_staff", err="An account with this email already exists."))
    base = re.sub(r"[^A-Za-z0-9._-]", "", email.split("@")[0])[:28] or "admin"
    username, i = base, 2
    while db.get_user_by_username(username) or not USERNAME_RE.match(username):
        username = f"{base}{i}" if len(base) >= 3 else f"admin{i}"
        i += 1
    temp = _temp_password()
    uid = db.create_user(username, _new_password_hash(temp), email=email, role="admin")
    db.set_user_fields(uid, must_change_password=1)
    db.audit(me["id"], "admin_added", username, email)
    return render_template("admin_temp_password.html", who=db.get_user_by_id(uid), temp=temp,
                           back=url_for("admin_staff"), title="Admin added", new_admin=True, **_admin_ctx("staff"))


@app.route("/admin/admins/<int:user_id>/remove", methods=["POST"])
@role_required("admin")
def admin_remove_admin(user_id: int):
    target = db.get_user_by_id(user_id)
    if not target or target["role"] != "admin":
        abort(404)
    err = _account_guard(target)
    if err:
        return redirect(url_for("admin_staff", err=err))
    db.delete_user(user_id)
    db.audit(_require_user(), "admin_removed", target["username"], target.get("email") or "")
    return redirect(url_for("admin_staff", msg=f"Admin '{target['username']}' removed."))


# ---------------- courses ----------------

def _course_stats() -> dict[str, dict]:
    stats: dict[str, dict] = {}

    def row(cid: str) -> dict:
        return stats.setdefault(cid, {"recommended": 0, "enrolled": 0, "completed": 0, "attempts": 0, "passed": 0})
    for l in db.list_learners_with_profiles():
        for r in (db.latest_submission(l["id"]) or {}).get("recommendations") or []:
            row(r["course_id"])["recommended"] += 1
    for e in db.list_enrollments():
        row(e["course_id"])["enrolled"] += 1
        row(e["course_id"])["completed"] += e["status"] == "completed"
    for a in _graded_all():
        row(a["course_id"])["attempts"] += 1
        row(a["course_id"])["passed"] += a["passed_bool"]
    return stats


@app.route("/admin/courses")
@role_required("admin")
def admin_courses():
    q = (request.args.get("q") or "").strip().lower()
    source = request.args.get("source", "all")
    page = max(1, int(request.args.get("page", "1") or 1))
    unpub = db.unpublished_courses()
    stats = _course_stats()
    items = []
    for c in courses.all_courses():
        if q and q not in c["title"].lower() and q not in c["course_id"].lower() and q not in (c["category"] or "").lower():
            continue
        if source == "uploaded" and c.get("video_source") != "trainer":
            continue
        if source == "unpublished" and c["course_id"] not in unpub:
            continue
        if source == "in_use" and not stats.get(c["course_id"]):
            continue
        st = stats.get(c["course_id"], {})
        items.append({**c, "st": st, "unpublished": c["course_id"] in unpub,
                      "pass_rate": _pct(st.get("passed", 0), st.get("attempts", 0))})
    items.sort(key=lambda c: (-(c["st"].get("recommended", 0) + c["st"].get("enrolled", 0)), c["title"]))
    per = 50
    pages = max(1, -(-len(items) // per))
    return render_template("admin_courses.html", items=items[(page - 1) * per: page * per], total=len(items),
                           page=page, pages=pages, q=request.args.get("q", ""), source=source,
                           unpub_n=len(unpub), catalogue_source=courses.source_label(), **_admin_ctx("courses"))


@app.route("/admin/courses/<course_id>/publish", methods=["POST"])
@role_required("admin")
def admin_publish_course(course_id: str):
    course = courses.get(course_id) or abort(404)
    publish = request.form.get("publish") == "1"
    db.set_unpublished(course_id, not publish, _require_user())
    db.audit(_require_user(), "course_published" if publish else "course_unpublished", f"{course_id} {course['title']}")
    return redirect(_with(_back(url_for("admin_courses")),
                          msg=f"“{course['title']}” {'published again' if publish else 'unpublished — it will no longer be recommended or assignable'}."))


@app.route("/admin/reload-courses", methods=["POST"])
@role_required("admin")
def admin_reload_courses():
    cat = courses.reload()
    db.audit(_require_user(), "catalogue_reloaded", "", cat["source"])
    return redirect(url_for("admin_courses", msg=f"Loaded {len(cat['courses'])} courses from {cat['source']}."))


# ---------------- quiz quality ----------------

@app.route("/admin/quality")
@role_required("admin")
def admin_quality():
    graded = _graded_all()
    per: dict[str, dict] = {}
    missed: dict[str, dict] = {}
    for a in graded:
        c = per.setdefault(a["course_id"], {"course_id": a["course_id"], "title": a["course_title"],
                                            "attempts": 0, "passed": 0, "acc": []})
        c["attempts"] += 1
        c["passed"] += a["passed_bool"]
        c["acc"].append(a["accuracy"] or 0)
        try:
            quiz = json.loads(a["quiz"] or "[]")
            answers = json.loads(a["answers"] or "{}")
        except json.JSONDecodeError:
            continue
        for q in quiz:
            key = _card_key(q.get("question", ""))
            m = missed.setdefault(key, {"question": q.get("question", ""), "answer": q.get("answer", ""),
                                        "course": a["course_title"], "asked": 0, "wrong": 0})
            m["asked"] += 1
            sel = answers.get(str(q.get("id")), "")
            m["wrong"] += not (sel and str(sel).lower() == str(q.get("answer", "")).lower())
    course_rows = sorted(({**c, "pass_rate": _pct(c["passed"], c["attempts"]),
                           "avg": round(sum(c["acc"]) / len(c["acc"]), 1)} for c in per.values()),
                         key=lambda c: (c["pass_rate"] or 0, -c["attempts"]))
    missed_rows = sorted((dict(m, pct=_pct(m["wrong"], m["asked"])) for m in missed.values() if m["wrong"]),
                         key=lambda m: (-(m["pct"] or 0), -m["asked"]))[:15]
    flagged, coverage = [], []
    for b in db.list_bank():
        full = db.get_bank_by_id(b["id"]) or {}
        for q in json.loads(full.get("questions") or "[]"):
            if (q.get("verification_status") or "").lower() == "flagged":
                flagged.append({"course_id": b["course_id"], "title": (courses.get_any(b["course_id"]) or {}).get("title", b["course_id"]),
                                "question": q.get("question"), "status": _q_status(q)})
    for up in db.list_trainer_courses():
        qs = [q for bk in db.list_banks_for_course(up["course_id"]) for q in json.loads(bk["questions"] or "[]")]
        coverage.append({**up, "questions": len(qs), "approved": sum(1 for q in qs if _q_status(q) == "approved"),
                         "rejected": sum(1 for q in qs if _q_status(q) == "rejected"),
                         "pending": sum(1 for q in qs if _q_status(q) == "pending")})
    return render_template("admin_quality.html", course_rows=course_rows, missed=missed_rows, flagged=flagged,
                           coverage=coverage,
                           overall=_pct(sum(1 for a in graded if a["passed_bool"]), len(graded)),
                           attempts=len(graded), **_admin_ctx("quality"))


# ---------------- reports (CSV) ----------------

REPORTS = {
    "learners": ("Learner progress", "One row per learner: designation, field, trainer, courses on path, in progress, completed, average quiz score."),
    "quiz_results": ("Quiz results", "Every graded quiz: learner, course, total, correct, wrong, percentage, passed."),
    "enrollments": ("Course enrollments", "Every learner-course pair: status, progress, best quiz score, start and completion dates."),
    "coverage": ("Training coverage by department", "Per department: learners, enrollments, completions, completion rate, average quiz score."),
    "trainers": ("Trainers", "Trainers with specialisations, number of students, uploads and reviews."),
}


def _csv_response(name: str, header: list[str], rows: list[list]):
    import io
    from flask import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="vivaran_{name}.csv"'})


@app.route("/admin/reports")
@role_required("admin")
def admin_reports():
    return render_template("admin_reports.html", reports=REPORTS, **_admin_ctx("reports"))


@app.route("/admin/reports/<name>.csv")
@role_required("admin")
def admin_report_csv(name: str):
    if name not in REPORTS:
        abort(404)
    db.audit(_require_user(), "report_downloaded", name)
    if name == "learners":
        rows = [[l["username"], l.get("email") or "", l.get("designation") or "", l.get("department") or "",
                 l.get("area_of_experience") or "", l.get("trainer_username") or "", l["recommended"],
                 l["in_progress"], l["completed"], l["avg_score"] if l["avg_score"] is not None else "",
                 "yes" if l.get("active", 1) else "no"] for l in _learner_rows()]
        return _csv_response(name, ["learner", "email", "designation", "department", "area_of_experience", "trainer",
                                    "courses_on_path", "in_progress", "completed", "avg_quiz_pct", "active"], rows)
    if name == "quiz_results":
        rows = [[a["username"], a["course_id"], a["course_title"], a["total"], a["score"], (a["total"] or 0) - (a["score"] or 0),
                 a["accuracy"], "yes" if a["passed_bool"] else "no", a["finished_at"]] for a in _graded_all()]
        return _csv_response(name, ["learner", "course_id", "course", "total_questions", "correct", "wrong",
                                    "percentage", "passed", "completed_at"], rows)
    if name == "enrollments":
        rows = [[e["username"], e["course_id"], e["course_title"], e["status"], e["progress"],
                 f"{e['best_score']}/{e['best_total']}" if e.get("best_total") else "", e["started_at"],
                 e.get("completed_at") or ""] for e in db.list_enrollments()]
        return _csv_response(name, ["learner", "course_id", "course", "status", "progress_pct", "best_quiz",
                                    "started_at", "completed_at"], rows)
    if name == "coverage":
        by: dict[str, dict] = {}
        dept_of = {l["id"]: l.get("department") or "Unknown" for l in db.list_learners_with_profiles()}
        for uid, d in dept_of.items():
            by.setdefault(d, {"learners": 0, "enrolled": 0, "completed": 0, "acc": []})["learners"] += 1
        for e in db.list_enrollments():
            d = by.setdefault(dept_of.get(e["user_id"], "Unknown"), {"learners": 0, "enrolled": 0, "completed": 0, "acc": []})
            d["enrolled"] += 1
            d["completed"] += e["status"] == "completed"
        for a in _graded_all():
            by.setdefault(dept_of.get(a["user_id"], "Unknown"), {"learners": 0, "enrolled": 0, "completed": 0, "acc": []})["acc"].append(a["accuracy"] or 0)
        rows = [[d, v["learners"], v["enrolled"], v["completed"], _pct(v["completed"], v["enrolled"]) or 0,
                 round(sum(v["acc"]) / len(v["acc"]), 1) if v["acc"] else ""] for d, v in sorted(by.items())]
        return _csv_response(name, ["department", "learners", "enrollments", "completions", "completion_rate_pct",
                                    "avg_quiz_pct"], rows)
    rows = [[t["username"], t.get("email") or "", "; ".join(courses.skill_name(s_) for s_ in t["specialisations"]),
             t["students"], t["uploads"], t["reviews"], "yes" if t.get("active", 1) else "no"] for t in _trainer_rows()]
    return _csv_response(name, ["trainer", "email", "specialisations", "students", "uploads", "reviews_done", "active"], rows)


# ---------------- audit log ----------------

@app.route("/admin/audit")
@role_required("admin")
def admin_audit():
    action = request.args.get("action") or None
    return render_template("admin_audit.html", rows=db.list_audit(limit=500, action=action),
                           actions=db.audit_actions(), action=action or "", **_admin_ctx("audit"))


@app.errorhandler(404)
def not_found(_error):
    if _wants_json():
        return jsonify({"ok": False, "error": "Not found."}), 404
    return render_template("error.html", message="That page or course was not found.",
                           back=_home_url(session.get("role", "")) if "user_id" in session else "/"), 404


@app.errorhandler(500)
def server_error(_error):
    if _wants_json():
        return jsonify({"ok": False, "error": "Something went wrong on the server."}), 500
    return render_template("error.html", message="Something went wrong on our side. Please try again.",
                           back="/"), 500


# ---------------------------------------------------------------------------
# JSON APIs (same logic as the pages; never return password hashes)
# ---------------------------------------------------------------------------

def _public_course(course: dict) -> dict:
    return {k: course[k] for k in ("course_id", "title", "description", "category",
                                   "designations", "skills", "skill_names", "videos",
                                   "target_level", "duration_minutes", "mode", "provider",
                                   "language")}


@app.route("/api/learner/profile")
@role_required("learner")
def api_learner_profile():
    user = db.get_user_by_id(_require_user())
    profile = db.get_learner_profile(user["id"]) or {}
    return jsonify({"username": user["username"], "email": user.get("email"),
                    "role": user.get("role"), "designation": profile.get("designation"),
                    "department": profile.get("department"),
                    "area_of_experience": profile.get("area_of_experience"),
                    "s1_employee_id": profile.get("s1_employee_id")})


@app.route("/api/learner/recommendations")
@app.route("/api/learner/roadmap", endpoint="api_learner_roadmap")
@role_required("learner")
def api_learner_recommendations():
    data = _dashboard_data(_require_user())
    return jsonify({"count": len(data["roadmap"]), "roadmap": data["roadmap"]})


@app.route("/api/learner/progress")
@role_required("learner")
def api_learner_progress():
    return jsonify(db.list_enrollments(_require_user()))


@app.route("/api/learner/results")
@role_required("learner")
def api_learner_results():
    rows = [a for a in db.list_attempts_for_user(_require_user()) if a["status"] == "graded"]
    return jsonify([{
        "attempt_id": a["id"], "course_id": a["course_id"], "course_title": a["course_title"],
        "total_questions": a["total"], "correct_answers": a["score"],
        "wrong_answers": (a["total"] or 0) - (a["score"] or 0),
        "score": f"{a['score']}/{a['total']}", "percentage": a["accuracy"],
        "passed": (a["accuracy"] or 0) >= settings.PASS_THRESHOLD,
        "completed_at": a["finished_at"],
    } for a in rows])


@app.route("/api/courses")
@login_required
def api_courses():
    return jsonify({"source": courses.source_label(),
                    "courses": [_public_course(c) for c in courses.all_courses()]})


@app.route("/api/courses/<course_id>")
@login_required
def api_course(course_id: str):
    return jsonify(_public_course(_course_or_404(course_id)))


@app.route("/api/courses/<course_id>/generate-quiz", methods=["POST"])
@role_required("learner")
def api_generate_quiz(course_id: str):
    attempt_id, error = _start_course_quiz(_require_user(), _course_or_404(course_id))
    if error:
        return jsonify({"ok": False, "error": error}), 400
    attempt = db.get_attempt(attempt_id)
    return jsonify({"ok": True, "quiz_id": attempt_id, "status": attempt["status"],
                    "status_url": url_for("quiz_status_json", attempt_id=attempt_id)})


@app.route("/api/quiz/<int:attempt_id>")
@login_required
def api_quiz(attempt_id: int):
    attempt = _owned_attempt(attempt_id)
    quiz = json.loads(attempt["quiz"]) if attempt.get("quiz") else []
    return jsonify({"quiz_id": attempt_id, "status": attempt["status"], "error": attempt.get("error"),
                    "questions": [{"id": q.get("id"), "question": q.get("question"),
                                   "options": q.get("options")} for q in quiz]})


@app.route("/api/quiz/<int:attempt_id>/submit", methods=["POST"])
@login_required
def api_quiz_submit(attempt_id: int):
    attempt = _owned_attempt(attempt_id)
    if attempt["status"] != "ready":
        return jsonify({"ok": False, "error": f"Quiz is {attempt['status']}, not ready."}), 409
    quiz = json.loads(attempt["quiz"]) if attempt.get("quiz") else []
    body = _json_body() or {}
    answers = {str(k): v for k, v in (body.get("answers") or {}).items()}
    result = db.grade_attempt(attempt_id, answers, quiz)
    _after_grading(attempt, result)
    return jsonify({"ok": True, "total_questions": result["total"],
                    "correct_answers": result["score"],
                    "wrong_answers": result["total"] - result["score"],
                    "score": f"{result['score']}/{result['total']}",
                    "percentage": result["accuracy"],
                    "status": "Passed" if result["accuracy"] >= settings.PASS_THRESHOLD else "Not passed"})


@app.route("/api/admin/overview")
@role_required("admin")
def api_admin_overview():
    return jsonify({"learners": _learner_rows(), "trainers": db.list_users("trainer"),
                    "courses": _course_rows_overview()})


def seed_staff_accounts() -> None:
    """Create the first admin/trainer from environment variables (never
    hardcoded): VIVARAN_ADMIN_USER / VIVARAN_ADMIN_PASSWORD and
    VIVARAN_TRAINER_USER / VIVARAN_TRAINER_PASSWORD. Existing users are left alone."""
    for role in ("admin", "trainer"):
        username = os.environ.get(f"VIVARAN_{role.upper()}_USER")
        password = os.environ.get(f"VIVARAN_{role.upper()}_PASSWORD")
        if username and password and not db.get_user_by_username(username):
            uid = db.create_user(username, _new_password_hash(password), role=role)
            if role == "admin" and not any(a.get("is_primary") for a in db.list_users("admin")):
                db.set_user_fields(uid, is_primary=1)
            logger.info("Created %s account '%s' from environment", role, username)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    db.init_db()
    seed_staff_accounts()
    logger.info("Vivaran-VQE running on http://%s:%s  (System 1: %s)",
                settings.FLASK_HOST, settings.FLASK_PORT, settings.S1_URL)
    app.run(host=settings.FLASK_HOST, port=settings.FLASK_PORT, threaded=True, debug=False)
