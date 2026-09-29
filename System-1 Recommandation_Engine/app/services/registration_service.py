"""
Approach A - registration service: persist a new employee to Dataset-5 and
compute their gaps + recommendations in one call.

This is the difference between "preview" (POST /employees/compute, stateless)
and "register" (POST /employees/register, persists). The profile is:
  1. Validated (role/skills) at the API layer.
  2. Assigned a real employee id (E806, E807, ...) if the caller did not
     supply one; a supplied id must be free (else 409).
  3. Run through the same Stage B/C/D pipeline as everything else (gaps +
     recommendations), computed BEFORE persisting so the run's recommended
     course ids can be stored with the row.
  4. Serialized to a Dataset-5 CSV row (packed skill/training fields back to
     strings, plus ``recommended_courses``) and appended to the file.
  5. Hot-added to the in-memory repository, so the new employee is
     immediately visible to every GET endpoint without a restart.

The frontend auth database is deliberately untouched - this engine only
persists the skill/profile data it computes against.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.data.loader import save_real_employee
from app.data.repositories import Repositories


class RegistrationService:
    def __init__(
        self,
        repos: Repositories,
        gap_service,
        recommendation_service,
    ) -> None:
        self._repos = repos
        self._gap_service = gap_service
        self._recommendation_service = recommendation_service

    def register(self, payload, top_n: int):
        """
        Persist a new employee and compute their gaps + recommendations.

        ``payload`` is a validated ``NewEmployeeInput``. Returns
        ``(employee_dict, gaps, recommendations, unmatched_gaps)`` where
        ``employee_dict`` is the parsed in-memory record (with the assigned
        ``employee_id`` and ``registered_at``). Raises ``ValueError`` if a
        caller-supplied ``employee_id`` is already taken.
        """
        employee_id = payload.employee_id
        if employee_id:
            if self._repos.employees.get(employee_id) is not None:
                raise ValueError(f"Employee {employee_id} already exists")
        else:
            employee_id = self._repos.employees.next_id()

        role_meta = self._repos.roles.get_meta(payload.role_id) or {}
        registered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Parsed record - same shape the repositories produce, plus the
        # registration metadata. Used by the services and hot-add.
        employee = {
            "employee_id": employee_id,
            "name": payload.name or "",
            "role_id": payload.role_id,
            "designation": payload.designation or role_meta.get("designation", ""),
            "department": payload.department or role_meta.get("department", ""),
            "current_assignment": payload.current_assignment,
            "educational_qualifications": payload.educational_qualifications,
            "work_experience_years": payload.work_experience_years,
            "registered_at": registered_at,
            "_self_rated": payload.self_rated_skills,
            "_quiz_verified": payload.quiz_verified_skills,
            "_previous_trainings": payload.previous_trainings,
        }

        # Run the pipeline first so the run's recommendations can be
        # persisted with the profile: the Dataset-5 row records which course
        # ids were suggested for THIS specific registration.
        gaps = self._gap_service.compute_gaps_for_employee(employee)
        recommendations, unmatched_gaps = (
            self._recommendation_service.recommend_for_employee(employee, top_n)
        )

        # Serialized row - packed fields back to their CSV string form.
        csv_row = {
            "employee_id": employee_id,
            "name": employee["name"],
            "role_id": payload.role_id,
            "designation": employee["designation"],
            "department": employee["department"],
            "current_assignment": payload.current_assignment or "",
            "educational_qualifications": payload.educational_qualifications or "",
            "work_experience_years": payload.work_experience_years,
            "previous_trainings": ",".join(payload.previous_trainings),
            "self_rated_skills": ";".join(
                f"{k}:{v}" for k, v in payload.self_rated_skills.items()
            ),
            "quiz_verified_skills": ";".join(
                f"{k}:{v}" for k, v in payload.quiz_verified_skills.items()
            ),
            "registered_at": registered_at,
            "recommended_courses": ",".join(
                course["course_id"] for course in recommendations
            ),
        }

        # Persist (file) + hot-add (memory).
        save_real_employee(csv_row)
        self._repos.employees.add_real(employee)

        return employee, gaps, recommendations, unmatched_gaps
