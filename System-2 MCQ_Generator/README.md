MCQ Generator — AI-Powered Quiz Generation from Lecture Videos
This system ingests a lecture video (uploaded file or a video link, including YouTube), transcribes it, and automatically generates fact-checked Multiple-Choice Questions (MCQs) using an LLM. Learners work through the course lecture-by-lecture, answering an auto-generated quiz after each one, then a final quiz drawing on material from across the course. Every attempt is scored and stored, so progress and improvement can be tracked from the first lecture to the last.
Designed for students, teachers, trainers, and learning platforms that want quiz creation straight from recorded lectures, with an assessment flow (per-lecture quizzes, a final quiz, and a results dashboard) rather than a single one-off question dump.
Key Features
Video-to-Quiz Pipeline
Flexible Video Input: Accepts a direct video file upload, a direct video URL, or a YouTube link (downloaded via yt-dlp).
Audio Extraction: Uses ffmpeg to pull the audio track from the lecture video before transcription.
Transcription: Converts lecture audio to text using OpenAI's Whisper model (configurable via the WHISPER_MODEL environment variable — defaults to tiny for speed).
MCQ Generation: Uses a Groq-hosted LLM (via LangChain's ChatGroq) to generate multiple-choice questions directly from the transcript, guided by an optional topic filter.
Validation: Every generated question is checked structurally before use — the marked answer must be one of the listed options, duplicate questions are dropped, and each question's source snippet is checked against the transcript for grounding.
Automated Fact-Checking: A second LLM pass re-checks each surviving question against the full transcript and tags it verified, flagged, or unverified, with a stated reason for anything flagged.
Course-Style Assessment Flow
Per-Lecture Quizzes: Course setup specifies total lectures and questions-per-lecture; each lecture's quiz is served from a slice of the generated question bank.
Final Quiz: After all lectures are complete, a final quiz draws from whatever questions weren't already used lecture-by-lecture.
Results & Scoring: The results page shows total attempted, correct/incorrect counts, accuracy, and a full question-by-question breakdown with the learner's answer, the correct answer, explanation, source snippet, and verification badge.
Progress Tracking: Every quiz attempt (per-lecture and final) is persisted to a local SQLite database, keyed to an anonymous per-session user ID. This powers an end-of-course improvement summary — first lecture accuracy vs. last lecture accuracy vs. final quiz accuracy.
Technology Stack
Backend: Python, Flask, LangChain, Groq LLM API, OpenAI Whisper, ffmpeg, yt-dlp, SQLite, python-dotenv
Frontend: HTML, CSS (custom, no framework), Jinja2 templating
LLM & AI Layer: Groq-hosted LLM (via langchain-groq's ChatGroq) for both MCQ generation and fact-checking; OpenAI Whisper for speech-to-text
Storage: SQLite (results.db) for quiz-attempt history and progress tracking
Quick Start
Clone the repository and install dependencies:
bash
git clone <your-repo-url>
cd PDFQuizzer
pip install -r requirements.txt
Make sure ffmpeg is installed and available on your system PATH (required for audio extraction), and, if you plan to process YouTube links, that yt-dlp is installed.
Create a .env file:
GROQ_API_KEY=your_key_here
WHISPER_MODEL=tiny
WHISPER_MODEL is optional — it defaults to tiny if omitted. Larger models (base, small, medium, large) trade speed for transcription accuracy.
Run the app:
bash
python app.py
Open the UI:
http://localhost:5050
(Port 5050 is used instead of the Flask default 5000 to avoid a conflict with macOS AirPlay Receiver — see the comment at the bottom of app.py if you'd rather change it back.)
High-Level Architecture
User (Course Details + Lecture Video or Link)
   ↓
Flask Web App
   ↓
┌────────────────── Processing Pipeline ───────────────────┐
│ Video Acquisition (upload / direct link / YouTube)        │
│ Audio Extraction (ffmpeg)                                 │
│ Transcription (Whisper)                                   │
│ MCQ Generation (Groq LLM)                                 │
│ Structural Validation + Transcript Grounding Check         │
│ LLM Fact-Check → verified / flagged / unverified           │
└─────────────────────────────────────────────────────────┘
   ↓
Per-Lecture Quiz → ... → Final Quiz
   ↓
Scoring + Verification Badges + Results Dashboard
   ↓
Attempt History (SQLite) → Course Progress Summary
Project Structure
app.py                     Flask routes: course setup, lecture quizzes, final quiz, results
utils/
  video_processor.py       Audio extraction from video (ffmpeg)
  audio_transcriber.py     Whisper-based transcription
  mcq_generator.py         MCQ generation, validation, and LLM fact-checking
  question_manager.py      Question-bank loading and per-lecture/final-quiz slicing
  results_manager.py       SQLite-backed quiz-attempt storage and progress summaries
question_bank/
  questions.json           Fallback/static question bank
templates/
  index.html               Course setup + video input
  quiz.html                Per-lecture and final quiz (shared template)
  result.html               Results dashboard
Roadmap & Future Enhancements
Live stage-by-stage progress reporting during video processing (transcription vs. generation vs. verification), rather than a single blocking wait
Configurable transcript truncation for very long lectures to keep fact-checking latency manageable
Difficulty-level selection for generated questions
Multi-chapter / multi-topic detection within a single long lecture
Persistent user accounts (progress currently tracked per anonymous browser session)
Export quiz results (PDF / CSV / JSON)
Classroom or instructor dashboard for tracking multiple learners
Multi-language lecture support