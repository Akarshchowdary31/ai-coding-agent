
import json

from google.genai import types
from app.services.llm import ask_llm
from app.services.tools import build_tool_registry


MAX_ITERATIONS = 10


def run_agent(task: str, project_path: str) -> str:
    registry = build_tool_registry(project_path)

    tool_declarations = [
        types.FunctionDeclaration(
            name="list_files",
            description="List files in the project or a subfolder.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING)
                },
            ),
        ),
        types.FunctionDeclaration(
            name="read_file",
            description="Read a project's text file.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING)
                },
                required=["path"],
            ),
        ),
        types.FunctionDeclaration(
            name="write_file",
            description="Create or overwrite a project file.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING),
                    "content": types.Schema(type=types.Type.STRING),
                },
                required=["path", "content"],
            ),
        ),
        types.FunctionDeclaration(
            name="edit_file",
            description="Replace the first occurrence of text in a file.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path": types.Schema(type=types.Type.STRING),
                    "old_text": types.Schema(type=types.Type.STRING),
                    "new_text": types.Schema(type=types.Type.STRING),
                },
                required=["path", "old_text", "new_text"],
            ),
        ),
        types.FunctionDeclaration(
            name="search_files",
            description="Search project files for matching text.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "query": types.Schema(type=types.Type.STRING),
                    "path": types.Schema(type=types.Type.STRING),
                },
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_command",
            description="Run only syntax checks: Python compileall or Node --check. Other commands are blocked.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "command": types.Schema(type=types.Type.STRING)
                },
                required=["command"],
            ),
        ),
    ]

    tool = types.Tool(function_declarations=tool_declarations)

    history = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(
                        "You are CodePilot, an AI coding agent working on a temporary copy. "
                        "Inspect relevant files before editing. "
                        "Use the available tools to complete the task. "
                        "Only make changes related to the user task. "
                        "Do not try to bypass command restrictions. "
                        "Never claim a change succeeded unless a tool confirms it.\n\n"
                        f"User task: {task}"
                    )
                )
            ],
        )
    ]

    for _ in range(MAX_ITERATIONS):
        response = ask_llm(
            history,
            tools=[tool],
        )

        if not response.candidates:
            return "The model returned no response. Please try again."

        candidate = response.candidates[0]
        if candidate.content:
            history.append(candidate.content)

        function_calls = response.function_calls or []

        if not function_calls:
            return response.text or "The model returned no text response."

        for call in function_calls:
            name = call.name
            args = dict(call.args or {})

            if name not in registry:
                result = {"error": f"Unknown tool: {name}"}
            else:
                try:
                    result = registry[name](**args)
                except Exception as error:
                    result = {"error": str(error)}

            history.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=name,
                            response={"result": result},
                        )
                    ],
                )
            )

    return (
        "Stopped after reaching the maximum number of steps. "
        "Please check the project files to see what was completed."
    )
