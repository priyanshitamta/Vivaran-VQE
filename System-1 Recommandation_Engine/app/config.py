"""
Central configuration for the System 1 Recommendation Engine.

All paths, filenames, and tunable constants live here so the rest of the
codebase has exactly one place to look. Values can be overridden via
environment variables (prefixed ``S1_``) so the module stays portable when
integrated into the larger full-stack platform later.

Assumptions (documented per the spec):
- Data files live in this repo root, next to this package.
- Default recommendation count is 5 unless the client passes ``top_n``.
- Tier 2 (semantic embeddings) is disabled by default so the service runs
  without the heavy torch/sentence-transformers install; enable via
  ``S1_ENABLE_EMBEDDINGS=1`` once requirements-tier2.txt is installed.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Repo root = parent of the ``app/`` package directory.
BASE_DIR = Path(__file__).resolve().parent.parent

# Where the four dataset CSVs live (now in datasets/ subdirectory).
DATA_DIR = Path(os.environ.get("S1_DATA_DIR", BASE_DIR / "datasets"))

DATASET_FILES = {
    "skills": DATA_DIR / "Dataset-1_Skill_Taxonomy.csv",
    "required_competency": DATA_DIR / "Dataset-2_Required_Competency.csv",
    "employees": DATA_DIR / "Dataset-3_Syn_Employee_Profiles.csv",
    "courses": DATA_DIR / "Dataset-4_Course_Catalouge.csv",
    # Dataset-5 is the real-employee accumulation store: written by the
    # register endpoint, loaded at startup alongside 1-4. May not exist yet.
    "real_employees": DATA_DIR / "Dataset-5_Real_Employee_Profiles.csv",
}

# Column order used to serialize real employees into Dataset-5 (mirrors
# Dataset-3 plus a registration timestamp).
REAL_EMPLOYEE_COLUMNS = [
    "employee_id",
    "name",
    "role_id",
    "designation",
    "department",
    "current_assignment",
    "educational_qualifications",
    "work_experience_years",
    "previous_trainings",
    "self_rated_skills",
    "quiz_verified_skills",
    "registered_at",
    # Comma-packed course_ids recommended for that employee on registration.
    "recommended_courses",
]

# ---------------------------------------------------------------------------
# API behaviour
# ---------------------------------------------------------------------------

# Default number of course recommendations returned when ``top_n`` is omitted.
DEFAULT_TOP_N = 5

# Hard ceiling on ``top_n`` to keep responses bounded.
MAX_TOP_N = 50

# ---------------------------------------------------------------------------
# CORS - enabled from day one because this module will be called from a
# full-stack frontend on a different origin. Lock down to specific origins
# before production; "*" is a permissive default for local development.
# ---------------------------------------------------------------------------

CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("S1_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

# ---------------------------------------------------------------------------
# Tier 2 - semantic embeddings (optional)
#
# DORMANT NLP CODE. This block is inactive by default - the engine runs
# entirely on Tier 1 tag-overlap without it. Kept for future use: when enabled
# it blends sentence-transformer cosine similarity into the course ranking.
# To activate: install context/requirements-tier2.txt, then set
# S1_ENABLE_EMBEDDINGS=1.
# ---------------------------------------------------------------------------

# Whether to compute and blend embedding-based similarity into the ranking.
ENABLE_EMBEDDINGS = os.environ.get("S1_ENABLE_EMBEDDINGS", "0") == "1"

# Sentence-transformers model used for course/skill description embeddings.
# ~90 MB download on first use.
EMBEDDING_MODEL = os.environ.get("S1_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# Weight given to the semantic-similarity score when blending with the Tier 1
# tag-overlap score (0..1). 0.35 keeps tag overlap dominant while still letting
# semantically related-but-untagged courses surface.
SEMANTIC_WEIGHT = float(os.environ.get("S1_SEMANTIC_WEIGHT", "0.35"))

# Cosine-similarity floor below which a course is not considered semantically
# relevant, even if the blend would otherwise admit it.
SEMANTIC_THRESHOLD = float(os.environ.get("S1_SEMANTIC_THRESHOLD", "0.30"))

# ---------------------------------------------------------------------------
# Data-layer constants
# ---------------------------------------------------------------------------

# Maximum proficiency level across all datasets (levels are 1..MAX_LEVEL).
MAX_LEVEL = 3
