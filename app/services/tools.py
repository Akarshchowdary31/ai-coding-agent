from pathlib import Path
import shlex
import subprocess

# Deliberately small command allowlist. This is basic hardening, not a full sandbox.
ALLOWED_COMMANDS = {"python", "python3", "pytest", "node", "npm"}


def build_tool_registry(project_path: str) -> dict:
    workspace = Path(project_path).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Project path must be an existing directory.")

    def safe_path(path: str) -> Path:
        target = (workspace / path).resolve()
        if not target.is_relative_to(workspace):
            raise ValueError("Path must stay inside the selected project.")
        return target

    def list_files(path: str = ".") -> list[str]:
        directory = safe_path(path)
        if not directory.is_dir():
            raise ValueError(f"Directory does not exist: {path}")
        return [str(item.relative_to(workspace)) for item in directory.rglob("*")
                if item.is_file() and not item.is_symlink()]

    def read_file(path: str) -> str:
        file_path = safe_path(path)
        if not file_path.is_file() or file_path.is_symlink():
            raise FileNotFoundError(f"File does not exist: {path}")
        if file_path.stat().st_size > 1_000_000:
            raise ValueError("Files larger than 1 MB cannot be read by the agent.")
        return file_path.read_text(encoding="utf-8")

    def write_file(path: str, content: str) -> str:
        file_path = safe_path(path)
        if file_path.is_symlink():
            raise ValueError("Writing through symbolic links is not allowed.")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"File written: {path}"

    def edit_file(path: str, old_text: str, new_text: str) -> str:
        file_path = safe_path(path)
        if not file_path.is_file() or file_path.is_symlink():
            raise FileNotFoundError(f"File does not exist: {path}")
        content = file_path.read_text(encoding="utf-8")
        if old_text not in content:
            raise ValueError("Text to replace was not found.")
        file_path.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"File edited: {path}"

    def search_files(query: str, path: str = ".") -> list[dict]:
        directory = safe_path(path)
        if not directory.is_dir():
            raise ValueError(f"Directory does not exist: {path}")
        matches = []
        for file_path in directory.rglob("*"):
            if not file_path.is_file() or file_path.is_symlink() or file_path.stat().st_size > 1_000_000:
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, start=1):
                if query.casefold() in line.casefold():
                    matches.append({"file": str(file_path.relative_to(workspace)), "line": number, "text": line.strip()})
                    if len(matches) >= 500:
                        return matches
        return matches

    def run_command(command: str) -> dict:
        try:
            args = shlex.split(command)
        except ValueError as error:
            raise ValueError("Invalid command format.") from error
        # Only parser/compile checks are allowed. Never run arbitrary scripts or package scripts.
        if len(args) == 3 and Path(args[0]).name in {"python", "python3"} and args[1:2] == ["-m"] and args[2] == "compileall":
            safe_args = [args[0], "-m", "compileall", "-q", "."]
        elif len(args) == 3 and Path(args[0]).name == "node" and args[1] == "--check":
            target = safe_path(args[2])
            if not target.is_file() or target.is_symlink():
                raise ValueError("JavaScript file not found inside the project.")
            safe_args = [args[0], "--check", str(target)]
        else:
            raise ValueError("Command blocked. Only Python compileall and Node --check syntax checks are allowed.")
        result = subprocess.run(safe_args, cwd=workspace, capture_output=True, text=True,
                                timeout=20, shell=False,
                                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(workspace)})
        return {"command": command, "return_code": result.returncode,
                "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:]}

    return {"list_files": list_files, "read_file": read_file, "write_file": write_file,
            "edit_file": edit_file, "search_files": search_files, "run_command": run_command}
