"""
Vivaran-VQE (System 3) - course catalogue.

One structured representation for every course the platform can recommend:

    Course
     ├── course_id
     ├── title
     ├── description
     ├── category
     ├── designations   (target roles; empty = relevant to all)
     ├── skills         (System-1 skill ids, S001..S016)
     ├── videos         (video URLs fed to System 2's MCQ pipeline)
     └── metadata       (level, duration, mode, provider, language)

Where courses come from (first match wins):

  1. The course document (source of truth). Put it at
     ``data/courses.csv`` / ``data/courses.json`` / ``data/courses.xlsx``
     (or point VIVARAN_COURSES at it). Column names are matched loosely
     ("Course Name", "course_title", "Title" ... all work) - see
     ``_FIELD_ALIASES``. Multiple video links in one cell may be separated by
     whitespace, commas, semicolons or "|".
  2. Fallback while no document is present: System 1's own catalogue
     (Dataset-4, read-only) with each course's video taken from Dataset-6,
     exactly like the original "Take a quiz" flow did - but pinned per course
     (deterministic) so quizzes can be cached. These are flagged
     ``video_source = "dataset6"`` and the UI says so.

Nothing here invents a course: every course is a row of one of those files.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

import settings

logger = logging.getLogger(__name__)

S1_DATASETS = settings.S1_ROOT / "datasets"

# ---------------------------------------------------------------------------
# Skill taxonomy (System 1 Dataset-1) + keyword hints for documents that
# describe courses in prose instead of skill ids.
# ---------------------------------------------------------------------------

SKILL_KEYWORDS: dict[str, list[str]] = {
    "S001": ["artificial intelligence", "machine learning", " ai ", " ai/", " ml ", "deep learning",
             "generative", "chatgpt", "llm", "neural", "data science"],
    "S002": ["communication", "presentation", "public speaking", "writing", "drafting"],
    "S003": ["digital public infrastructure", "dpi", "aadhaar", "upi", "e-governance",
             "egovernance", "digital india", "digilocker", "e-office", "eoffice"],
    "S004": ["cyber", "security", "phishing", "malware", "privacy", "data protection"],
    "S005": ["visualization", "visualisation", "dashboard", "chart", "power bi", "tableau",
             "analytics", "excel", "data analysis"],
    "S006": ["digital signature", "e-sign", "esign", "pki", "dsc"],
    "S007": ["price statistic", "inflation", "cpi", "wpi", "price index", "index number"],
    "S008": ["leadership", "leader", "team building", "motivat"],
    "S009": ["government cloud", "meghraj", "gov cloud", "govt cloud"],
    "S010": ["decision", "problem solving", "critical thinking"],
    "S011": ["project management", "programme management", "monitoring", "evaluation", "planning"],
    "S012": ["change management", "transformation", "reform"],
    "S013": ["ethic", "integrity", "accountability", "rti", "code of conduct", "vigilance"],
    "S014": ["cloud computing", "cloud"],
    "S015": ["sampling", "survey", "sample", "census", "statistic", "nsso", "nss ", "regression",
             "probability", "econometric"],
    "S016": ["time management", "productivity", "prioriti"],
}

# Area of Experience -> the skills that field leans on. Used to (a) rank
# courses that fit the learner's professional field higher and (b) explain
# why a course was picked.
AREAS_OF_EXPERIENCE: dict[str, list[str]] = {
    "Data Science & Analytics": ["S001", "S005", "S015"],
    "Statistics & Surveys": ["S015", "S007", "S005"],
    "Economic & Price Statistics": ["S007", "S015", "S010"],
    "Digital Governance & e-Services": ["S003", "S006", "S009", "S004"],
    "IT, Cloud & Cybersecurity": ["S004", "S009", "S014", "S001"],
    "Administration & Management": ["S008", "S010", "S011", "S012", "S016"],
    "Policy, Ethics & Communication": ["S002", "S013", "S010", "S012"],
}


def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return [
            {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(handle, skipinitialspace=True)
        ]


_SKILLS: dict[str, dict] | None = None


def skills() -> dict[str, dict]:
    """skill_id -> {skill_id, skill_name, category} from Dataset-1."""
    global _SKILLS
    if _SKILLS is None:
        try:
            rows = _read_csv(S1_DATASETS / "Dataset-1_Skill_Taxonomy.csv")
            _SKILLS = {r["skill_id"]: r for r in rows if r.get("skill_id")}
        except OSError:
            logger.warning("Dataset-1 not found under %s", S1_DATASETS)
            _SKILLS = {}
    return _SKILLS


def skill_name(skill_id: str) -> str:
    return (skills().get(skill_id) or {}).get("skill_name", skill_id)


def infer_skills(text: str) -> list[str]:
    """Skill ids whose keywords appear in ``text`` (keywords match at a word
    start, so "esign" does not fire on "design")."""
    hay = f" {text.lower()} "
    found = []
    for sid, words in SKILL_KEYWORDS.items():
        for word in words:
            w = word.strip()
            if re.search(r"(?<![a-z0-9])" + re.escape(w) + (r"(?![a-z0-9])" if word.endswith(" ") else ""), hay):
                found.append(sid)
                break
    return found


# ---------------------------------------------------------------------------
# Course document loader (flexible column names)
# ---------------------------------------------------------------------------

_FIELD_ALIASES: dict[str, list[str]] = {
    "course_id": ["course_id", "course id", "id", "course code", "code", "s.no", "sno", "sr no", "s no"],
    "title": ["course_title", "course title", "course name", "course_name", "title", "name", "course"],
    "description": ["description", "course description", "summary", "about", "details", "overview"],
    "category": ["category", "course category", "domain", "type", "area", "field"],
    "designations": ["designation", "designations", "target designation", "target role",
                     "target_role", "target roles", "role", "roles", "target audience", "audience"],
    "skills": ["skills", "skill_tags", "skill tags", "relevant skills", "competencies", "competency"],
    "videos": ["video links", "video_links", "video link", "video_link", "videos", "video",
               "link", "links", "url", "urls", "youtube", "youtube link"],
    "target_level": ["target_level", "level", "difficulty"],
    "duration_minutes": ["duration_minutes", "duration", "duration (min)", "length"],
    "mode": ["mode"],
    "provider": ["provider", "source", "platform", "offered by"],
    "language": ["language"],
}

_URL_RE = re.compile(r"https?://[^\s,;|\"'<>]+")
_LEVEL_WORDS = {"beginner": 1, "basic": 1, "foundation": 1, "foundational": 1,
                "intermediate": 2, "advanced": 3, "expert": 3}


def _pick(row: dict, field: str) -> str:
    lowered = {k.lower().strip(): v for k, v in row.items() if k}
    for alias in _FIELD_ALIASES[field]:
        if alias in lowered and str(lowered[alias]).strip():
            return str(lowered[alias]).strip()
    return ""


def _split_list(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;,|/\n]", value or "") if p.strip()]


def _level(value: str) -> int:
    value = (value or "").strip().lower()
    if value.isdigit():
        return max(1, min(3, int(value)))
    return _LEVEL_WORDS.get(value, 1)


def _load_document_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["courses"] if isinstance(data, dict) and "courses" in data else data
    if suffix in (".xlsx", ".xls"):
        import pandas as pd  # only needed for spreadsheets
        frame = pd.read_excel(path).fillna("")
        return [{str(k): str(v) for k, v in rec.items()} for rec in frame.to_dict("records")]
    return _read_csv(path)


def _normalise_document(rows: list[dict]) -> list[dict]:
    courses: list[dict] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        title = _pick(row, "title")
        if not title:
            continue
        videos = _URL_RE.findall(_pick(row, "videos"))
        if not videos:  # a link hiding in another column still counts
            videos = _URL_RE.findall(" ".join(str(v) for v in row.values()))
        description = _pick(row, "description")
        category = _pick(row, "category")
        skill_field = _pick(row, "skills")
        skill_ids = re.findall(r"S0\d\d", skill_field)
        if not skill_ids:
            names = {s["skill_name"].lower(): sid for sid, s in skills().items()}
            skill_ids = [names[n.lower()] for n in _split_list(skill_field) if n.lower() in names]
        if not skill_ids:
            skill_ids = infer_skills(" ".join([title, description, category, skill_field]))
        raw_id = _pick(row, "course_id")
        course_id = raw_id if raw_id and not raw_id.isdigit() else f"DOC{index:03d}"
        duration = _pick(row, "duration_minutes")
        courses.append({
            "course_id": course_id,
            "title": title,
            "description": description,
            "category": category,
            "designations": _split_list(_pick(row, "designations")),
            "skills": list(dict.fromkeys(skill_ids)),
            "videos": list(dict.fromkeys(videos)),
            "video_source": "document",
            "target_level": _level(_pick(row, "target_level")),
            "duration_minutes": int(duration) if duration.isdigit() else None,
            "mode": _pick(row, "mode"),
            "provider": _pick(row, "provider"),
            "language": _pick(row, "language"),
        })
    return courses


def _dataset6_links() -> list[str]:
    try:
        rows = _read_csv(settings.DATASET6_CSV)
    except OSError:
        return []
    return [r["Link"] for r in rows if r.get("Link")]


def _load_s1_catalogue() -> list[dict]:
    rows = _read_csv(S1_DATASETS / "Dataset-4_Course_Catalouge.csv")
    links = _dataset6_links()
    courses = []
    for row in rows:
        cid = row.get("course_id")
        if not cid:
            continue
        video = []
        if links:  # pinned per course so the quiz for a course is cacheable
            digest = int(hashlib.sha256(cid.encode()).hexdigest(), 16)
            video = [links[digest % len(links)]]
        tags = [t.strip() for t in (row.get("skill_tags") or "").split(",") if t.strip()]
        category = ", ".join(dict.fromkeys(
            (skills().get(t) or {}).get("category", "") for t in tags if skills().get(t)
        ))
        duration = row.get("duration_minutes") or ""
        courses.append({
            "course_id": cid,
            "title": row.get("course_title") or cid,
            "description": row.get("description") or "",
            "category": category,
            "designations": [],
            "skills": tags,
            "videos": video,
            "video_source": "dataset6",
            "target_level": _level(row.get("target_level") or "1"),
            "duration_minutes": int(duration) if duration.isdigit() else None,
            "mode": row.get("mode") or "",
            "provider": row.get("provider") or "",
            "language": row.get("language") or "",
        })
    return courses


def trainer_course_dict(row: dict) -> dict:
    """A trainer_courses row in the catalogue's course shape."""
    return {
        "course_id": row["course_id"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "designations": row["designations"],
        "skills": row["skills"],
        "videos": row["videos"],
        "video_source": "trainer",
        "target_level": row["target_level"],
        "duration_minutes": row["duration_minutes"],
        "mode": "self-paced",
        "provider": f"Trainer: {row['trainer_username']}",
        "language": "",
        "uploaded_by": row["trainer_id"],
        "approval": row.get("status", "approved"),
        "skill_names": [skill_name(s) for s in row["skills"]],
    }


def _load_trainer_courses() -> list[dict]:
    """APPROVED trainer uploads only: pending/rejected ones never reach the
    catalogue, so learners can't see, be assigned or be recommended them."""
    import db  # local import: db has no dependency on this module
    return [trainer_course_dict(row) for row in db.list_trainer_courses(status="approved")]


def get_any(course_id: str) -> dict | None:
    """Catalogue course, or a trainer upload in ANY approval state (for the
    uploader / reviewers / MCQ generation before approval)."""
    course = get(course_id)
    if course or not course_id.startswith("TRN"):
        return course
    import db
    try:
        row_id = int(course_id[3:])
    except ValueError:
        return None
    row = db.get_trainer_course(row_id)
    return trainer_course_dict(row) if row else None


def document_path() -> Path | None:
    env = os.environ.get("VIVARAN_COURSES")
    if env:
        return Path(env)
    for name in ("courses.json", "courses.csv", "courses.xlsx"):
        candidate = settings.S3_ROOT / "data" / name
        if candidate.exists():
            return candidate
    return None


_LOCK = threading.Lock()
_CATALOGUE: dict | None = None


def _load() -> dict:
    path = document_path()
    if path and path.exists():
        courses = _normalise_document(_load_document_rows(path))
        source = f"course document ({path.name})"
    else:
        courses = _load_s1_catalogue()
        source = "System 1 catalogue (Dataset-4) + Dataset-6 videos"
    trainer_courses = _load_trainer_courses()
    courses = courses + trainer_courses
    if trainer_courses:
        source += f" + {len(trainer_courses)} trainer-uploaded"
    for course in courses:
        course["skill_names"] = [skill_name(s) for s in course["skills"]]
    logger.info("Loaded %d courses from %s", len(courses), source)
    return {"source": source, "courses": courses,
            "by_id": {c["course_id"]: c for c in courses},
            "from_document": bool(path and path.exists())}


def catalogue() -> dict:
    global _CATALOGUE
    with _LOCK:
        if _CATALOGUE is None:
            _CATALOGUE = _load()
        return _CATALOGUE


def reload() -> dict:
    global _CATALOGUE
    with _LOCK:
        _CATALOGUE = None
    return catalogue()


def all_courses() -> list[dict]:
    return catalogue()["courses"]


def get(course_id: str) -> dict | None:
    return catalogue()["by_id"].get(course_id)


def source_label() -> str:
    return catalogue()["source"]


def youtube_embed(url: str) -> str | None:
    """Turn a YouTube watch/short link into an embeddable URL (else None)."""
    match = re.search(r"(?:youtu\.be/|v=|/embed/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return f"https://www.youtube.com/embed/{match.group(1)}" if match else None
