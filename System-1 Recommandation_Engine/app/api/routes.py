"""
Stage E - HTTP API layer.

Three thin endpoints that translate requests into service calls and return
schema-validated JSON. No business logic lives here - services do the work,
repositories own the data, and this module only wires them together.

Response shapes are defined in ``app.models.schemas`` and are the integration
contract for the frontend - treat field names/types as public API.

Error handling: unknown employee_id -> 404; invalid ``top_n`` -> 422
(FastAPI/Query validation). No authentication - out of scope for this module.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app import config
from app.models.schemas import (
    BatchRecommendationEntry,
    ComputeResponse,
    EmployeeProfile,
    Gap,
    NewEmployeeInput,
    RecommendationResponse,
    SkillUpdateInput,
)

router = APIRouter(prefix="/employees", tags=["employees"])


def _404(employee_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Employee {employee_id} not found")


@router.get(
    "/{employee_id}/gaps",
    response_model=list[Gap],
    summary="Skill gaps for an employee",
    description=(
        "Required-vs-current skill levels for the employee's role. "
        "Quiz-verified levels are preferred over self-ratings. Only positive "
        "gaps are returned, sorted largest first."
    ),
)
def get_employee_gaps(employee_id: str, request: Request) -> list[Gap]:
    gaps = request.app.state.gap_service.compute_gaps(employee_id)
    if gaps is None:
        raise _404(employee_id)
    return gaps


@router.get(
    "/{employee_id}/recommendations",
    response_model=RecommendationResponse,
    summary="Course recommendations closing the employee's skill gaps",
    description=(
        "Ranked course recommendations (top_n, default 5) plus an explicit "
        "report of every gap skill with no course coverage - never silently "
        "omitted."
    ),
)
def get_employee_recommendations(
    employee_id: str,
    request: Request,
    top_n: int = Query(
        default=config.DEFAULT_TOP_N,
        ge=1,
        le=config.MAX_TOP_N,
        description="Number of course recommendations to return.",
    ),
) -> RecommendationResponse:
    result = request.app.state.recommendation_service.recommend(employee_id, top_n)
    if result is None:
        raise _404(employee_id)
    recommendations, unmatched_gaps = result
    return RecommendationResponse(
        employee_id=employee_id,
        recommendations=recommendations,
        unmatched_gaps=unmatched_gaps,
    )


@router.get(
    "/{employee_id}/profile",
    response_model=EmployeeProfile,
    summary="Raw employee profile",
    description=(
        "The employee's profile record for frontend display, with the packed "
        "skill/training fields parsed into lists and dictionaries."
    ),
)
def get_employee_profile(employee_id: str, request: Request) -> EmployeeProfile:
    employee = request.app.state.repos.employees.get(employee_id)
    if employee is None:
        raise _404(employee_id)
    return EmployeeProfile(
        employee_id=employee["employee_id"],
        name=employee["name"],
        role_id=employee["role_id"],
        designation=employee["designation"],
        department=employee["department"],
        current_assignment=employee.get("current_assignment"),
        educational_qualifications=employee.get("educational_qualifications"),
        work_experience_years=employee.get("work_experience_years"),
        previous_trainings=employee["_previous_trainings"],
        self_rated_skills=employee["_self_rated"],
        quiz_verified_skills=employee["_quiz_verified"],
    )


@router.post(
    "/{employee_id}/skills",
    status_code=200,
    summary="Update an employee's skill levels (System 2 integration)",
    description=(
        "System 2 (quiz generator) calls this when a quiz verifies a skill. "
        "Updates are persisted to disk (Dataset-3 or Dataset-5) and hot-added "
        "to the in-memory store, so the very next gap call reflects the new "
        "levels - no restart needed. At least one of self_rated_skills or "
        "quiz_verified_skills must be present."
    ),
)
def update_employee_skills(
    employee_id: str,
    payload: SkillUpdateInput,
    request: Request,
) -> dict:
    from app.data.loader import pack_skill_levels, update_employee_row

    if not payload.self_rated_skills and not payload.quiz_verified_skills:
        raise HTTPException(
            status_code=422, detail="At least one skill dict must be present"
        )

    repos = request.app.state.repos
    source = repos.employees.source(employee_id)
    if source is None:
        raise _404(employee_id)

    # Hot-update in memory
    updated = repos.employees.update_skills(
        employee_id,
        self_rated=payload.self_rated_skills,
        quiz_verified=payload.quiz_verified_skills,
    )

    # Persist to the right CSV
    csv_updates = {}
    if payload.self_rated_skills:
        merged = {**updated["_self_rated"]}
        csv_updates["self_rated_skills"] = pack_skill_levels(merged)
    if payload.quiz_verified_skills:
        merged = {**updated["_quiz_verified"]}
        csv_updates["quiz_verified_skills"] = pack_skill_levels(merged)

    path = (
        config.DATASET_FILES["real_employees"]
        if source == "real"
        else config.DATASET_FILES["employees"]
    )
    update_employee_row(path, employee_id, csv_updates)

    return {
        "employee_id": employee_id,
        "updated": True,
        "self_rated_skills": updated["_self_rated"],
        "quiz_verified_skills": updated["_quiz_verified"],
    }


@router.get(
    "/recommendations/batch",
    response_model=list[BatchRecommendationEntry],
    summary="Batch recommendations for ALL employees (Admin Dashboard)",
    description=(
        "Returns gaps + top-5 recommendations for every employee in one call, "
        "so System 3's Admin Dashboard does not loop 805 individual requests. "
        "Each row matches what the per-employee endpoints return."
    ),
)
def get_batch_recommendations(
    request: Request,
    top_n: int = Query(
        default=config.DEFAULT_TOP_N,
        ge=1,
        le=config.MAX_TOP_N,
        description="Number of course recommendations per employee.",
    ),
) -> list[BatchRecommendationEntry]:
    repos = request.app.state.repos
    gap_service = request.app.state.gap_service
    rec_service = request.app.state.recommendation_service

    batch = []
    for employee in repos.employees.all():
        gaps = gap_service.compute_gaps_for_employee(employee)
        recommendations, unmatched_gaps = rec_service.recommend_for_employee(
            employee, top_n
        )
        batch.append(
            BatchRecommendationEntry(
                employee_id=employee["employee_id"],
                gaps=gaps,
                recommendations=recommendations,
                unmatched_gaps=unmatched_gaps,
            )
        )
    return batch


def _validate_intake(payload: NewEmployeeInput, repos) -> None:
    """Shared validation for both on-the-fly endpoints."""
    if not repos.roles.has_role(payload.role_id):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown role_id '{payload.role_id}' - not in Dataset-2",
        )
    unknown_skills = [
        sid
        for sid in {**payload.self_rated_skills, **payload.quiz_verified_skills}
        if repos.skills.get(sid) is None
    ]
    if unknown_skills:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown skill_ids: {', '.join(sorted(unknown_skills))}",
        )


def _employee_profile(employee: dict) -> EmployeeProfile:
    """EmployeeProfile from a parsed employee record dict."""
    return EmployeeProfile(
        employee_id=employee["employee_id"],
        name=employee["name"],
        role_id=employee["role_id"],
        designation=employee["designation"],
        department=employee["department"],
        current_assignment=employee.get("current_assignment"),
        educational_qualifications=employee.get("educational_qualifications"),
        work_experience_years=employee.get("work_experience_years"),
        previous_trainings=employee["_previous_trainings"],
        self_rated_skills=employee["_self_rated"],
        quiz_verified_skills=employee["_quiz_verified"],
    )


@router.post(
    "/compute",
    response_model=ComputeResponse,
    summary="Compute gaps + recommendations for a NEW employee on the fly",
    description=(
        "Approach A preview: takes a full employee profile (role + current "
        "skill levels) that does NOT need to exist in the loaded dataset, runs "
        "the same Stage B/C/D pipeline as the lookup endpoints, and returns the "
        "profile echo plus gaps, ranked recommendations and the unmatched-gap "
        "report in one response. NOTHING is saved - use /employees/register "
        "to persist the profile to Dataset-5."
    ),
)
def compute_for_new_employee(
    payload: NewEmployeeInput,
    request: Request,
    top_n: int = Query(
        default=config.DEFAULT_TOP_N,
        ge=1,
        le=config.MAX_TOP_N,
        description="Number of course recommendations to return.",
    ),
) -> ComputeResponse:
    repos = request.app.state.repos
    _validate_intake(payload, repos)

    # Build a parsed employee record (same shape repositories produce).
    role_meta = repos.roles.get_meta(payload.role_id) or {}
    employee = {
        "employee_id": payload.employee_id or f"NEW-{payload.role_id}",
        "name": payload.name or "",
        "role_id": payload.role_id,
        "designation": payload.designation or role_meta.get("designation", ""),
        "department": payload.department or role_meta.get("department", ""),
        "current_assignment": payload.current_assignment,
        "educational_qualifications": payload.educational_qualifications,
        "work_experience_years": payload.work_experience_years,
        "_self_rated": payload.self_rated_skills,
        "_quiz_verified": payload.quiz_verified_skills,
        "_previous_trainings": payload.previous_trainings,
    }

    gaps = request.app.state.gap_service.compute_gaps_for_employee(employee)
    recommendations, unmatched_gaps = (
        request.app.state.recommendation_service.recommend_for_employee(
            employee, top_n
        )
    )

    return ComputeResponse(
        employee=_employee_profile(employee),
        gaps=gaps,
        recommendations=recommendations,
        unmatched_gaps=unmatched_gaps,
    )


@router.post(
    "/register",
    response_model=ComputeResponse,
    status_code=201,
    summary="Register a NEW employee: persist to Dataset-5 + compute",
    description=(
        "Like /employees/compute, but PERSISTS the profile: writes the new "
        "employee to Dataset-5_Real_Employee_Profiles.csv and hot-adds them to "
        "the in-memory store, so they are immediately queryable via the GET "
        "lookup endpoints (E806, E807, ...). Returns the profile echo plus "
        "gaps, ranked recommendations and the unmatched-gap report."
    ),
)
def register_employee(
    payload: NewEmployeeInput,
    request: Request,
    top_n: int = Query(
        default=config.DEFAULT_TOP_N,
        ge=1,
        le=config.MAX_TOP_N,
        description="Number of course recommendations to return.",
    ),
) -> ComputeResponse:
    _validate_intake(payload, request.app.state.repos)
    try:
        employee, gaps, recommendations, unmatched_gaps = (
            request.app.state.registration_service.register(payload, top_n)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return ComputeResponse(
        employee=_employee_profile(employee),
        gaps=gaps,
        recommendations=recommendations,
        unmatched_gaps=unmatched_gaps,
    )


# ---------------------------------------------------------------------------
# Admin analytics - dual segment (synthetic vs real) + combined + log
# ---------------------------------------------------------------------------

analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])


@analytics_router.get(
    "/synthetic",
    summary="Admin analytics - synthetic segment (Dataset-3, the 805)",
    description=(
        "Aggregates over the original synthetic workforce only - the baseline "
        "the engine was validated against. Real registrations never touch this."
    ),
)
def analytics_synthetic(request: Request) -> dict:
    return request.app.state.analytics_service.workforce("synthetic")


@analytics_router.get(
    "/real",
    summary="Admin analytics - real segment (Dataset-5 registrations)",
    description=(
        "Aggregates over the accumulating real-employee registrations, "
        "kept separate from the synthetic baseline."
    ),
)
def analytics_real(request: Request) -> dict:
    return request.app.state.analytics_service.workforce("real")


@analytics_router.get(
    "/combined",
    summary="Admin analytics - full workforce (synthetic + real)",
    description="The whole picture: original 805 plus every Dataset-5 registration.",
)
def analytics_combined(request: Request) -> dict:
    return request.app.state.analytics_service.workforce()


@analytics_router.get(
    "/registrations",
    summary="Admin registration log (Dataset-5 only)",
    description="Every real employee who registered, newest first.",
)
def analytics_registrations(request: Request) -> list[dict]:
    return request.app.state.analytics_service.registrations()


# ---------------------------------------------------------------------------
# Frontend helpers - form-building metadata
# ---------------------------------------------------------------------------

meta_router = APIRouter(prefix="/meta", tags=["meta"])


@meta_router.get(
    "/skills",
    summary="All 16 skills (intake form builder)",
    description="Skill id, name, category, description and proficiency levels.",
)
def list_skills(request: Request) -> list[dict]:
    return request.app.state.repos.skills.all()


@meta_router.get(
    "/roles",
    summary="All 60 roles (role picker for the intake form)",
    description="role_id with designation and department.",
)
def list_roles(request: Request) -> list[dict]:
    repos = request.app.state.repos
    roles = []
    for role_id in repos.roles.all_roles():
        meta = repos.roles.get_meta(role_id) or {}
        roles.append(
            {
                "role_id": role_id,
                "designation": meta.get("designation", ""),
                "department": meta.get("department", ""),
            }
        )
    return roles


@meta_router.get(
    "/courses",
    summary="All 784 courses (for previous-trainings picker)",
    description="Full course catalogue with id, title, and metadata.",
)
def list_courses(request: Request) -> list[dict]:
    return request.app.state.repos.courses.all()


@meta_router.get(
    "/unique-values",
    summary="Unique profile field values (intake form dropdowns)",
    description=(
        "The distinct values actually present in the datasets for designation, "
        "department, current_assignment, and educational_qualifications - "
        "useful for building dropdown/autocomplete lists on the intake form "
        "instead of free-typing."
    ),
)
def unique_profile_values(request: Request) -> dict:
    employees = request.app.state.repos.employees.all()
    fields = ("designation", "department", "current_assignment",
              "educational_qualifications")
    result = {}
    for field in fields:
        values = set()
        for row in employees:
            val = row.get(field)
            # Drop None, empty, NaN (pandas reads empty CSV cells as float NaN)
            if val and isinstance(val, str) and val.strip():
                values.add(val.strip())
        result[field] = sorted(values)
    return result
