# ---------------------------------------------------------------------------
# DORMANT NLP CODE - Tier 2 semantic embeddings (Stage C upgrade).
#
# This entire module is INACTIVE by default. The engine never reaches it unless
# config.ENABLE_EMBEDDINGS is set (S1_ENABLE_EMBEDDINGS=1) AND the heavy
# dependencies in context/requirements-tier2.txt are installed. When enabled it
# re-ranks Tier 1 tag-matched courses using sentence-transformer cosine
# similarity. Kept for future use (e.g. a resume-parsing NLP pipeline that
# extracts skills and feeds the same matching algorithm). The service degrades
# gracefully to Tier 1 when disabled.
# ---------------------------------------------------------------------------
"""
Tier 2 - optional semantic-matching engine (Stage C upgrade).

Embeddings are precomputed ONCE at startup (784 course descriptions -> an
in-memory matrix) and only *queried* per request, per the spec. The heavy
dependencies (``sentence-transformers``, ``torch``, ``numpy``) are imported
lazily inside methods so this module can sit in the codebase without them
installed - the service degrades gracefully to Tier 1 when embeddings are
disabled (see ``app.config.ENABLE_EMBEDDINGS``).
"""

from __future__ import annotations

import logging
from typing import Optional

from app import config

logger = logging.getLogger(__name__)


class EmbeddingEngine:
    """Wraps the sentence-transformer model + course embedding matrix."""

    def __init__(self, model_name: str = config.EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self._model = None  # lazy
        self._course_ids: list[str] = []
        self._course_matrix = None  # (n_courses, dim) numpy array
        self._ready = False

    # ------------------------------------------------------------------
    # Build time (once per process)
    # ------------------------------------------------------------------

    def build(self, course_rows: list[dict]) -> None:
        """
        Embed every course description once. ``course_rows`` is the full
        course catalogue from the repository. Called at app startup when
        Tier 2 is enabled; must complete before serving recommendations.
        """
        from sentence_transformers import SentenceTransformer  # guarded import

        descriptions = [row.get("description") or "" for row in course_rows]
        self._course_ids = [row["course_id"] for row in course_rows]
        logger.info("Loading embedding model %s ...", self.model_name)
        self._model = SentenceTransformer(self.model_name)
        logger.info("Embedding %d course descriptions ...", len(descriptions))
        self._course_matrix = self._model.encode(descriptions, show_progress_bar=False)
        self._ready = True
        logger.info("Course embedding matrix ready: %s", self._course_matrix.shape)

    # ------------------------------------------------------------------
    # Query time (per request)
    # ------------------------------------------------------------------

    def _skill_embedding(self, text: str):
        """Embed one short text (a gap skill's name + description)."""
        assert self._model is not None, "build() must run before encoding"
        return self._model.encode([text], show_progress_bar=False)[0]

    def best_cosine_for_course(self, skill_embeddings, course_idx: int) -> float:
        """
        Highest cosine similarity between this course's embedding and any of
        the employee's gap-skill embeddings. Returns 0.0 if not ready.
        """
        if not self._ready or self._course_matrix is None:
            return 0.0
        import numpy as np  # guarded import

        course_vec = self._course_matrix[course_idx]
        similarities = [
            float(np.dot(course_vec, e) / (np.linalg.norm(course_vec) * np.linalg.norm(e) + 1e-9))
            for e in skill_embeddings
        ]
        return max(similarities)

    def skill_texts(self, gaps: list[dict], skills_repo) -> list[str]:
        """
        Build the short text per gap skill: "{skill_name}: {description}".
        Used to produce the embeddings queried per request.
        """
        texts: list[str] = []
        for gap in gaps:
            skill = skills_repo.get(gap["skill_id"])
            desc = skill["description"] if skill else ""
            texts.append(f"{gap['skill_name']}: {desc}".strip())
        return texts

    def encode_skill_texts(self, texts: list[str]) -> list:
        """Embed a list of skill texts (a list of numpy vectors)."""
        assert self._model is not None, "build() must run before encoding"
        if not texts:
            return []
        return list(self._model.encode(texts, show_progress_bar=False))

    @property
    def is_ready(self) -> bool:
        return self._ready

    # Convenience lookup used by the recommendation service.
    def course_index(self, course_id: str) -> Optional[int]:
        try:
            return self._course_ids.index(course_id)
        except ValueError:
            return None
