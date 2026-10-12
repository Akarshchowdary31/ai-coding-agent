from difflib import unified_diff
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4
from datetime import datetime, timezone
import hmac
import json
import os
import shutil
import stat
import tempfile
import zipfile
import subprocess
import sys

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.agent.orchestrator import run_agent
from app.config import CODEPILOT_USERS

app = FastAPI(title="CodePilot")
security = HTTPBasic()
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
STAGING_DIR = BASE_DIR / "staging"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STAGING_DIR.mkdir(parents=True, exist_ok=True)
OWNER_FILE = UPLOAD_DIR / ".owners.json"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
MAX_EXTRACTED_SIZE = 200 * 1024 * 1024
MAX_FILES = 5000
MAX_REVIEW_FILES = 200
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class AgentRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    project_id: str = Field(min_length=32, max_length=32)


def require_user(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not CODEPILOT_USERS:
        raise HTTPException(status_code=503, detail="CodePilot access is not configured. Set CODEPILOT_USERS in the environment.")
    for username, password in CODEPILOT_USERS.items():
        if hmac.compare_digest(credentials.username, username) and hmac.compare_digest(credentials.password, password):
            return username
    raise HTTPException(status_code=401, detail="Invalid CodePilot username or password.", headers={"WWW-Authenticate": "Basic"})


def read_owners() -> dict:
    try:
        return json.loads(OWNER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_owners(owners: dict) -> None:
    temp = OWNER_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(owners), encoding="utf-8")
    temp.replace(OWNER_FILE)


def get_project(project_id: str, username: str) -> Path:
    if not project_id.isalnum() or len(project_id) != 32:
        raise HTTPException(status_code=400, detail="Invalid project ID.")
    if read_owners().get(project_id) != username:
        # Do not reveal whether another user's project exists.
        raise HTTPException(status_code=404, detail="Project not found.")
    project_dir = (UPLOAD_DIR / project_id).resolve()
    if not project_dir.is_relative_to(UPLOAD_DIR.resolve()) or not project_dir.is_dir():
        raise HTTPException(status_code=404, detail="Project not found.")
    return project_dir


def get_safe_file(project_dir: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts or "\\" in relative_path or ":" in relative_path:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    target = (project_dir / Path(*relative.parts)).resolve()
    if not target.is_relative_to(project_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid file path.")
    return target


def file_map(directory: Path) -> dict[str, Path]:
    found = {}
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(directory).as_posix()
        if rel.startswith(".codepilot-") or "/.codepilot-" in rel:
            continue
        found[rel] = path
    return found


def make_review(project_dir: Path, staged_dir: Path) -> list[dict]:
    before, after = file_map(project_dir), file_map(staged_dir)
    paths = sorted(set(before) | set(after))
    changes = []
    for rel in paths:
        old = before.get(rel)
        new = after.get(rel)
        if old and new and old.read_bytes() == new.read_bytes():
            continue
        if len(changes) >= MAX_REVIEW_FILES:
            raise HTTPException(status_code=413, detail="Too many changed files to review at once (limit 200).")
        try:
            old_lines = old.read_text(encoding="utf-8").splitlines(keepends=True) if old and old.stat().st_size <= 500_000 else []
            new_lines = new.read_text(encoding="utf-8").splitlines(keepends=True) if new and new.stat().st_size <= 500_000 else []
            diff = "".join(unified_diff(old_lines, new_lines, fromfile=f"before/{rel}" if old else "/dev/null", tofile=f"after/{rel}" if new else "/dev/null"))
            if not diff and (old or new):
                diff = "Binary or large file changed; content preview omitted."
        except (UnicodeDecodeError, OSError):
            diff = "Binary or large file changed; content preview omitted."
        changes.append({"path": rel, "status": "added" if not old else ("deleted" if not new else "modified"), "diff": diff[:30000]})
    return changes


@app.get("/")
def home(username: str = Depends(require_user)):
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/health")
def health_check():
    return {"status": "running"}

@app.post("/upload-project")
async def upload_project(file: UploadFile = File(...), username: str = Depends(require_user)):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a ZIP file.")
    data = await file.read(MAX_UPLOAD_SIZE + 1)
    await file.close()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="ZIP file exceeds 50 MB.")
    project_id = uuid4().hex
    project_dir = UPLOAD_DIR / project_id
    project_dir.mkdir(parents=True)
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                raise ValueError("ZIP contains too many entries.")
            total_size, seen_paths = 0, set()
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                relative = PurePosixPath(name)
                if not name or relative.is_absolute() or ".." in relative.parts or ":" in name:
                    raise ValueError("ZIP contains an unsafe path.")
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("ZIP contains an unsupported file type.")
                normalized = relative.as_posix().rstrip("/")
                if normalized in seen_paths:
                    raise ValueError("ZIP contains duplicate paths.")
                seen_paths.add(normalized)
                total_size += entry.file_size
                if total_size > MAX_EXTRACTED_SIZE:
                    raise ValueError("Extracted project exceeds 200 MB.")
                target = (project_dir / Path(*relative.parts)).resolve()
                if not target.is_relative_to(project_dir.resolve()):
                    raise ValueError("ZIP contains an unsafe path.")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
        owners = read_owners()
        owners[project_id] = username
        write_owners(owners)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as error:
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Could not upload project: {error}")
    return {"message": "Project uploaded successfully.", "project_id": project_id,
            "file_count": sum(1 for path in project_dir.rglob("*") if path.is_file())}

@app.get("/project-files/{project_id}")
def get_project_files(project_id: str, username: str = Depends(require_user)):
    project_dir = get_project(project_id, username)
    files = []
    for path in sorted(project_dir.rglob("*")):
        relative = path.relative_to(project_dir)
        if any(part.startswith(".") for part in relative.parts) or path.is_symlink():
            continue
        if not path.is_file() and not path.is_dir():
            continue
        files.append({"path": relative.as_posix(), "name": path.name, "type": "folder" if path.is_dir() else "file"})
    return {"project_id": project_id, "files": files}

@app.get("/project-file/{project_id}")
def read_project_file(project_id: str, path: str, username: str = Depends(require_user)):
    project_dir = get_project(project_id, username)
    file_path = get_safe_file(project_dir, path)
    if not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(status_code=404, detail="File not found.")
    if file_path.stat().st_size > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files larger than 1 MB cannot be previewed.")
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="This file is not a readable UTF-8 text file.")
    return {"path": path, "content": content}

@app.post("/agent")
def execute_agent(request: AgentRequest, username: str = Depends(require_user)):
    if not request.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty.")
    project_dir = get_project(request.project_id, username)
    staged_dir = STAGING_DIR / username / request.project_id
    shutil.rmtree(staged_dir, ignore_errors=True)
    staged_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project_dir, staged_dir, symlinks=False)
    try:
        result = run_agent(request.task, str(staged_dir))
        changes = make_review(project_dir, staged_dir)
        if not changes:
            shutil.rmtree(staged_dir, ignore_errors=True)
        return {"result": result, "project_id": request.project_id, "changes": changes,
                "needs_approval": bool(changes)}
    except HTTPException:
        shutil.rmtree(staged_dir, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(staged_dir, ignore_errors=True)
        message = str(error)
        if "429" in message or "quota" in message.lower():
            raise HTTPException(status_code=429, detail="Gemini rate limit reached. Wait a little and try again.")
        if "503" in message or "unavailable" in message.lower():
            raise HTTPException(status_code=503, detail="Gemini is temporarily busy. Please try again shortly.")
        raise HTTPException(status_code=502, detail=f"The AI task failed: {message[:300]}")

@app.post("/apply-changes/{project_id}")
def apply_changes(project_id: str, username: str = Depends(require_user)):
    project_dir = get_project(project_id, username)
    staged_dir = STAGING_DIR / username / project_id
    if not staged_dir.is_dir():
        raise HTTPException(status_code=404, detail="No pending review was found. Run the task again.")
    before, after = file_map(project_dir), file_map(staged_dir)
    changed = [p for p in sorted(set(before) | set(after)) if p not in before or p not in after or before[p].read_bytes() != after[p].read_bytes()]
    if len(changed) > MAX_REVIEW_FILES:
        raise HTTPException(status_code=413, detail="Too many changed files.")
    for rel in changed:
        target = get_safe_file(project_dir, rel)
        staged = staged_dir / rel
        if rel not in after:
            if target.exists() and target.is_file() and not target.is_symlink():
                target.unlink()
            continue
        if staged.is_symlink() or not staged.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".codepilot-tmp")
        shutil.copyfile(staged, temp)
        temp.replace(target)
    shutil.rmtree(staged_dir, ignore_errors=True)
    test_result = run_safe_checks(project_dir)
    return {"message": "Approved changes applied.", "changed_files": changed, "tests": test_result}

