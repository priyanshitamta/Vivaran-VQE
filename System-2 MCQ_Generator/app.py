import json
import importlib
import logging
import os
import uuid
from urllib.parse import urlparse
from urllib.request import urlopen

from flask import Flask, render_template, request, redirect, url_for, session

from utils.audio_transcriber import transcribe_audio
from utils.mcq_generator import generate_mcqs, validate_mcqs, verify_mcqs
from utils.video_processor import process_video

from utils.question_manager import (
    load_questions,
    get_questions_for_lecture,
    get_remaining_questions,
    select_final_questions
)

from utils.results_manager import (
    save_quiz_result,
    get_improvement_summary,
)

app = Flask(__name__)

app.secret_key = "sih-secret-key"
UPLOAD_FOLDER = os.path.join(app.root_path, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load fixed question bank
ALL_QUESTIONS = load_questions()


def get_questions_per_lecture(duration_hours):
    """
    Decide how many questions should be asked
    after a lecture based on its duration.

    1 hour  -> 2 questions
    2 hours -> 3 questions
    3 hours -> 4 questions
    4+ hours -> 5 questions
    """

    duration_hours = int(duration_hours)

    if duration_hours <= 1:
        return 2
    elif duration_hours == 2:
        return 3
    elif duration_hours == 3:
        return 4
    else:
        return 5


def _get_user_id():
    """Persistent anonymous id for this browser session, used to group quiz
    attempts for progress/improvement tracking until real auth exists."""
    if "user_id" not in session:
        session["user_id"] = str(uuid.uuid4())
    return session["user_id"]


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


def _download_video(video_link):
    """Download a direct video URL, or use yt-dlp for supported video sites."""
    parsed_url = urlparse(video_link)
    hostname = (parsed_url.hostname or "").lower()

    if hostname == "youtu.be" or hostname.endswith("youtube.com"):
        try:
            yt_dlp = importlib.import_module("yt_dlp")
        except ImportError as error:
            raise RuntimeError("Install yt-dlp to process YouTube links") from error
        output_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}.%(ext)s")
        options = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "outtmpl": output_path,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(video_link, download=True)
            downloaded_path = downloader.prepare_filename(info)
        merged_path = os.path.splitext(downloaded_path)[0] + ".mp4"
        return merged_path if os.path.exists(merged_path) else downloaded_path

    extension = os.path.splitext(parsed_url.path)[1] or ".mp4"
    output_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}{extension}")
    with urlopen(video_link) as response, open(output_path, "wb") as video_file:
        video_file.write(response.read())
    return output_path


def _parse_generated_questions(raw_questions):
    if isinstance(raw_questions, dict):
        raw_questions = raw_questions.get("questions", raw_questions)
    if isinstance(raw_questions, str):
        cleaned = raw_questions.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            raw_questions = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("[")
            end = cleaned.rfind("]")
            if start < 0 or end <= start:
                raise ValueError(
                    "MCQ generator did not return a JSON question array: "
                    f"{cleaned[:200]}"
                )
            raw_questions = json.loads(cleaned[start:end + 1])
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("MCQ generator returned no questions")

    questions = []
    for index, question in enumerate(raw_questions, start=1):
        if not isinstance(question, dict):
            raise ValueError(f"Invalid generated question at index {index}")
        options = question.get("options", [])
        if isinstance(options, dict):
            options = list(options.values())
        if not question.get("question") or len(options) < 2:
            raise ValueError(f"Incomplete generated question at index {index}")
        question["id"] = index
        question["options"] = options
        questions.append(question)
    return questions


def _run_pipeline(video_path, requested_questions, total_lectures, topic):
    """Shared pipeline: audio extraction -> transcription -> MCQ generation -> verification."""
    audio_path = process_video(video_path)

    if not audio_path:
        raise RuntimeError("Audio extraction returned no output")

    transcript = transcribe_audio(audio_path)

    generated = generate_mcqs(
        transcript,
        requested_questions * total_lectures,
        topic,
    )

    questions = _parse_generated_questions(generated)
    questions = validate_mcqs(questions, transcript)
    questions = verify_mcqs(questions, transcript)

    if not questions:
        raise RuntimeError(
            "No questions passed validation/verification. "
            "Try a longer or clearer lecture video."
        )

    return questions, transcript


