"""
Vivaran-VQE - one-command launcher for the full platform.

Run this with the *System 2 venv* python (it has Flask + the utils stack that
System 3 imports in-process):

    "System-2 MCQ_Generator/.venv/Scripts/python.exe" run_vivaran.py

It starts, in order:
  1. System 1 (Recommendation Engine, FastAPI/uvicorn) on :8000 - using the
     machine's default ``python`` (which has fastapi/pandas/uvicorn).
  2. System 3 (this web shell, Flask) on :8080 - using this same interpreter.

System 2's code never runs as a server in this topology; System 3 imports its
utils in-process. Press Ctrl+C to stop everything together.

Optional env overrides: VIVARAN_PORT (S3), VIVARAN_S1_URL, VIVARAN_S2_ROOT,
VIVARAN_FFMPEG_DIR, VIVARAN_S1_PYTHON (python used to launch System 1).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Make S3's own modules importable when this file is run by path.
S3_ROOT = Path(__file__).resolve().parent
if str(S3_ROOT) not in sys.path:
    sys.path.insert(0, str(S3_ROOT))

import settings  # noqa: E402
import s1_client  # noqa: E402

S1_ROOT = S3_ROOT.parent / "System-1 Recommandation_Engine"
S1_PORT = "8000"
S3_PORT = str(settings.FLASK_PORT)

# Python that can run System 1 (fastapi/uvicorn/pandas live on the default
# interpreter, not the System 2 venv).
S1_PYTHON = os.environ.get("VIVARAN_S1_PYTHON", shutil.which("python") or "python")


def _start(prog_args, cwd, label):
    print(f"\n>>> starting {label}: {prog_args[0]} ... (cwd={cwd})")
    return subprocess.Popen(prog_args, cwd=str(cwd))


def main() -> int:
    if not (S1_ROOT / "app" / "main.py").exists():
        print(f"ERROR: System 1 not found at {S1_ROOT}")
        return 1
    if not (settings.S2_ROOT / "app.py").exists():
        print(f"WARNING: System 2 not found at {settings.S2_ROOT} - quizzes will show a "
              "friendly error unless VIVARAN_FAKE_QUIZ=1 is set.")
    if not settings.DATASET6_CSV.exists():
        print(f"ERROR: Dataset-6 not found at {settings.DATASET6_CSV}")
        return 1

    print("=" * 62)
    print("  Vivaran-VQE  —  System 3 (web shell) + System 1 (recommender)")
    print("=" * 62)

    # 1. System 1 (FastAPI) ------------------------------------------------
    s1_cmd = [
        S1_PYTHON, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", S1_PORT, "--log-level", "info",
    ]
    proc_s1 = _start(s1_cmd, S1_ROOT, "System 1 (Recommendation Engine)")

    # 2. Wait until it answers before starting S3's intake dependency.
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc_s1.poll() is not None:
            print("ERROR: System 1 exited early. Is uvicorn/fastapi installed for "
                  f"'{S1_PYTHON}'? See the traceback above.")
            return 1
        if s1_client.ping():
            break
        time.sleep(1)
    else:
        print("WARNING: System 1 still not answering - starting System 3 anyway.")

    # 3. System 3 (this interpreter has Flask + S2's utils) -----------------
    os.environ.setdefault("VIVARAN_S1_URL", f"http://127.0.0.1:{S1_PORT}")
    proc_s3 = _start([sys.executable, "app.py"], S3_ROOT, "System 3 (Vivaran-VQE)")

    print()
    print(f"  System 1 (recommendation engine): http://127.0.0.1:{S1_PORT}")
    print(f"  Vivaran-VQE  (this web app):         http://127.0.0.1:{S3_PORT}")
    print()
    print("  Press Ctrl+C in this terminal to stop both servers.")
    print("=" * 62)

    try:
        while True:
            time.sleep(1)
            if proc_s1.poll() is not None or proc_s3.poll() is not None:
                print("\nOne of the servers stopped. Shutting down the other…")
                break
    except KeyboardInterrupt:
        print("\nStopping servers…")
    finally:
        for proc in (proc_s3, proc_s1):
            if proc.poll() is None:
                proc.terminate()
        for proc in (proc_s3, proc_s1):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
