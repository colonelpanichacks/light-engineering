"""Nexus Runner -- Command sandboxing and security."""

import os
import re
import shlex
import subprocess

# Commands the agent is allowed to run
ALLOWED_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "date", "cal",
    "df", "uptime", "whoami", "pwd", "echo", "which",
    "brew", "ollama", "tart",
    "open",
}

# Subcommand restrictions for multi-word tools
ALLOWED_SUBCOMMANDS = {
    "brew": {"list", "info", "search"},
    "ollama": {"list", "ps", "show"},
    "tart": {"list", "ip"},
}

# Paths the agent can access (expanded at runtime)
HOME = os.path.expanduser("~")
ALLOWED_PATHS = [
    os.path.join(HOME, "Desktop"),
    os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Downloads"),
    "/tmp",
]

# Paths that are always blocked
BLOCKED_PATHS = [
    os.path.join(HOME, ".ssh"),
    os.path.join(HOME, ".aws"),
    os.path.join(HOME, ".gnupg"),
    os.path.join(HOME, ".config"),
    os.path.join(HOME, ".env"),
    "/etc",
    "/var",
    "/System",
    "/Library",
]

# Shell metacharacters that indicate injection attempts
DANGEROUS_PATTERNS = re.compile(r'[;|&`$()]|&&|\|\||>>|<<')

MAX_OUTPUT = 2000
TIMEOUT = 30


def validate_command(command: str) -> str | None:
    """Validate a command against the allowlist.

    Returns None if allowed, or an error message if blocked.
    """
    if not command or not command.strip():
        return "Empty command"

    # Block shell metacharacters
    if DANGEROUS_PATTERNS.search(command):
        return "Blocked: shell metacharacters not allowed"

    try:
        parts = shlex.split(command)
    except ValueError:
        return "Blocked: malformed command"

    if not parts:
        return "Empty command"

    base = parts[0]

    # Resolve to basename (in case of full path)
    base_name = os.path.basename(base)

    if base_name not in ALLOWED_COMMANDS:
        return f"Blocked: '{base_name}' is not an allowed command"

    # Check subcommand restrictions
    if base_name in ALLOWED_SUBCOMMANDS and len(parts) > 1:
        sub = parts[1]
        if sub not in ALLOWED_SUBCOMMANDS[base_name]:
            return f"Blocked: '{base_name} {sub}' is not allowed"

    # Check path arguments against blocked paths
    for arg in parts[1:]:
        if arg.startswith("-"):
            continue
        expanded = os.path.expanduser(arg)
        abs_path = os.path.abspath(expanded)
        for blocked in BLOCKED_PATHS:
            if abs_path.startswith(blocked):
                return f"Blocked: access to {blocked} is not allowed"

    return None


def run_command(command: str) -> str:
    """Run a validated command and return output."""
    error = validate_command(command)
    if error:
        return error

    try:
        parts = shlex.split(command)
        result = subprocess.run(
            parts,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=HOME,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        output = output.strip()
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n... (truncated)"
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out (30s limit)"
    except FileNotFoundError:
        return f"Command not found: {parts[0]}"
    except Exception as e:
        return f"Error: {e}"