# --------------------------------------------------
# START COURSE
# --------------------------------------------------

@app.route("/start-course", methods=["POST"])
def start_course():
    global ALL_QUESTIONS

    course_name = request.form.get("course_name", "Course")
    total_lectures = int(request.form.get("total_lectures", 1))
    lecture_duration = int(request.form.get("lecture_duration", 1))
    requested_questions = int(request.form.get("num_questions", 5))
    topic = request.form.get("topic", "").strip()
    video_file = request.files.get("lecture_video")
    video_link = request.form.get("video_link", "").strip()

    # Resolve video_path from either an uploaded file or a pasted link
    if not video_file or not video_file.filename:
        if not video_link:
            return render_template(
                "index.html",
                error="Upload a lecture video or provide a video link."
            ), 400
        try:
            video_path = _download_video(video_link)
        except Exception:
            logger.exception("Video link processing failed")
            return render_template(
                "index.html",
                error="Video link processing failed. Check the Flask terminal for details."
            ), 400
    else:
        video_path = os.path.join(
            UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{video_file.filename}"
        )
        video_file.save(video_path)

    # Run the same pipeline regardless of upload vs. link
    try:
        ALL_QUESTIONS, transcript = _run_pipeline(
            video_path, requested_questions, total_lectures, topic
        )
    except Exception as e:
        import traceback
        print("\n========== ACTUAL BACKEND ERROR ==========")
        print("ERROR:", e)
        traceback.print_exc()
        print("==========================================\n")

        return render_template(
            "index.html",
            error=f"Lecture processing failed: {e}"
        )

    questions_per_lecture = requested_questions or get_questions_per_lecture(lecture_duration)

    # Assign (or reuse) a persistent id for this browser session, used to
    # group all of this user's quiz attempts for this course together.
    _get_user_id()

    # Store course information in session
    session["course_name"] = course_name
    session["total_lectures"] = total_lectures
    session["lecture_duration"] = lecture_duration
    session["questions_per_lecture"] = questions_per_lecture
    session["transcript"] = transcript

    # Start from lecture 1
    session["current_lecture"] = 1

    # Questions already shown to this user
    session["used_question_ids"] = []

    # Answers given by user
    session["answers"] = {}

    session["topic"] = topic or "Lecture"

    return redirect(url_for("lecture_quiz", lecture_number=1))


# --------------------------------------------------
# QUIZ AFTER EACH LECTURE
# --------------------------------------------------

@app.route("/lecture/<int:lecture_number>", methods=["GET", "POST"])
def lecture_quiz(lecture_number):

    total_lectures = session.get("total_lectures", 1)

    questions_per_lecture = session.get(
        "questions_per_lecture", 2
    )

    # Make sure lecture number is valid
    if lecture_number > total_lectures:
        return redirect(url_for("final_quiz"))

    # Get questions for this lecture
    questions = get_questions_for_lecture(
        ALL_QUESTIONS,
        lecture_number,
        questions_per_lecture
    )

    # POST = user submitted lecture quiz
    if request.method == "POST":

        used_ids = session.get("used_question_ids", [])
        answers = session.get("answers", {})

        # Answers for just THIS lecture's questions, kept separately so we
        # can score and store this attempt on its own (session["answers"]
        # keeps accumulating across the whole course as before).
        lecture_answers = {}

        for question in questions:

            question_id = str(question["id"])

            selected_answer = request.form.get(
                f"question_{question_id}"
            )

            if selected_answer:
                answers[question_id] = selected_answer
                lecture_answers[question_id] = selected_answer

            if question["id"] not in used_ids:
                used_ids.append(question["id"])

        session["used_question_ids"] = used_ids
        session["answers"] = answers

        # Persist this lecture's quiz result.
        save_quiz_result(
            user_id=_get_user_id(),
            course_name=session.get("course_name", "Course"),
            attempt_type="lecture",
            lecture_number=lecture_number,
            questions=questions,
            answers=lecture_answers,
        )

        # Move to next lecture
        next_lecture = lecture_number + 1

        if next_lecture <= total_lectures:

            session["current_lecture"] = next_lecture

            return redirect(
                url_for(
                    "lecture_quiz",
                    lecture_number=next_lecture
                )
            )

        # All lectures completed
        return redirect(url_for("final_quiz"))

    return render_template(
        "quiz.html",
        questions=questions,
        lecture_number=lecture_number,
        total_lectures=total_lectures
    )


