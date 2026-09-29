"""
Stage A - data loading layer.

Responsible for:
  1. Reading the four dataset CSVs from disk (paths from ``app.config``).
  2. Stripping whitespace from all column names and string cell values
     (the source CSVs contain padding whitespace).
  3. Converting numeric columns to ``int``.
  4. Parsing the semicolon/comma-packed fields into proper Python structures
     (dicts / lists) so business logic never re-parses strings.
  5. Validating foreign-key references defensively, logging (not raising) on
     any dangling reference - the datasets are clean today but may not stay
     that way if edited later.

Everything downstream consumes the cleaned DataFrames via ``repositories.py``;
raw CSV parsing never leaks into business logic.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pandas as pd

from app import config

logger = logging.getLogger(__name__)

# Guard for CSV writes (Dataset-5 appends and skill-update rewrites must not
# interleave).
_csv_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------


def _strip_cell(value: Any) -> Any:
    """Strip whitespace from strings; pass non-strings through unchanged."""
    if isinstance(value, str):
        return value.strip()
    return value


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Strip column names and string cells; convert numeric columns to int."""
    df = df.rename(columns=lambda c: c.strip())
    # NaN cells would otherwise poison int conversion; pandas handles via
    # nullable Int64 in newer versions, but these datasets have no NaN, so a
    # plain fill + astype is the simplest reasonable choice.
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(_strip_cell)
    return df


def read_csv(path: Path) -> pd.DataFrame:
    """Read one CSV and return a cleaned DataFrame."""
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    df = clean_dataframe(df)
    return df


# ---------------------------------------------------------------------------
# Packed-field parsers (used at load time, per the spec)
# ---------------------------------------------------------------------------


def parse_skill_levels(value: str) -> dict[str, int]:
    """
    Parse a semicolon-separated ``skill_id:level`` string into a dict.

    Example: ``"S001:2;S002:1;S003:2"`` -> ``{"S001": 2, "S002": 1, "S003": 2}``
    """
    result: dict[str, int] = {}
    # Tolerate NaN (empty cells read back as float NaN from pandas)
    if not value or not isinstance(value, str) or not value.strip():
        return result
    for pair in value.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(":")
        if len(parts) != 2:
            logger.warning("Skipping malformed skill-level pair: %r", pair)
            continue
        skill_id, level = parts[0].strip(), parts[1].strip()
        try:
            result[skill_id] = int(level)
        except ValueError:
            logger.warning("Skipping non-integer level in pair: %r", pair)
    return result


def parse_csv_list(value: str) -> list[str]:
    """Parse a comma-separated string into a cleaned list of ids."""
    # Tolerate NaN (empty cells read back as float NaN from pandas)
    if not value or not isinstance(value, str) or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def pack_skill_levels(levels: dict[str, int]) -> str:
    """Serialize ``{skill_id: level}`` back to the packed CSV form
    (``"S001:2;S002:1"``) - the inverse of ``parse_skill_levels``, used when
    persisting skill updates to disk."""
    return ";".join(f"{k}:{v}" for k, v in levels.items())


# ---------------------------------------------------------------------------
# Numeric columns per dataset (converted to int at load time)
# ---------------------------------------------------------------------------

# Numeric columns per dataset, converted to int at load time.
# Note: Dataset-1 ``proficiency_levels`` is deliberately excluded - it is a
# string constant ("1,2,3"), not a per-skill level.
INT_COLUMNS = {
    "required_competency": ["required_level", "priority_weight"],
    "employees": ["work_experience_years"],
    "real_employees": ["work_experience_years"],
    "courses": ["target_level", "duration_minutes"],
}


