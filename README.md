# CodePilot

A small AI coding agent powered by Gemini and FastAPI.

## Features
- Upload and browse a ZIP project.
- Ask Gemini to inspect or change project files.
- Review a proposed diff before approving changes.
- Run basic syntax checks after approved changes.
- Download the updated project.
- HTTP Basic login and per-user project ownership.

## Local setup

1. Create a virtual environment and install dependencies:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

2. Create `app/services/.env` with your own values (never commit this file):

    GEMINI_API_KEY=your_gemini_api_key
    CODEPILOT_USERS=you:use-a-long-password,friend:use-another-long-password
    GEMINI_MODEL=gemini-3.8-flash

3. Run from the project root:

    uvicorn app.main:app --reload --port 8001

4. Open `http://127.0.0.1:8001`. Your browser asks for your CodePilot username and password.

## Deployment

Use a Python web host that supports environment variables and a persistent disk. Set the start command to:

    uvicorn app.main:app --host 0.0.0.0 --port $PORT

Set these environment variables in the host dashboard (not in Git):
- `GEMINI_API_KEY`: your Gemini key
- `CODEPILOT_USERS`: comma-separated `username:password` entries, each with a long unique password
- `GEMINI_MODEL`: `gemini-3.8-flash`

Mount a persistent disk at the app's `app/uploads` directory so uploaded projects and owner metadata survive restarts. If your host cannot mount a disk there, change `UPLOAD_DIR` to its persistent disk path before launch. Keep the app behind HTTPS; HTTP Basic credentials are not safe over plain HTTP.

## Important security note

This is basic protection for a small private app, not a hardened public multi-tenant service. Command execution is restricted to a small allowlist and runs without a shell, but running arbitrary project tools can still be risky. Keep access limited to people you trust. A proper isolated container/sandbox is needed before allowing unknown public users.

Automatic checks are intentionally syntax-only (Python compile checks and JavaScript parser checks). They do not run a project's test suite because tests can execute arbitrary code.
