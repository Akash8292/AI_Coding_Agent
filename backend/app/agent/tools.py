"""
Agent tool definitions and registry.
Each tool has: name, description, parameters, danger_level.
Danger levels: safe, moderate, dangerous
"""
from dataclasses import dataclass, field
from typing import Callable, Any

DANGER_SAFE = "safe"
DANGER_MODERATE = "moderate"
DANGER_DANGEROUS = "dangerous"


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict  # JSON Schema
    danger_level: str = DANGER_SAFE
    category: str = "general"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "danger_level": self.danger_level,
            "category": self.category,
        }


TOOLS: dict[str, ToolDef] = {
    # ── Repository tools ─────────────────────────────────────────────────
    "list_files": ToolDef(
        name="list_files",
        description="List all files in the repository with their paths",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Optional subfolder to list"},
        }},
        danger_level=DANGER_SAFE,
        category="repository",
    ),
    "search_code": ToolDef(
        name="search_code",
        description="Search the repository for code relevant to a query using BM25 keyword search",
        parameters={"type": "object", "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "default": 6},
        }, "required": ["query"]},
        danger_level=DANGER_SAFE,
        category="repository",
    ),
    "read_file": ToolDef(
        name="read_file",
        description="Read the full contents of a file in the repository",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to repo root"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        }, "required": ["path"]},
        danger_level=DANGER_SAFE,
        category="repository",
    ),

    # ── Git tools ────────────────────────────────────────────────────────
    "git_status": ToolDef(
        name="git_status",
        description="Show the current git status of the repository",
        parameters={"type": "object", "properties": {}},
        danger_level=DANGER_SAFE,
        category="git",
    ),
    "git_diff": ToolDef(
        name="git_diff",
        description="Show git diff for the repository or a specific file",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Optional file path"},
        }},
        danger_level=DANGER_SAFE,
        category="git",
    ),
    "git_log": ToolDef(
        name="git_log",
        description="Show recent git commit history",
        parameters={"type": "object", "properties": {
            "n": {"type": "integer", "default": 10, "description": "Number of commits"},
        }},
        danger_level=DANGER_SAFE,
        category="git",
    ),

    # ── Modification tools ───────────────────────────────────────────────
    "edit_file": ToolDef(
        name="edit_file",
        description="Propose an edit to a file. Shows a diff for user review before applying.",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "instruction": {"type": "string"},
        }, "required": ["path", "instruction"]},
        danger_level=DANGER_MODERATE,
        category="modification",
    ),
    "create_file": ToolDef(
        name="create_file",
        description="Create a new file in the repository",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
        danger_level=DANGER_MODERATE,
        category="modification",
    ),
    "delete_file": ToolDef(
        name="delete_file",
        description="Delete a file from the repository",
        parameters={"type": "object", "properties": {
            "path": {"type": "string"},
        }, "required": ["path"]},
        danger_level=DANGER_DANGEROUS,
        category="modification",
    ),

    # ── Execution tools ──────────────────────────────────────────────────
    "run_command": ToolDef(
        name="run_command",
        description="Run a shell command in the repository directory",
        parameters={"type": "object", "properties": {
            "command": {"type": "string"},
            "reason": {"type": "string", "description": "Why this command is needed"},
        }, "required": ["command"]},
        danger_level=DANGER_DANGEROUS,
        category="execution",
    ),
    "run_tests": ToolDef(
        name="run_tests",
        description="Run the project test suite",
        parameters={"type": "object", "properties": {
            "command": {"type": "string", "description": "Test command, e.g. 'pytest -q'"},
        }},
        danger_level=DANGER_MODERATE,
        category="execution",
    ),
}


def get_tool(name: str) -> ToolDef | None:
    return TOOLS.get(name)


def get_safe_tools() -> list[ToolDef]:
    return [t for t in TOOLS.values() if t.danger_level == DANGER_SAFE]


def all_tools_schema() -> list[dict]:
    """Return tool list in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS.values()
    ]
