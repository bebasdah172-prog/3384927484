"""Bootstrap the server: check the port, build the venv, seed .env, run uvicorn.

Replaces the old start.bat/stop.bat, whose hardcoded interpreter path pointed at
another user's profile and so only worked by falling through to PATH.

Usage:
    python run.py                  # start, open the dashboard when healthy
    python run.py --no-browser     # start headless
    python run.py --port 9000
    python run.py --reload         # uvicorn autoreload (dev)
    python run.py --check          # setup + port check only, don't boot
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
LOG = ROOT / "server.log"
DEFAULT_PORT = 8080


def venv_python() -> Path | None:
    """The venv interpreter, or None if the venv was never created."""
    exe = VENV / "Scripts" / "python.exe"
    if exe.exists():
        return exe
    exe = VENV / "bin" / "python"  # non-Windows layout
    return exe if exe.exists() else None


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """True if something already accepts connections on `port`.

    Probes by connecting rather than parsing netstat: the question is "will
    uvicorn fail to bind", and only a real connect answers that.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def child_environment() -> dict[str, str]:
    """Add the Nix-provided C++ runtime library for native Python extensions."""
    env = os.environ.copy()
    gcc = shutil.which("gcc")
    if os.name != "nt" and gcc:
        result = subprocess.run(
            [gcc, "-print-file-name=libstdc++.so.6"],
            capture_output=True,
            text=True,
            check=False,
        )
        library = Path(result.stdout.strip())
        if result.returncode == 0 and library.is_file():
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = (
                f"{library.parent}{os.pathsep}{existing}" if existing
                else str(library.parent)
            )
    return env