def convert_int_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Convert listed columns to int; absent columns are tolerated. Missing
    values (NaN) become 0 so real-employee rows with optional fields left
    blank load cleanly."""
    for col in columns:
        if col in df.columns:
            df[col] = (
                pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
            )
    return df


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_all() -> dict[str, pd.DataFrame]:
    """
    Load and clean all datasets.

    Returns a dict keyed by ``"skills" | "required_competency" | "employees"
    | "courses" | "real_employees"``. Raises ``FileNotFoundError`` with a
    clear message if any of the four core datasets is missing.

    ``real_employees`` (Dataset-5) is optional: if the file does not exist
    yet, an empty frame with the same schema is returned so the rest of the
    pipeline is untouched.
    """
    loaded: dict[str, pd.DataFrame] = {}
    for key, path in config.DATASET_FILES.items():
        if not path.exists():
            if key == "real_employees":
                loaded[key] = pd.DataFrame(
                    columns=config.REAL_EMPLOYEE_COLUMNS
                )
                logger.info(
                    "No Dataset-5 yet (%s) - starting with 0 real employees",
                    path.name,
                )
                continue
            raise FileNotFoundError(
                f"Dataset file not found: {path.name} (expected at {path}). "
                f"Check config.DATASET_FILES."
            )
        df = read_csv(path)
        df = convert_int_columns(df, INT_COLUMNS.get(key, []))
        loaded[key] = df
        logger.info("Loaded %s: %d rows from %s", key, len(df), path.name)
    return loaded


def save_real_employee(row: dict[str, Any]) -> None:
    """
    Append one real employee row to Dataset-5 (thread-safe).

    The file is created with its header on first write. CSV is the
    persistence store for real registrations; a database can later replace
    this function without changing its callers.
    """
    path = config.DATASET_FILES["real_employees"]
    # Normalize to the canonical column order, tolerating missing values.
    out = {col: row.get(col, "") for col in config.REAL_EMPLOYEE_COLUMNS}
    new_file = not path.exists()
    with _csv_write_lock:
        df = pd.DataFrame([out], columns=config.REAL_EMPLOYEE_COLUMNS)
        df.to_csv(
            path,
            mode="a",
            header=new_file,
            index=False,
            encoding="utf-8",
        )


def update_employee_row(
    path: Path, employee_id: str, updates: dict[str, Any]
) -> bool:
    """
    Update one row of a CSV, matched by ``employee_id``, in place.

    Used by the skill-update endpoint to persist quiz-verified / self-rated
    level changes to disk (System 2 writes back verified levels; a restart
    must not lose them). ``updates`` maps column name -> packed string value
    (e.g. ``{"quiz_verified_skills": "S001:3;S002:2"}``). Returns True when a
    row was found and rewritten, False when the employee is not in the file.

    Thread-safe: serialised under the same lock as Dataset-5 appends.
    """
    with _csv_write_lock:
        if not path.exists():
            return False
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
        df = clean_dataframe(df)
        mask = df["employee_id"] == employee_id
        if not mask.any():
            return False
        for col, value in updates.items():
            if col in df.columns:
                # An all-blank packed column (e.g. quiz_verified_skills in a
                # file where no row has one yet) loads as float64 (all-NaN).
                # Cast to object first so string values assign cleanly -
                # pandas otherwise warns and, in a future version, refuses.
                df[col] = df[col].astype(object)
                df.loc[mask, col] = value
        df.to_csv(path, index=False, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Defensive FK validation (log-only)
# ---------------------------------------------------------------------------


def validate_references(loaded: dict[str, pd.DataFrame]) -> list[str]:
    """
    Check all foreign-key relationships defined in the spec. Returns a list
    of human-readable problems (empty when everything is clean). Never raises
    - the engine must still run on dirty data, just with gaps/recommendations
    computed from what *is* consistent.
    """
    problems: list[str] = []

    skills = set(loaded["skills"]["skill_id"])
    roles = set(loaded["required_competency"]["role_id"])
    courses = set(loaded["courses"]["course_id"])

    d2 = loaded["required_competency"]
    d3 = loaded["employees"]
    d4 = loaded["courses"]

    # D2.skill_id -> D1.skill_id
    bad = sorted(set(d2["skill_id"]) - skills)
    if bad:
        problems.append(f"Dataset-2 skill_id(s) not in taxonomy: {bad}")

    # D4.skill_tags -> D1.skill_id
    bad = sorted({t for row in d4["skill_tags"] for t in parse_csv_list(row)} - skills)
    if bad:
        problems.append(f"Dataset-4 skill_tags not in taxonomy: {bad}")

    # D3.role_id -> D2.role_id
    bad = sorted(set(d3["role_id"]) - roles)
    if bad:
        problems.append(f"Dataset-3 role_id(s) not in Dataset-2: {bad}")

    # D3.self_rated_skills / quiz_verified_skills -> D1.skill_id
    for col in ("self_rated_skills", "quiz_verified_skills"):
        bad = sorted({k for row in d3[col] for k in parse_skill_levels(row)} - skills)
        if bad:
            problems.append(f"Dataset-3 {col} skill(s) not in taxonomy: {bad}")

    # D3.previous_trainings -> D4.course_id
    bad = sorted(
        {t for row in d3["previous_trainings"] for t in parse_csv_list(row)} - courses
    )
    if bad:
        problems.append(f"Dataset-3 previous_trainings not in catalogue: {bad}")

    if not problems:
        logger.info("FK validation passed - all references consistent.")
    else:
        for p in problems:
            logger.warning("FK validation: %s", p)
    return problems
