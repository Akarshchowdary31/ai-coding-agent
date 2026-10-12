import os
from pathlib import Path
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent / "services" / ".env"
load_dotenv(ENV_FILE)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Format: username:password,another_username:another_password
_raw_users = os.getenv("CODEPILOT_USERS", "")
CODEPILOT_USERS = {}
for item in _raw_users.split(","):
    if ":" in item:
        username, password = item.split(":", 1)
        if username.strip() and password:
            CODEPILOT_USERS[username.strip()] = password
