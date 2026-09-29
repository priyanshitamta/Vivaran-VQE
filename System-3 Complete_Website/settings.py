"""
Vivaran-VQE (System 3) - central settings.

Paths/ports/constants for the web shell, all overridable via VIVARAN_*
environment variables (the launcher sets the machine-specific ones). Kept
separate from ``db.py`` so imports stay light.
"""

from __future__ import annotations

import os
import shutil

import envcompat  # noqa: F401  (maps old ANTAHAI_* env vars to VIVARAN_*)
from pathlib import Path

# System-3 root (this file lives there).
S3_ROOT = Path(__file__).resolve().parent

# --- neighbouring systems ---------------------------------------------------
# System-1 Recommendation Engine is a separate FastAPI process we call over
# HTTP (it owns the datasets and the pipeline). System-2 MCQ Generator is a
# venv + utils we import in-process (it exposes no JSON API), so we only need
# its project root here.
S1_URL = os.environ.get("VIVARAN_S1_URL", "http://127.0.0.1:8000").rstrip("/")

_DEFAULT_S2 = S3_ROOT.parent / "System-2 MCQ_Generator"
S1_ROOT = Path(os.environ.get(
    "VIVARAN_S1_ROOT", str(S3_ROOT.parent / "System-1 Recommandation_Engine")
))
S2_ROOT = Path(os.environ.get("VIVARAN_S2_ROOT", str(_DEFAULT_S2)))

# ffmpeg is shelled out to by S2's utils/video_processor.py via bare `ffmpeg`,
# so its bin dir must be prepended to PATH for our process. Env override wins;
# otherwise look it up (or fall back to the known WinGet install path).
_DEFAULT_FFMPEG = Path(
    r"C:\Users\parth\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.1-full_build\bin"
)
def _resolve_ffmpeg_dir() -> Path:
    env = os.environ.get("VIVARAN_FFMPEG_DIR")
    if env:
        return Path(env)
    if _DEFAULT_FFMPEG.joinpath("ffmpeg.exe").exists():
        return _DEFAULT_FFMPEG
    found = shutil.which("ffmpeg")
    if found:
        return Path(found).parent
    raise RuntimeError(
        "ffmpeg not found. Install it or set VIVARAN_FFMPEG_DIR to the bin "
        "directory containing ffmpeg.exe."
    )

FFMPEG_DIR = _resolve_ffmpeg_dir()

# --- data files -------------------------------------------------------------
DATASET6_CSV = Path(
    os.environ.get("VIVARAN_DATASET6", str(S3_ROOT / "Dataset-6 Youtube_Links.csv"))
)
UPLOAD_DIR = S3_ROOT / "uploads"   # quiz videos/audio are cleaned up after use

# --- behaviour --------------------------------------------------------------
# How many MCQs one Dataset-6 video yields for a course quiz.
QUIZ_QUESTION_COUNT = int(os.environ.get("VIVARAN_QUIZ_QUESTIONS", "5"))
# Server port for this web shell (System 1 keeps its own on :8000).
FLASK_HOST = os.environ.get("VIVARAN_HOST", "127.0.0.1")
FLASK_PORT = int(os.environ.get("VIVARAN_PORT", "8080"))

# S1 "recommendation" endpoints we relay through.
def s1_url(path: str) -> str:
    return f"{S1_URL}{path}"

META_ENDPOINTS = ("skills", "roles", "courses", "unique-values")

# YouTube player clients yt-dlp tries, in order, when downloading Dataset-6
# links. The default `android` is the one that currently slips past YouTube's
# "Sign in to confirm you're not a bot" anonymous-web block; if YouTube locks
# it down later, set VIVARAN_YT_CLIENT="ios,tv,web" (etc.) to switch without
# touching code.
YT_CLIENTS = [
    c.strip()
    for c in os.environ.get("VIVARAN_YT_CLIENT", "android").split(",")
    if c.strip()
]

# Quiz retake limit is a UI-level notice only (per spec) - no backend
# enforcement. This constant is what the copy on the results page quotes.
ONE_ATTEMPT_NOTICE = (
    "Please note: each course's quiz can only be attempted once per "
    "recommendation cycle."
)

# --- learner platform ---------------------------------------------------------
# Pass mark for a course quiz (percentage). A pass completes the course.
PASS_THRESHOLD = float(os.environ.get("VIVARAN_PASS_THRESHOLD", "60"))
# Roadmap size bounds; the actual count is decided by the recommender (it stops
# once the learner's role gaps are covered).
ROADMAP_MIN_COURSES = int(os.environ.get("VIVARAN_ROADMAP_MIN", "3"))
ROADMAP_MAX_COURSES = int(os.environ.get("VIVARAN_ROADMAP_MAX", "6"))
# Allowed e-mail domain for learner registration.
GOV_EMAIL_DOMAIN = os.environ.get("VIVARAN_EMAIL_DOMAIN", "gov.in").lower()

# Course progress milestones (percent).
PROGRESS_STARTED = 10
PROGRESS_VIDEO_WATCHED = 50
PROGRESS_QUIZ_TAKEN = 75
PROGRESS_COMPLETED = 100
