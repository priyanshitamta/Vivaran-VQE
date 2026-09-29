"""
Vivaran-VQE (System 3) - client for the System-1 Recommendation Engine.

System 1 runs as its own FastAPI process (uvicorn, :8000). This module is the
only place System 3 talks to it, over HTTP using the stdlib (no requests
dependency). All calls go to S1's documented endpoints; we never reimplement
the recommendation logic.

Used for:
  * intake form metadata (skills / roles / courses / unique-values)
  * registering a new employee (POST /employees/register?top_n=5) which is
    what actually runs the gap + recommendation pipeline.

Responses are validated loosely here - System 1 is the schema owner and its
responses are already the integration contract.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import settings

S1_CONNECT_TIMEOUT = 8      # metadata / health probes
S1_REGISTER_TIMEOUT = 60    # registration computes gaps + recs server-side


class S1Error(Exception):
    """System 1 returned an error response (with its detail message)."""


class S1Unavailable(Exception):
    """System 1 could not be reached at all."""


def _read_error_detail(body: bytes, status: int) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"HTTP {status}"
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, list):  # pydantic validation errors
        return "; ".join(
            d.get("msg", str(d)) for d in detail if isinstance(d, dict)
        ) or f"HTTP {status}"
    if isinstance(detail, str):
        return detail
    return f"HTTP {status}"


def _request(method: str, url: str, payload=None, timeout: float = 15.0) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raise S1Error(_read_error_detail(err.read(), err.code)) from None
    except urllib.error.URLError as err:
        raise S1Unavailable(
            f"Cannot reach the recommendation engine at {settings.S1_URL} "
            f"({err.reason}). Is it running?"
        ) from None
    return json.loads(raw.decode("utf-8"))


def ping() -> bool:
    """True when System 1 answers (health probe for the launcher)."""
    try:
        _request("GET", settings.s1_url("/meta/skills"), timeout=S1_CONNECT_TIMEOUT)
        return True
    except (S1Error, S1Unavailable):
        return False


# ---------------------------------------------------------------------------
# Metadata (cached in-process for a few minutes - it is static per S1 boot)
# ---------------------------------------------------------------------------

_META_CACHE: dict[str, tuple[float, object]] = {}
_META_TTL = 300  # seconds


def _meta(name: str):
    if name not in settings.META_ENDPOINTS:
        raise ValueError(f"unknown meta endpoint '{name}'")
    cached = _META_CACHE.get(name)
    now = time.monotonic()
    if cached and now - cached[0] < _META_TTL:
        return cached[1]
    data = _request("GET", settings.s1_url(f"/meta/{name}"), timeout=S1_CONNECT_TIMEOUT)
    _META_CACHE[name] = (now, data)
    return data


def meta_skills() -> list[dict]:
    """All 16 skills: {skill_id, skill_name, category, ...}."""
    return _meta("skills")


def meta_roles() -> list[dict]:
    """All roles: [{role_id, designation, department}, ...]."""
    return _meta("roles")


def meta_courses_light() -> list[dict]:
    """Course catalogue trimmed to {course_id, course_title} for pickers."""
    courses = _meta("courses")
    return [
        {"course_id": c["course_id"], "course_title": c.get("course_title", "")}
        for c in courses
    ]


def meta_unique_values() -> dict:
    """Distinct {current_assignment, educational_qualifications, ...}."""
    return _meta("unique-values")


# ---------------------------------------------------------------------------
# Registration (runs the S1 pipeline and persists the profile to Dataset-5)
# ---------------------------------------------------------------------------


def register_employee(payload: dict, top_n: int = 5) -> dict:
    """POST /employees/register?top_n=N.

    ``payload`` is a System-1 ``NewEmployeeInput`` body. Returns the full
    ComputeResponse (employee echo + gaps + recommendations + unmatched_gaps).
    Raises S1Error with the engine's detail message on rejection (422, 409...).
    """
    query = urllib.parse.urlencode({"top_n": top_n})
    return _request(
        "POST",
        settings.s1_url(f"/employees/register?{query}"),
        payload=payload,
        timeout=S1_REGISTER_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Learner platform additions (existing S1 endpoints, nothing new on S1's side)
# ---------------------------------------------------------------------------


def compute(payload: dict, top_n: int = 5) -> dict:
    """POST /employees/compute - gaps + recs WITHOUT persisting to Dataset-5."""
    query = urllib.parse.urlencode({"top_n": top_n})
    return _request(
        "POST",
        settings.s1_url(f"/employees/compute?{query}"),
        payload=payload,
        timeout=S1_REGISTER_TIMEOUT,
    )


def update_skills(employee_id: str, quiz_verified_skills: dict[str, int]) -> dict:
    """POST /employees/{id}/skills - S1's System-2 hook: raise quiz-verified levels."""
    return _request(
        "POST",
        settings.s1_url(f"/employees/{urllib.parse.quote(employee_id)}/skills"),
        payload={"quiz_verified_skills": quiz_verified_skills},
        timeout=S1_CONNECT_TIMEOUT,
    )


def analytics(segment: str = "combined") -> dict:
    """GET /analytics/{synthetic|real|combined} - S1's admin workforce view."""
    if segment not in ("synthetic", "real", "combined"):
        raise ValueError(segment)
    return _request("GET", settings.s1_url(f"/analytics/{segment}"), timeout=S1_CONNECT_TIMEOUT)
