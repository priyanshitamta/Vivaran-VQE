"""
Live integration test: real System 1 (FastAPI) + real System 3 (Flask).

S1 is launched against a TEMP COPY of its datasets (S1_DATA_DIR) so the real
Dataset-5 is never written. S3 runs normally (venv python). Quiz generation is
canned via VIVARAN_FAKE_QUIZ=1 (no YouTube/whisper/Groq needed here - the
S2 bridge itself was not exercised; that is the manual demo path).

Usage (System 2 venv python, which has Flask + requests):
    ".venv/Scripts/python.exe" integration_live.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIH = ROOT.parent
S1_ROOT = SIH / "System-1 Recommandation_Engine"
PY = shutil.which("python") or "python"  # has fastapi/uvicorn/pandas
VENV_PY = str(SIH / "System-2 MCQ_Generator" / ".venv" / "Scripts" / "python.exe")

S1_PORT = "8000"
S3_PORT = "8091"
S1_URL = f"http://127.0.0.1:{S1_PORT}"
S3_URL = f"http://127.0.0.1:{S3_PORT}"

tmp = tempfile.mkdtemp(prefix="vivaran_live_")
work = Path(tmp)


def wait_http(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.6)
    return False


def main() -> int:
    # ---- temp dataset copy for S1 (never touch the real one) ---------------
    ds_copy = work / "datasets"
    shutil.copytree(S1_ROOT / "datasets", ds_copy)

    real_ds5 = S1_ROOT / "datasets" / "Dataset-5_Real_Employee_Profiles.csv"
    real_before = real_ds5.read_text(encoding="utf-8")

    env = dict(os.environ)
    env["S1_DATA_DIR"] = str(ds_copy)
    env["VIVARAN_DB"] = str(work / "vivaran.db")
    env["VIVARAN_PORT"] = S3_PORT
    env["VIVARAN_S1_URL"] = S1_URL
    env["VIVARAN_FAKE_QUIZ"] = "1"
    env["VIVARAN_SECRET"] = "live-test"

    s1_log = open(work / "s1.log", "wb")
    s3_log = open(work / "s3.log", "wb")

    proc_s1 = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", S1_PORT, "--log-level", "warning"],
        cwd=str(S1_ROOT), env=env, stdout=s1_log, stderr=subprocess.STDOUT,
    )
    proc_s3 = subprocess.Popen(
        [VENV_PY, "app.py"], cwd=str(ROOT), env=env, stdout=s3_log, stderr=subprocess.STDOUT,
    )

    try:
        if not wait_http(f"{S1_URL}/meta/skills"):
            print("FAIL: System 1 never came up."); return 1
        print("System 1 up.")
        if not wait_http(f"{S3_URL}/"):
            print("FAIL: System 3 never came up."); return 1
        print("System 3 up.")

        import requests  # venv has it

        s = requests.Session()

        r = s.post(f"{S3_URL}/api/register", json={"username": "live", "password": "pass"})
        assert r.status_code == 200, r.text
        r = s.post(f"{S3_URL}/login", json={"username": "live", "password": "pass"})
        assert r.status_code == 200 and r.json()["redirect"] == "/recommendation", r.text

        html = s.get(f"{S3_URL}/recommendation").text
        assert "iGOT Recommendation" in html, "intake page missing"

        # Pull a real role + skill id straight from System 1.
        def jget(url):
            return json.loads(urllib.request.urlopen(url, timeout=5).read().decode())

        role = jget(f"{S1_URL}/meta/roles")[0]
        skills = jget(f"{S1_URL}/meta/skills")

        payload = {
            "name": "Live Tester",
            "role_id": role["role_id"],
            "designation": role["designation"],
            "department": role["department"],
            "current_assignment": None,
            "educational_qualifications": "M.Sc Statistics",
            "work_experience_years": 4,
            "previous_trainings": [],
            "self_rated_skills": {s["skill_id"]: 1 for s in skills[:6]},
        }
        r = s.post(f"{S3_URL}/recommendation", json=payload)
        assert r.status_code == 200 and r.json().get("ok"), r.text
        results_url = r.json()["redirect"]
        html = s.get(f"{S3_URL}{results_url}").text
        assert "Live Tester" in html and "Take a quiz" in html, "results page missing"

        # Pick the first recommended course id for a quiz run (read the child
        # S3 process's own vivaran.db directly).
        import sqlite3

        submission_id = results_url.rstrip("/").split("/")[-1]
        con = sqlite3.connect(env["VIVARAN_DB"])
        con.row_factory = sqlite3.Row
        sub = con.execute(
            "SELECT * FROM submissions WHERE id = ?", (int(submission_id),)
        ).fetchone()
        course_id = json.loads(sub["recommendations"])[0]["course_id"]

        r = s.post(f"{S3_URL}/quiz/start", data={"submission_id": submission_id, "course_id": course_id}, allow_redirects=False)
        assert r.status_code == 302 and "/quiz/" in r.headers["Location"], r.text
        attempt_url = r.headers["Location"]
        attempt_id = attempt_url.split("/")[-1]

        for _ in range(50):
            st = s.get(f"{S3_URL}/quiz/{attempt_id}/status.json").json()
            if st["status"] == "ready":
                break
            time.sleep(0.1)
        assert st["status"] == "ready", f"quiz never ready: {st}"

        quiz = json.loads(con.execute(
            "SELECT quiz FROM quiz_attempts WHERE id = ?", (int(attempt_id),)
        ).fetchone()["quiz"])
        con.close()
        answers = {f"q_{q['id']}": q["answer"] for q in quiz}
        r = s.post(f"{S3_URL}/quiz/{attempt_id}", data=answers, allow_redirects=False)
        assert r.status_code == 302 and "results" in r.headers["Location"], r.text
        html = s.get(f"{S3_URL}{r.headers['Location']}").text
        assert f"{len(quiz)}/{len(quiz)}" in html and "can only be attempted once per" in html

        html = s.get(f"{S3_URL}/profile").text
        assert "Hi, live!" in html and "Live Tester" in html and "Quiz:" in html

        # ---- the real Dataset-5 must be byte-identical ----------------------
        assert real_ds5.read_text(encoding="utf-8") == real_before, "REAL Dataset-5 was modified!"
        ds5_copy = (ds_copy / "Dataset-5_Real_Employee_Profiles.csv").read_text(encoding="utf-8")
        assert "Live Tester" in ds5_copy, "temp Dataset-5 missing the new employee"
        print("\nALL LIVE CHECKS PASSED (real Dataset-5 untouched; new row in temp copy).")
        return 0
    except AssertionError as err:
        print(f"\nFAIL: {err}")
        return 1
    except Exception as err:  # noqa: BLE001
        print(f"\nFAIL: {err!r}")
        return 1
    finally:
        for proc in (proc_s3, proc_s1):
            proc.terminate()
        for proc in (proc_s3, proc_s1):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        s1_log.close(); s3_log.close()
        if os.environ.get("KEEP_LIVE_TMP"):
            print("workspace kept at", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
