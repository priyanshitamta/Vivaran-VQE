"""
Vivaran-VQE (System 3) - in-process bridge into the System-2 MCQ Generator.

System 2 exposes no JSON API; its quiz pipeline is plain functions in
``utils/`` that only run correctly inside its own environment. Instead of
reimplementing transcription / LLM question generation / verification, this
bridge imports System 2's ``utils`` in-process and wraps the two steps System
3 needs:

  1. ``download_video``  - a Dataset-6 YouTube link -> local mp4 (yt-dlp)
  2. ``generate_quiz``   - S2's pipeline (audio -> transcript -> MCQs ->
                           validate -> verify) yielding the question list.

System 2 details that bite, and how we handle each:

  * ``utils.mcq_generator`` reads GROQ_API_KEY at import time (its own
    load_dotenv + os.getenv) and opens ``prompts/mcq_prompt.txt`` relative to
    the *current working directory* on every call. We therefore load the
    S2 .env into os.environ BEFORE importing utils, and run the pipeline with
    the S2 root as CWD (restored afterwards).
  * ``utils.audio_transcriber`` imports whisper lazily inside the call, so
    torch/whisper only load on the first quiz.
  * ``utils.video_processor`` shells out to bare ``ffmpeg``, so the ffmpeg bin
    dir is prepended to PATH for this process (see settings.FFMPEG_DIR).

Quiz generation is slow (download + whisper + two LLM calls, minutes), so
``generate_quiz`` is only ever called from a background worker thread; HTTP
request threads poll status via the DB. A process-wide lock serialises quiz
jobs - whisper's model and the CWD swap both want exclusivity anyway.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid

from dotenv import load_dotenv

import settings

logger = logging.getLogger(__name__)

# CWD for os.chdir (System 2 reads prompts/mcq_prompt.txt relative to it).
_S2_ROOT_STR = str(settings.S2_ROOT)

_READY = False
_IMPORT_LOCK = threading.Lock()

# Serialises quiz jobs so only one runs at a time (whisper model + os.chdir).
PIPELINE_LOCK = threading.Lock()


class QuizPipelineError(RuntimeError):
    """Raised when S2's pipeline rejects a video or produces no questions."""


def ensure_ready() -> None:
    """Idempotently prepare the process to import and run System 2's utils.

    Called lazily (not at module import) so the S3 process starts fast and
    never depends on S2's heavy stack unless a quiz is actually requested.
    """
    global _READY
    if _READY:
        return
    with _IMPORT_LOCK:
        if _READY:
            return

        # 1. Make S2's package importable: it has utils/__init__.py.
        if _S2_ROOT_STR not in sys.path:
            sys.path.insert(0, _S2_ROOT_STR)

        # 2. Export the S2 .env (GROQ_API_KEY, WHISPER_MODEL, ...). Must happen
        #    BEFORE importing utils.mcq_generator, which reads the key at
        #    import time. load_dotenv defaults to override=False so an already
        #    exported value (e.g. set by the launcher) wins.
        dotenv_path = os.path.join(_S2_ROOT_STR, ".env")
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path)

        # 3. ffmpeg for utils.video_processor (bare `ffmpeg` subprocess).
        ffmpeg_bin = str(settings.FFMPEG_DIR)
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")

        # 4. Import S2's utils (imports langchain-groq, but not whisper/torch).
        #    Deliberately NOT importing utils.app/question_manager/results.
        import utils.audio_transcriber  # noqa: F401
        import utils.mcq_generator  # noqa: F401
        import utils.video_processor  # noqa: F401

        logger.info("System 2 utils ready (S2 root: %s)", settings.S2_ROOT)
        _READY = True


# ---------------------------------------------------------------------------
# Step 1 - download a Dataset-6 link to an mp4
# ---------------------------------------------------------------------------


