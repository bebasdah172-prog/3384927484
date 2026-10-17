---
name: DuckAPI Nix runtime
description: Replit Nix environment details for the DuckAPI nested virtualenv and Playwright native modules.
---

For this project, the server runs from `duckapi/.venv`, not the root Python environment. Reconcile dependencies when the requirements file changes, and verify imports even if the venv already exists. Run the Playwright import check with the same child environment as the server: without the Nix `libstdc++` path, `greenlet` looks like a missing package and causes needless reinstalls. Replit's pip config sets `global.user = yes`; the nested-venv install must override that with `PIP_USER=false`.

Prefer the Nix-provided `chromium` executable for Replit deployments. The Playwright-bundled browser may lack Linux shared libraries in this environment, and some container hosts have crashed Playwright's separate `chromium-headless-shell` with `SIGSEGV`. The app can use bundled Chromium via `channel="chromium"` as a fallback, but it still needs the host browser libraries.

Playwright's `greenlet` extension needs `libstdc++.so.6`. Installing the Nix `gcc` package provides the library, but the child server process may still need its directory in `LD_LIBRARY_PATH`. Resolve it with `gcc -print-file-name=libstdc++.so.6`; do not hardcode a Nix store path.

For Pterodactyl, browser OS dependencies must be baked into a custom Docker image because the server runs unprivileged. Use `Dockerfile.pterodactyl`, set `PLAYWRIGHT_BROWSERS_PATH` to the image's read-only browser directory, and keep the panel startup command `python3 main.py`; do not try `apt` from the server startup command.

**Why:** Playwright's browser downloads do not install their Linux shared-library dependencies. Nix Chromium works in Replit with its runtime libraries, while Pterodactyl's unprivileged server process cannot add missing OS packages at startup.

**How to apply:** When deploying or changing browser selection, keep Nix Chromium available in Replit, prefer configured/system Chrome or Chromium, and use bundled regular Chromium only on hosts with its required libraries. On Pterodactyl, use a custom image with Playwright OS dependencies and browser preinstalled. Preserve the venv import check, pip override, and dynamic C++ library lookup.
