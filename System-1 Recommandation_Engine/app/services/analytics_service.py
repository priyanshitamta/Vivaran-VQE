"""
Admin analytics service - the dual-segment view.

The admin needs two SEPARATE segments plus a combined view:

  - ``synthetic``: the original 805-person training dataset (Dataset-3).
    This is the baseline the engine was validated against.
  - ``real``: the accumulating Dataset-5 registrations. Kept separate so
    live registrations never pollute the synthetic baseline's statistics.
  - ``combined``: both together (full workforce).

Every segment runs the same aggregation over its employee subset, so the
admin can compare apples to apples. Gap computations reuse GapService -
one engine, no duplicated logic.
"""

from __future__ import annotations

from typing import Optional

from app.data.repositories import Repositories


class AnalyticsService:
    def __init__(self, repos: Repositories, gap_service) -> None:
        self._repos = repos
        self._gap_service = gap_service

    # ------------------------------------------------------------------
    # Per-segment aggregation
    # ------------------------------------------------------------------

    def workforce(self, source: Optional[str] = None) -> dict:
        """
        Aggregate analytics over one employee segment.

        ``source``: ``"synthetic"`` (Dataset-3 only), ``"real"`` (Dataset-5
        only), or ``None`` for the combined view.
        """
        if source == "synthetic":
            employees = self._repos.employees.all_synthetic()
        elif source == "real":
            employees = self._repos.employees.all_real()
        else:
            employees = self._repos.employees.all()

        dept_counts: dict[str, int] = {}
        role_counts: dict[str, int] = {}
        total_gap_magnitude = 0
        total_gap_count = 0
        fully_qualified = 0
        s014_gapped = 0

        for employee in employees:
            dept = employee.get("department") or "unknown"
            dept_counts[dept] = dept_counts.get(dept, 0) + 1
            role = employee.get("role_id") or "unknown"
            role_counts[role] = role_counts.get(role, 0) + 1

            gaps = self._gap_service.compute_gaps_for_employee(employee)
            total_gap_count += len(gaps)
            total_gap_magnitude += sum(g["gap"] for g in gaps)
            if not gaps:
                fully_qualified += 1
            if any(g["skill_id"] == "S014" for g in gaps):
                s014_gapped += 1

        n = len(employees)
        return {
            "segment": source or "combined",
            "count": n,
            "by_department": self._top_counts(dept_counts),
            "by_role": self._top_counts(role_counts),
            "avg_gaps_per_employee": self._safe_round(total_gap_count / n) if n else 0.0,
            "avg_total_gap_magnitude": (
                self._safe_round(total_gap_magnitude / n) if n else 0.0
            ),
            "fully_qualified_count": fully_qualified,
            "s014_gapped_count": s014_gapped,
        }

    def registrations(self) -> list[dict]:
        """Every real employee (Dataset-5), newest first, with metadata for
        the admin registration log."""
        rows = [
            {
                "employee_id": e["employee_id"],
                "name": e["name"],
                "role_id": e["role_id"],
                "designation": e["designation"],
                "department": e["department"],
                "work_experience_years": e.get("work_experience_years"),
                "registered_at": e.get("registered_at"),
            }
            for e in self._repos.employees.all_real()
        ]
        rows.sort(key=lambda r: r.get("registered_at") or "", reverse=True)
        return rows

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _top_counts(counter: dict[str, int], limit: int = 10) -> list[dict]:
        return [
            {"key": key, "count": count}
            for key, count in sorted(
                counter.items(), key=lambda kv: (-kv[1], kv[0])
            )[:limit]
        ]

    @staticmethod
    def _safe_round(value: float, digits: int = 2) -> float:
        return round(value, digits)