def download_video(video_link: str, dest_dir: str) -> str:
    """Download a YouTube link into ``dest_dir``; return the local mp4 path.

    Mirrors System 2's ``_download_video`` for the youtu.be / youtube.com
    case (all Dataset-6 links are youtu.be). Dataset-6 never contains direct
    files, so the plain urlopen branch of S2 is not ported.
    """
    os.makedirs(dest_dir, exist_ok=True)
    try:
        import yt_dlp
    except ImportError as error:  # pragma: no cover - venv ships it
        raise QuizPipelineError("Install yt-dlp to process YouTube links") from error

    outtmpl = os.path.join(dest_dir, f"{uuid.uuid4().hex}.%(ext)s")
    options = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        # Use the player clients in settings.YT_CLIENTS (default "android").
        # YouTube blocks anonymous downloads on the default web clients with a
        # "Sign in to confirm you're not a bot" wall; the android client
        # currently slips past it (verified end-to-end, see settings.YT_CLIENTS).
        "extractor_args": {"youtube": {"player_client": settings.YT_CLIENTS}},
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(video_link, download=True)
        if info is not None and info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise QuizPipelineError("YouTube playlist had no downloadable videos")
            info = entries[0]
        downloaded = downloader.prepare_filename(info)
    except QuizPipelineError:
        raise
    except Exception as error:  # yt-dlp raises a broad mix of exceptions
        raise QuizPipelineError(f"Video download failed: {error}") from error

    # prepare_filename returns the pre-merge extension (e.g. .webm); after a
    # successful merge the .mp4 exists next to it.
    merged = os.path.splitext(downloaded)[0] + ".mp4"
    if os.path.exists(merged):
        return merged
    if os.path.exists(downloaded):
        return downloaded
    raise QuizPipelineError("Video downloaded but the file could not be located")


# ---------------------------------------------------------------------------
# Step 2 - S2's pipeline on a local video
# ---------------------------------------------------------------------------


def _parse_generated_questions(raw_questions: object) -> list[dict]:
    """Normalise S2's raw LLM output into the question list shape S3 expects.

    This mirrors System 2's app.py ``_parse_generated_questions`` so question
    ids are renumbered 1..N and options are lists (not dicts), matching the
    stored-quiz convention S3's grader (db.grade_attempt) already uses.
    """
    if isinstance(raw_questions, dict):
        raw_questions = raw_questions.get("questions", raw_questions)
    if isinstance(raw_questions, str):
        cleaned = raw_questions.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            raw_questions = json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = cleaned.find("["), cleaned.rfind("]")
            if start < 0 or end <= start:
                raise QuizPipelineError(
                    "MCQ generator did not return a JSON question array: "
                    f"{cleaned[:200]}"
                )
            raw_questions = json.loads(cleaned[start : end + 1])
    if not isinstance(raw_questions, list) or not raw_questions:
        raise QuizPipelineError("MCQ generator returned no questions")

    questions = []
    for index, question in enumerate(raw_questions, start=1):
        if not isinstance(question, dict):
            raise QuizPipelineError(f"Invalid generated question at index {index}")
        options = question.get("options", [])
        if isinstance(options, dict):
            options = list(options.values())
        if not question.get("question") or len(options) < 2:
            raise QuizPipelineError(f"Incomplete generated question at index {index}")
        question["id"] = index
        question["options"] = options
        questions.append(question)
    return questions


def generate_quiz(
    video_link: str, topic: str | None = None,
    num_questions: int = settings.QUIZ_QUESTION_COUNT,
) -> tuple[list[dict], str]:
    """Download ``video_link`` and run the full S2 pipeline on it.

    Returns ``(questions, transcript)`` where each question dict carries the
    S2 fields (question, options, answer, explanation, source_snippet,
    verification_status, ...). Raises QuizPipelineError with a user-facing
    message on any failure. Call from a worker thread - this takes minutes.
    """
    ensure_ready()

    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    with PIPELINE_LOCK:
        video_path = None
        audio_path = None
        previous_cwd = os.getcwd()
        try:
            # System 2 reads prompts/mcq_prompt.txt relative to CWD.
            os.chdir(_S2_ROOT_STR)
            video_path = download_video(video_link, str(settings.UPLOAD_DIR))

            from utils.audio_transcriber import transcribe_audio
            from utils.mcq_generator import generate_mcqs, validate_mcqs, verify_mcqs
            from utils.video_processor import process_video

            audio_path = process_video(video_path)
            if not audio_path:
                raise QuizPipelineError("Audio extraction returned no output")

            transcript = transcribe_audio(audio_path)

            generated = generate_mcqs(transcript, num_questions, topic or "")
            questions = _parse_generated_questions(generated)
            questions = validate_mcqs(questions, transcript)
            questions = verify_mcqs(questions, transcript)
        except QuizPipelineError:
            raise
        except Exception as error:
            raise QuizPipelineError(f"Quiz generation failed: {error}") from error
        finally:
            os.chdir(previous_cwd)
            for path in (video_path, audio_path):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:  # pragma: no cover - best-effort cleanup
                        logger.warning("could not remove temp file %s", path)

    if not questions:
        raise QuizPipelineError(
            "No questions passed validation/verification. "
            "Try a different lecture video."
        )
    return questions, transcript
