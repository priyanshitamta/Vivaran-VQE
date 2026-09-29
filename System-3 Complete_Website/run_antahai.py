"""Old launcher name kept for existing start scripts - runs run_vivaran.py."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("run_vivaran.py")), run_name="__main__")
