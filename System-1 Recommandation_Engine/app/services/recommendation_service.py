"""
Stage C - course matching / recommendation service, plus Stage D handling.

Two-tier ranking per the spec:

Tier 1 (always): tag-overlap scoring.
  - Candidate set = every course tagged with at least one of the employee's
    gap skills.
  - ``matched_weight`` = sum of ``priority_weight`` over the gap skills the
    course covers.
  - Level relevance = how well the course's ``target_level`` matches the
    highest ``required_level`` among its matched gaps:
        relevance = 1 / (1 + |target_level - max_required_level|)
        (1.0 when exact, decreasing as the course overshoots/undershoots)
  - ``tag_score = matched_weight * level_relevance``

Tier 2 (DORMANT NLP - optional, config-gated): semantic blend.
  - If embeddings are enabled and built, each candidate course gets
    ``cosine`` = highest cosine similarity between its description embedding
    and the employee's gap-skill embeddings.
  - The tag score is normalised to [0, 1] across candidates so the two scales
    are comparable, then blended:
        final = (1 - w) * normalised_tag_score + w * cosine,  w = SEMANTIC_WEIGHT

Stage D (required behaviour): every gap skill with zero course coverage is
reported explicitly via ``unmatched_gaps`` - never silently dropped, never
crashed on, never filled with a fabricated course.
"""

from __future__ import annotations

from typing import Optional

from app import config
from app.data.repositories import Repositories
# DORMANT NLP import (Tier 2 semantic embeddings) - unused while
# config.ENABLE_EMBEDDINGS is off. Kept for future use.
from app.services.embeddings import EmbeddingEngine