@app.delete("/review/{project_id}")
def discard_review(project_id: str, username: str = Depends(require_user)):
    get_project(project_id, username)
    staged_dir = STAGING_DIR / username / project_id
    shutil.rmtree(staged_dir, ignore_errors=True)
    return {"message": "Pending changes discarded."}


def run_safe_checks(project_dir: Path) -> dict:
    """Run syntax-only checks; do not execute arbitrary uploaded test scripts automatically."""
    checks = []
    py_files = [str(p) for p in project_dir.rglob("*.py") if p.is_file() and not p.is_symlink()]
    if py_files:
        try:
            result = subprocess.run([sys.executable, "-m", "compileall", "-q", str(project_dir)],
                                    cwd=project_dir, capture_output=True, text=True, timeout=20)
            checks.append({"name": "Python syntax", "passed": result.returncode == 0,
                           "output": (result.stdout + result.stderr)[-5000:] or ("Passed" if result.returncode == 0 else "Failed")})
        except subprocess.TimeoutExpired:
            checks.append({"name": "Python syntax", "passed": False, "output": "Timed out."})
    js_files = [p for p in project_dir.rglob("*.js") if p.is_file() and not p.is_symlink()]
    if js_files and shutil.which("node"):
        for path in js_files[:50]:
            try:
                result = subprocess.run(["node", "--check", str(path)], cwd=project_dir,
                                        capture_output=True, text=True, timeout=10)
                checks.append({"name": f"JavaScript syntax: {path.relative_to(project_dir)}", "passed": result.returncode == 0,
                               "output": (result.stdout + result.stderr)[-3000:] or ("Passed" if result.returncode == 0 else "Failed")})
            except subprocess.TimeoutExpired:
                checks.append({"name": f"JavaScript syntax: {path.relative_to(project_dir)}", "passed": False, "output": "Timed out."})
    if not checks:
        checks.append({"name": "Automatic syntax checks", "passed": True, "output": "No supported Python or JavaScript files found. No syntax checks were run."})
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "note": "These are syntax checks, not a full test suite."}

@app.get("/download-project/{project_id}")
def download_project(project_id: str, username: str = Depends(require_user)):
    project_dir = get_project(project_id, username)
    temp_file = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zip_path = temp_file.name
    temp_file.close()
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for root, dirs, filenames in os.walk(project_dir, followlinks=False):
                root_path = Path(root)
                dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]
                for filename in filenames:
                    file_path = root_path / filename
                    if file_path.is_symlink() or not file_path.is_file():
                        continue
                    archive.write(file_path, arcname=file_path.relative_to(project_dir))
        return FileResponse(path=zip_path, media_type="application/zip", filename="codepilot-project.zip",
                            background=BackgroundTask(os.unlink, zip_path))
    except Exception:
        if os.path.exists(zip_path): os.unlink(zip_path)
        raise