def ensure_venv() -> Path:
    """Return a venv with the exact dependencies declared by requirements.txt."""
    py = venv_python()
    if not py:
        print("Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        py = venv_python()
        if not py:  # pragma: no cover - venv module just created it
            raise RuntimeError(f"venv created but no interpreter found under {VENV}")

    requirements = ROOT / "requirements.txt"
    if not requirements.is_file():
        raise FileNotFoundError(f"Dependency file not found: {requirements}")

    check = [
        str(py), "-c",
        "import fastapi, uvicorn, pydantic, dotenv, playwright",
    ]
    # The requirements fingerprint catches version changes as well as a fresh
    # environment. The import check repairs a venv whose packages were removed
    # or only partly installed. Use the same native-library environment as the
    # server so Playwright's greenlet extension is checked correctly on Nix.
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    fingerprint_file = VENV / ".requirements.sha256"
    installed_fingerprint = (
        fingerprint_file.read_text(encoding="utf-8").strip()
        if fingerprint_file.is_file()
        else ""
    )
    imports_ok = subprocess.run(
        check,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_environment(),
    ).returncode == 0

    if not imports_ok or installed_fingerprint != fingerprint:
        print("Installing or updating project dependencies...")
        pip_env = child_environment()
        # The Replit base image sets `global.user = yes` in pip.conf. That
        # default is invalid inside a venv; override it only for this install.
        pip_env["PIP_USER"] = "false"
        pip_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        pip_env["PIP_NO_INPUT"] = "1"
        subprocess.run(
            [
                str(py), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input",
                "-r", str(requirements),
            ],
            check=True,
            env=pip_env,
        )
        subprocess.run(check, check=True, env=child_environment())
        fingerprint_file.write_text(fingerprint + "\n", encoding="utf-8")
    return py


def _configured_chrome_path(py: Path) -> str:
    """Read DUCKAI_CHROME_PATH from the process environment or the app's .env."""
    configured = os.getenv("DUCKAI_CHROME_PATH", "").strip()
    if configured:
        return configured

    result = subprocess.run(
        [
            str(py), "-c",
            "from dotenv import load_dotenv; import os; "
            "load_dotenv(); print(os.getenv('DUCKAI_CHROME_PATH', '').strip())",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=child_environment(),
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _playwright_chromium_paths(py: Path) -> tuple[Path, Path] | None:
    """Return Playwright's regular Chromium executable and cache marker paths."""
    driver = subprocess.run(
        [
            str(py), "-c",
            "from playwright._impl._driver import compute_driver_executable\n"
            "print(*compute_driver_executable(), sep='\\n')\n",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=child_environment(),
        check=False,
    )
    if driver.returncode != 0:
        return None
    driver_paths = driver.stdout.strip().splitlines()
    if len(driver_paths) < 2:
        return None

    node, cli = driver_paths[:2]
    registry = Path(cli).parent / "lib" / "server" / "registry" / "index.js"
    if not registry.is_file():
        return None

    # Ask Playwright's own registry for the platform- and version-specific
    # executable path rather than guessing names such as chromium-1148.
    script = (
        "const { registry, browserDirectoryToMarkerFilePath } = require(process.argv[1]); "
        "const browser = registry.findExecutable('chromium'); "
        "console.log(browser.executablePath()); "
        "console.log(browserDirectoryToMarkerFilePath(browser.directory));"
    )
    result = subprocess.run(
        [
            node, "-e", script, str(registry),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=child_environment(),
        check=False,
    )
    if result.returncode != 0:
        return None
    paths = result.stdout.strip().splitlines()
    return (Path(paths[-2]), Path(paths[-1])) if len(paths) >= 2 else None


def ensure_browser(py: Path) -> None:
    """Use a configured/system browser, or ensure bundled Chromium exists."""
    configured = _configured_chrome_path(py)
    if configured:
        if not Path(configured).is_file():
            raise RuntimeError(
                "DUCKAI_CHROME_PATH does not point to a browser executable: "
                f"{configured}"
            )
        return

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return

    browser_paths = _playwright_chromium_paths(py)
    if not browser_paths:
        raise RuntimeError("Could not determine Playwright's Chromium executable path.")

    chromium, install_marker = browser_paths
    if chromium.is_file():
        return

    # A stale completion marker can make Playwright believe a partial browser
    # download is complete. Remove only the marker for this exact Chromium build
    # so `install chromium` repairs it without touching other browser data.
    install_marker.unlink(missing_ok=True)
    print("Playwright Chromium is missing; installing browser files...")
    subprocess.run(
        [str(py), "-m", "playwright", "install", "chromium"],
        cwd=ROOT,
        check=True,
        env=child_environment(),
    )
    browser_paths = _playwright_chromium_paths(py)
    if not browser_paths or not browser_paths[0].is_file():
        raise RuntimeError(
            "Playwright Chromium installation finished, but the browser "
            f"executable is still missing: {chromium}"
        )


def ensure_env() -> None:
    """Seed .env from example.env when absent. Never overwrites an existing one -
    it holds the user's API key and proxy settings."""
    env = ROOT / ".env"
    if env.exists():
        return
    shutil.copyfile(ROOT / "example.env", env)
    print(f"Created .env from example.env (open access, no API key set).")


def wait_healthy(port: int, timeout: float = 90.0) -> bool:
    """Poll /health until the server answers or `timeout` elapses.

    The dashboard is opened only after this passes, so the browser never lands
    on a connection-refused page during the ~10s prewarm.
    """
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def serve(py: Path, port: int, reload: bool) -> int:
    """Run uvicorn as a child process, teeing its output to server.log and the
    console. A child (rather than uvicorn.run in-process) keeps Ctrl-C simple:
    the signal reaches uvicorn directly and this process just reaps it.
    """
    cmd = [
        str(py), "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0", "--port", str(port),
    ]
    if reload:
        cmd.append("--reload")

    print(f"Starting uvicorn on port {port}...")
    print(f"  dashboard : http://localhost:{port}/")
    print(f"  log file  : {LOG}")

    # Truncate rather than append: one run's log is one run's story, and a
    # stale tail from a crashed run is actively misleading while debugging.
    with LOG.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=child_environment(),
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
            return proc.wait()
        except KeyboardInterrupt:
            print("\nShutting down...")
            proc.terminate()
            try:
                return proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the DuckAI2API server.")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)))
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard")
    ap.add_argument("--reload", action="store_true", help="uvicorn autoreload (dev)")
    ap.add_argument("--check", action="store_true", help="setup + port check only, don't boot")
    args = ap.parse_args()

    if port_busy(args.port):
        print(f"Port {args.port} is already in use - something else is listening there.")
        print(f"Close it first, or start on another port:  python run.py --port {args.port + 1}")
        return 1

    ensure_env()
    py = ensure_venv()
    ensure_browser(py)
    print(f"Interpreter: {py}")

    if args.check:
        print(f"Port {args.port} is free. Setup OK.")
        return 0

    # Open the dashboard only once the server actually answers, so the browser
    # never lands on a connection-refused page during the ~10s prewarm.
    if not args.no_browser and not args.reload:
        threading.Thread(
            target=lambda: (wait_healthy(args.port) and webbrowser.open(f"http://localhost:{args.port}/")),
            daemon=True,
        ).start()

    return serve(py, args.port, args.reload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