class RecommendationService:
    def __init__(
        self,
        repos: Repositories,
        # DORMANT NLP wiring (Tier 2 embeddings) - stays None while Tier 2 is
        # disabled. Kept for future use.
        embeddings: Optional[EmbeddingEngine] = None,
    ) -> None:
        self._repos = repos
        self._embeddings = embeddings  # DORMANT NLP - None unless Tier 2 enabled

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def recommend(
        self, employee_id: str, top_n: int = config.DEFAULT_TOP_N
    ) -> Optional[tuple[list[dict], list[dict]]]:
        """
        Return ``(recommendations, unmatched_gaps)`` for an employee, or
        ``None`` if the employee does not exist (404 at the API layer).

        ``recommendations`` is a list of dicts shaped like the
        ``Recommendation`` schema, ranked best-first and truncated to
        ``top_n``. ``unmatched_gaps`` matches the ``UnmatchedGap`` schema.
        """
        employee = self._repos.employees.get(employee_id)
        if employee is None:
            return None
        return self.recommend_for_employee(employee, top_n)

    def recommend_for_employee(
        self, employee: dict, top_n: int = config.DEFAULT_TOP_N
    ) -> tuple[list[dict], list[dict]]:
        """
        The shared core: rank courses for an arbitrary employee record dict.

        Powers both ``recommend`` (Dataset-3 lookup) and the on-the-fly
        compute endpoint for employees who are NOT in the dataset. The record
        needs the same parsed shape the repositories produce (``role_id``,
        ``_self_rated``, ``_quiz_verified``).
        """
        # Validate via the gap service so scoring and unmatched-reporting
        # share one source of truth (employee existence + gap list).
        from app.services.gap_service import GapService

        gaps = GapService(self._repos).compute_gaps_for_employee(employee)

        top_n = max(1, min(top_n, config.MAX_TOP_N))
        recommendations, unmatched_gaps = self._rank_courses(gaps, top_n)
        return recommendations, unmatched_gaps

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _rank_courses(
        self, gaps: list[dict], top_n: int
    ) -> tuple[list[dict], list[dict]]:
        unmatched_gaps: list[dict] = []
        matched_by_skill: dict[str, list[dict]] = {}
        skill_max_required: dict[str, int] = {}

        # --- Pass 1: gather candidates + build Stage D report -------------
        for gap in gaps:
            skill_id = gap["skill_id"]
            skill_max_required[skill_id] = gap["required_level"]
            courses = self._repos.courses.get_courses_for_skill(skill_id)
            if not courses:
                unmatched_gaps.append(self._unmatched_entry(gap))
            else:
                matched_by_skill[skill_id] = courses

        if not matched_by_skill:
            return [], unmatched_gaps

        # --- Pass 2: score every candidate course -------------------------
        # course_id -> running score pieces
        scores: dict[str, dict] = {}

        for skill_id, courses in matched_by_skill.items():
            weight = self._priority(gaps, skill_id)
            max_required = skill_max_required[skill_id]
            for course in courses:
                entry = scores.setdefault(
                    course["course_id"],
                    {
                        "course": course,
                        "matched_weight": 0,
                        "max_required": max_required,
                        "matched_skills": [],
                    },
                )
                entry["matched_weight"] += weight
                entry["max_required"] = max(entry["max_required"], max_required)
                entry["matched_skills"].append(skill_id)

        # Normalise matched_skills to descending priority order per schema.
        for entry in scores.values():
            entry["matched_skills"].sort(
                key=lambda s: -self._priority(gaps, s)
            )

        tag_scores = {
            cid: entry["matched_weight"]
            * self._level_relevance(
                entry["course"]["target_level"], entry["max_required"]
            )
            for cid, entry in scores.items()
        }

        # --- Tier 2: DORMANT NLP semantic blend ---------------------------
        # Inactive unless config.ENABLE_EMBEDDINGS=1 AND the embedding engine
        # built successfully. While off (the default), ``embed`` is None and
        # ranking is pure Tier 1 tag-overlap. Kept for future use.
        embed = (
            self._embeddings
            if config.ENABLE_EMBEDDINGS and self._embeddings and self._embeddings.is_ready
            else None
        )
        skill_embeddings: list = []
        if embed is not None:
            texts = embed.skill_texts(gaps, self._repos.skills)
            skill_embeddings = embed.encode_skill_texts(texts)
            # cache course index lookups
            index_map = {cid: embed.course_index(cid) for cid in scores}

        max_tag = max(tag_scores.values()) if tag_scores else 1.0

        ranked: list[tuple[float, dict]] = []
        for cid, entry in scores.items():
            tag_score = tag_scores[cid]
            norm_tag = tag_score / max_tag if max_tag else 0.0
            final = norm_tag
            if embed is not None and skill_embeddings:
                idx = index_map.get(cid)
                if idx is not None:
                    cosine = embed.best_cosine_for_course(skill_embeddings, idx)
                    final = (
                        (1.0 - config.SEMANTIC_WEIGHT) * norm_tag
                        + config.SEMANTIC_WEIGHT * cosine
                    )
            ranked.append((final, entry))

        # --- Pass 3: rank + truncate --------------------------------------
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["course"]["course_id"]))
        top = ranked[:top_n]

        recommendations = [
            self._recommendation_dict(entry, final_score)
            for final_score, entry in top
        ]
        return recommendations, unmatched_gaps

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _priority(gaps: list[dict], skill_id: str) -> int:
        for gap in gaps:
            if gap["skill_id"] == skill_id:
                return gap["priority_weight"]
        return 0

    @staticmethod
    def _level_relevance(target_level: int, required_level: int) -> float:
        """1.0 when the course level matches the required level; decays with
        distance in either direction."""
        return 1.0 / (1.0 + abs(target_level - required_level))

    def _unmatched_entry(self, gap: dict) -> dict:
        skill_name = gap["skill_name"]
        return {
            "skill_id": gap["skill_id"],
            "skill_name": skill_name,
            "status": "no_courses_available",
            # Exact wording from the spec (CLAUDE.md Stage D) - the frontend
            # and tests both rely on the "flagged for the training team" tail.
            "message": (
                f"No courses currently available for {skill_name}. "
                f"This gap has been flagged for the training team."
            ),
        }

    def _recommendation_dict(self, entry: dict, score: float) -> dict:
        course = entry["course"]
        return {
            "course_id": course["course_id"],
            "course_title": course["course_title"],
            "description": course["description"],
            "target_level": course["target_level"],
            "duration_minutes": course["duration_minutes"],
            "mode": course["mode"],
            "provider": course["provider"],
            "language": course["language"],
            "matched_skills": entry["matched_skills"],
            "score": round(score, 4),
        }
