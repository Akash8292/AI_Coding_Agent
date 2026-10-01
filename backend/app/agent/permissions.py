"""
Command permission policy and sandboxed execution.

Every command the agent proposes is classified before the user sees it:
  blocked    never runs, even with approval (destructive / exfiltration / escalation)
  dangerous  runs only after explicit approval (installs, git writes, network)
  moderate   runs only after explicit approval (tests, builds, scripts)
Nothing runs without an explicit approval from the owning user; "Allow for
session" is just the client re-approving the same command string.

Execution: no shell (argument vector only), cwd pinned to the workspace, a
scrubbed environment (no API keys / secrets from the server process), a hard
timeout that kills the whole process tree, and capped output.
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
import time

SHELL_METACHARS = re.compile(r"[;&|`$<>]|\$\(|\n")

BLOCKED_PATTERNS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b", "recursive force delete"),
    (r"\brm\s+-rf?\b|\brmdir\s+/s\b|\bdel\s+/[sq]\b|\brd\s+/s\b", "recursive delete"),
    (r"\b(mkfs|fdisk|diskpart|format)\b", "disk formatting"),
    (r"\bdd\s+if=", "raw disk write"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "system power control"),
    (r"\b(sudo|su|doas|runas)\b", "privilege escalation"),
    (r"\bchmod\s+-R\b|\bchown\s+-R\b|\bicacls\b|\btakeown\b", "recursive permission change"),
    (r"\bgit\s+(push\s+.*--force|push\s+-f\b|reset\s+--hard|clean\s+-[a-z]*f|filter-branch|reflog\s+expire)",
     "destructive git operation"),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)", "discards working tree changes"),
    (r"\b(curl|wget|iwr|invoke-webrequest)\b.*\|\s*(sh|bash|python|pwsh|powershell|iex)", "pipes remote code to a shell"),
    (r"\b(nc|ncat|netcat|telnet)\b", "raw network tools"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"(^|\s)(/etc/|/root/|~/.ssh|\.ssh/|\.aws/|\.env\b)", "touches credentials or system files"),
    (r"\bremove-item\b.*-recurse", "recursive delete"),
]
DANGEROUS_PATTERNS = [
    (r"^(pip3?|npm|yarn|pnpm|poetry|uv|cargo|go)\s+(install|add|remove|uninstall|update|upgrade|i)\b",
     "changes installed dependencies"),
    (r"^git\s+(commit|push|pull|merge|rebase|checkout|switch|stash|tag|branch\s+-[dD])\b", "changes git state"),
    (r"^(curl|wget)\b", "network access"),
    (r"^docker\b", "container control"),
    (r"^(rm|del|mv|move|cp|copy)\b", "moves or deletes files"),
]

SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|DATABASE_URL|_AUTH)", re.I)


def assess_command(command: str) -> tuple[str, str]:
    """Return (level, reason): blocked | dangerous | moderate."""
    cmd = (command or "").strip()
    if not cmd:
        return "blocked", "empty command"
    if len(cmd) > 1000:
        return "blocked", "command is too long"
    for pat, why in BLOCKED_PATTERNS:
        if re.search(pat, cmd, re.I):
            return "blocked", why
    if SHELL_METACHARS.search(cmd):
        return "blocked", "shell operators (; | & > $ `) are not supported — run one command at a time"
    for pat, why in DANGEROUS_PATTERNS:
        if re.search(pat, cmd, re.I):
            return "dangerous", why
    return "moderate", "runs a program in your workspace"


def _scrubbed_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONUNBUFFERED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["CI"] = "1"
    return env


def split_command(command: str) -> list[str]:
    argv = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a for a in argv]
    return argv


def run_command(repo_path: str, command: str, timeout: int = 60,
                max_output: int = 50_000) -> dict:
    """
    Execute an APPROVED command. Returns
    {ok, exit_code, output, duration_ms, timed_out, truncated, error}.
    """
    level, reason = assess_command(command)
    if level == "blocked":
        return {"ok": False, "exit_code": None, "output": "", "duration_ms": 0,
                "timed_out": False, "truncated": False, "error": f"Blocked: {reason}"}
    try:
        argv = split_command(command)
    except ValueError as e:
        return {"ok": False, "exit_code": None, "output": "", "duration_ms": 0,
                "timed_out": False, "truncated": False, "error": f"Could not parse command: {e}"}
    exe = shutil.which(argv[0], path=_scrubbed_env().get("PATH")) or shutil.which(argv[0])
    if not exe:
        return {"ok": False, "exit_code": None, "output": "", "duration_ms": 0,
                "timed_out": False, "truncated": False, "error": f"Program not found: {argv[0]}"}
    argv[0] = exe

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=repo_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=_scrubbed_env(), **kwargs)
    except OSError as e:
        return {"ok": False, "exit_code": None, "output": "", "duration_ms": 0,
                "timed_out": False, "truncated": False, "error": str(e)}
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, _ = proc.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)
    text = (out or b"").decode("utf-8", errors="replace")
    truncated = len(text) > max_output
    if truncated:
        text = text[: max_output // 2] + "\n\n… [output truncated] …\n\n" + text[-max_output // 2:]
    return {
        "ok": (proc.returncode == 0) and not timed_out,
        "exit_code": proc.returncode,
        "output": text,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "truncated": truncated,
        "error": f"Command timed out after {timeout}s and was stopped" if timed_out else None,
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