# --------------------------------------------------
# FINAL QUIZ
# --------------------------------------------------

@app.route("/final-quiz", methods=["GET", "POST"])
def final_quiz():

    used_ids = session.get("used_question_ids", [])

    # Get questions that user has NOT seen
    final_questions = select_final_questions(
        ALL_QUESTIONS,
        used_ids
    )

    if not final_questions:
        return redirect(url_for("result"))

    # POST = final quiz submitted
    if request.method == "POST":

        answers = session.get("answers", {})
        final_answers = {}

        for question in final_questions:

            question_id = str(question["id"])

            selected_answer = request.form.get(
                f"question_{question_id}"
            )

            if selected_answer:
                answers[question_id] = selected_answer
                final_answers[question_id] = selected_answer

        session["answers"] = answers

        # Persist the final quiz result.
        save_quiz_result(
            user_id=_get_user_id(),
            course_name=session.get("course_name", "Course"),
            attempt_type="final",
            lecture_number=None,
            questions=final_questions,
            answers=final_answers,
        )

        return redirect(url_for("result"))

    return render_template(
        "quiz.html",
        questions=final_questions
    )


# --------------------------------------------------
# RESULT PAGE
# --------------------------------------------------

@app.route("/result")
def result():

    answers = session.get("answers", {})

    score = 0
    total = 0

    results = []

    for question in ALL_QUESTIONS:

        question_id = str(question["id"])

        # Only evaluate questions actually attempted
        if question_id not in answers:
            continue

        user_answer = answers[question_id]

        correct_answer = question["answer"]

        is_correct = (
            user_answer.lower()
            == correct_answer.lower()
        )

        if is_correct:
            score += 1

        total += 1

        results.append({
            "question": question["question"],
            "options": question["options"],
            "user_answer": user_answer,
            "correct_answer": correct_answer,
            "explanation": question.get("explanation", ""),
            "source_snippet": question.get("source_snippet", ""),
            "is_correct": is_correct,
            # NEW ‚Äî pull whatever verify_mcqs() attached to the question dict
            "verification_status": question.get("verification_status", "unverified"),
            "verification_reason": question.get("verification_reason", ""),
        })

    accuracy = 0

    if total > 0:
        accuracy = round(
            (score / total) * 100,
            2
        )

    # Cross-lecture progress, built from everything already saved via
    # save_quiz_result() above -- this is the "improvement by end of
    # course" signal for the dashboard.
    progress = get_improvement_summary(
        session.get("user_id", "anonymous"),
        session.get("course_name", "Course"),
    )

    return render_template(
        "result.html",
        topic=session.get("topic", "Lecture"),
        total_attempted=total,
        total_correct=score,
        total_incorrect=total - score,
        accuracy_percentage=accuracy,
        used_question_count=len(answers),
        question_count=len(ALL_QUESTIONS),
        performance_data={session.get("topic", "Lecture"): accuracy},
        results=results,
        transcript=session.get("transcript", ""),
        progress=progress,
    )


# --------------------------------------------------
# RUN APPLICATION
# --------------------------------------------------

if __name__ == "__main__":
    # Port changed from the default 5000 to 5050 because on macOS,
    # AirPlay Receiver occupies port 5000 by default, causing
    # "Address already in use" errors. You can also fix this by
    # disabling AirPlay Receiver in System Settings > General >
    # AirDrop & Handoff, and reverting this to port=5000 if you prefer.
    app.run(debug=True, port=5050)

