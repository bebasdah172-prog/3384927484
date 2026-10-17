"""Replit entrypoint for the DuckAI-compatible API."""
from pathlib import Path
import runpy
import sys


APP_DIR = Path(__file__).resolve().parent / "duckapi"
sys.path.insert(0, str(APP_DIR))

# Delegate setup and serving to the project's existing bootstrap script.
# Replit should keep the API headless instead of opening a local browser.
if "--no-browser" not in sys.argv:
    sys.argv.append("--no-browser")
sys.argv[0] = str(APP_DIR / "run.py")
runpy.run_path(str(APP_DIR / "run.py"), run_name="__main__")
