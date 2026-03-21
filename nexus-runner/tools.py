"""Nexus Runner -- Tool definitions and handlers."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime

from security import run_command as secure_run_command

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(SCRIPT_DIR, "skills")

# Detect current platform
if sys.platform == "darwin":
    CURRENT_PLATFORM = "macos"
elif sys.platform.startswith("linux"):
    CURRENT_PLATFORM = "linux"
elif sys.platform == "win32":
    CURRENT_PLATFORM = "windows"
else:
    CURRENT_PLATFORM = "common"

# Skill subdirectories: OS-specific + common (always loaded)
SKILLS_OS_DIR = os.path.join(SKILLS_DIR, CURRENT_PLATFORM)
SKILLS_COMMON_DIR = os.path.join(SKILLS_DIR, "common")

# Ensure skill directories exist
for _d in (SKILLS_DIR, SKILLS_OS_DIR, SKILLS_COMMON_DIR):
    os.makedirs(_d, exist_ok=True)

# -- Tool definitions (OpenAI-compatible format for Ollama) --

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current date and time",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Add a reminder that the agent will surface contextually",
            "parameters": {
                "type": "object",
                "properties": {
                    "trigger": {
                        "type": "string",
                        "description": "When to surface: 'morning', 'evening', a keyword, or a date like '2026-03-20'",
                    },
                    "content": {
                        "type": "string",
                        "description": "What to remind the user about",
                    },
                    "expires": {
                        "type": "string",
                        "description": "When to auto-remove: a date or 'never' (default: never)",
                    },
                },
                "required": ["trigger", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event",
            "description": "Create an event in Apple Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time": {"type": "string", "description": "Start time in HH:MM format (24h)"},
                    "duration_minutes": {"type": "integer", "description": "Duration in minutes (default: 60)"},
                },
                "required": ["title", "date", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_soul",
            "description": "Update the agent's persistent memory about the user or learned preferences",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["about_user", "learned_preferences", "highlights"],
                        "description": "Which section to update",
                    },
                    "content": {"type": "string", "description": "Content to append to the section"},
                },
                "required": ["section", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command on the Mac (allowlisted commands only)",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open a macOS application by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Application name (e.g. 'Safari', 'Terminal', 'Finder', 'Calendar')",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a command or script to run at a specific time on the Mac",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short name for the task (alphanumeric, no spaces)"},
                    "command": {"type": "string", "description": "Shell command to run"},
                    "when": {
                        "type": "string",
                        "description": "When to run: 'YYYY-MM-DD HH:MM' for one-time, or cron-style '0 9 * * *' for repeating",
                    },
                    "repeat": {
                        "type": "boolean",
                        "description": "If true, uses cron-style repeating schedule. If false, runs once (default: false)",
                    },
                },
                "required": ["name", "command", "when"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_tasks",
            "description": "List all scheduled tasks created by Nexus Runner",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_scheduled_task",
            "description": "Remove a scheduled task by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Task name to remove"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Execute a code block in Python or bash and return the output",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash"],
                        "description": "Language to execute",
                    },
                    "code": {"type": "string", "description": "Code to execute"},
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Create a reusable skill (saved script the agent can run later by name)",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name (alphanumeric, dashes ok)"},
                    "description": {"type": "string", "description": "What this skill does"},
                    "language": {"type": "string", "enum": ["python", "bash"], "description": "Script language"},
                    "code": {"type": "string", "description": "The script code"},
                    "platform": {
                        "type": "string",
                        "enum": ["macos", "linux", "windows", "common"],
                        "description": "Target platform (default: current OS). Use 'common' for OS-agnostic skills.",
                    },
                },
                "required": ["name", "description", "language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "Run a previously saved skill by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name to run"},
                    "args": {"type": "string", "description": "Optional arguments to pass to the skill"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List all saved skills",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_soul",
            "description": "Replace the entire soul.md file with new content (use for major personality/identity changes)",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full new content for soul.md"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save a fact, preference, or piece of knowledge to long-term memory (persists across all sessions)",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["facts", "preferences", "knowledge"],
                        "description": "Memory category: facts (about user/environment), preferences (user likes/dislikes), knowledge (useful info)",
                    },
                    "content": {"type": "string", "description": "What to remember"},
                },
                "required": ["category", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search long-term memory for relevant information",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for (optional -- omit to get all memories)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the filesystem and return its contents (text files only, max 4000 chars)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_spotify",
            "description": "Search Spotify for a song, artist, album, or playlist and open the results",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for (song name, artist, album, etc.)"},
                    "type": {
                        "type": "string",
                        "enum": ["track", "artist", "album", "playlist"],
                        "description": "Type of search (default: track)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_instagram",
            "description": "Search Instagram for a user profile, hashtag, or content",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Username, hashtag, or search term"},
                    "type": {
                        "type": "string",
                        "enum": ["user", "hashtag", "search"],
                        "description": "Type of search: 'user' for profile, 'hashtag' for tag page, 'search' for general (default: search)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse",
            "description": "Control Brave browser. IMPORTANT: Use action='open' with a url to launch Brave AND navigate in one step. If Brave is already running without CDP, it will be restarted automatically. After opening, use navigate/click/fill/get_text etc for further interaction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "open", "navigate", "click", "fill", "submit", "scroll",
                            "get_text", "get_title", "get_url", "list_links", "list_inputs",
                            "screenshot", "wait_for", "run_js", "new_tab",
                        ],
                        "description": "Browser action. Use 'open' first (can include url to navigate immediately).",
                    },
                    "url": {"type": "string", "description": "URL for open/navigate/new_tab actions"},
                    "selector": {"type": "string", "description": "CSS selector for click/fill/submit/wait_for"},
                    "value": {"type": "string", "description": "Value for fill action or JS expression for run_js"},
                    "direction": {"type": "string", "description": "Scroll direction: up/down/top/bottom (default: down)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_settings",
            "description": "Modify agent settings like voice, speech rate, audio output device, wake duration, or greeting",
            "parameters": {
                "type": "object",
                "properties": {
                    "setting": {
                        "type": "string",
                        "enum": ["voice", "rate", "audio_device", "wake_duration", "query_duration", "greeting"],
                        "description": "Which setting to change. audio_device accepts a device name like 'MacBook Pro Speakers' or 'External Headphones'.",
                    },
                    "value": {"type": "string", "description": "New value for the setting"},
                },
                "required": ["setting", "value"],
            },
        },
    },
]

# -- Section headers in soul.md for update_soul --
SOUL_SECTIONS = {
    "about_user": "## About the User",
    "learned_preferences": "## Learned Preferences",
    "highlights": "## Conversation History Highlights",
}

# -- Allowed apps for open_app --
ALLOWED_APPS = {
    "safari", "finder", "terminal", "calendar", "notes", "reminders",
    "messages", "mail", "music", "photos", "preview", "textedit",
    "calculator", "activity monitor", "system preferences", "system settings",
    "visual studio code", "iterm", "iterm2", "slack", "discord", "spotify",
    "firefox", "chrome", "google chrome", "brave browser",
}

PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")


# -- Handlers --

def handle_get_time() -> str:
    now = datetime.now()
    return now.strftime("It's %A, %B %d, %Y at %I:%M %p")


def handle_set_reminder(trigger: str, content: str, expires: str = "never") -> str:
    reminders_path = os.path.join(SCRIPT_DIR, "reminders.md")

    try:
        with open(reminders_path) as f:
            text = f.read()
    except FileNotFoundError:
        text = "# Nexus Runner Reminders\n\n## Active Reminders\n\n(none yet)\n"

    today = datetime.now().strftime("%Y-%m-%d")
    entry = f"\n- trigger: {trigger}\n  content: {content}\n  created: {today}\n  expires: {expires}\n"

    if "(none yet)" in text:
        text = text.replace("(none yet)", entry.strip())
    else:
        marker = "## Active Reminders"
        idx = text.find(marker)
        if idx != -1:
            insert_at = idx + len(marker)
            text = text[:insert_at] + "\n" + entry + text[insert_at:]
        else:
            text += "\n## Active Reminders\n" + entry

    with open(reminders_path, "w") as f:
        f.write(text)

    return f"Reminder set: '{content}' (trigger: {trigger})"


def handle_add_calendar_event(
    title: str, date: str, time: str, duration_minutes: int = 60
) -> str:
    title = title.replace('"', '\\"').replace("'", "\\'")[:200]

    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return f"Invalid date/time format. Use YYYY-MM-DD and HH:MM. Got: {date} {time}"

    from datetime import timedelta
    end_dt = dt + timedelta(minutes=duration_minutes)
    year, month, day = dt.year, dt.month, dt.day
    hour, minute = dt.hour, dt.minute
    end_year, end_month, end_day = end_dt.year, end_dt.month, end_dt.day
    end_hour, end_minute = end_dt.hour, end_dt.minute

    script = f'''
    tell application "Calendar"
        tell calendar "Calendar"
            set startDate to current date
            set year of startDate to {year}
            set month of startDate to {month}
            set day of startDate to {day}
            set hours of startDate to {hour}
            set minutes of startDate to {minute}
            set seconds of startDate to 0
            set endDate to current date
            set year of endDate to {end_year}
            set month of endDate to {end_month}
            set day of endDate to {end_day}
            set hours of endDate to {end_hour}
            set minutes of endDate to {end_minute}
            set seconds of endDate to 0
            make new event with properties {{summary:"{title}", start date:startDate, end date:endDate}}
        end tell
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return f"Calendar event created: '{title}' on {date} at {time}"
        return f"Calendar error: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "Calendar timed out"
    except Exception as e:
        return f"Calendar error: {e}"


def handle_update_soul(section: str, content: str) -> str:
    soul_path = os.path.join(SCRIPT_DIR, "soul.md")
    header = SOUL_SECTIONS.get(section)
    if not header:
        return f"Unknown section: {section}"

    try:
        with open(soul_path) as f:
            text = f.read()
    except FileNotFoundError:
        return "soul.md not found"

    idx = text.find(header)
    if idx == -1:
        return f"Section '{header}' not found in soul.md"

    section_start = idx + len(header)
    next_header = text.find("\n## ", section_start)
    insert_at = len(text) if next_header == -1 else next_header

    section_content = text[section_start:insert_at]
    placeholder_match = re.search(
        r'\(.*?fills this in.*?\)|\(.*?likes/dislikes.*?\)|\(.*?Notable interactions.*?\)',
        section_content,
    )
    if placeholder_match:
        ps = section_start + placeholder_match.start()
        pe = section_start + placeholder_match.end()
        text = text[:ps] + "- " + content + text[pe:]
    else:
        text = text[:insert_at].rstrip() + "\n- " + content + "\n" + text[insert_at:]

    with open(soul_path, "w") as f:
        f.write(text)

    return f"Soul updated ({section}): {content}"


def handle_run_command(command: str) -> str:
    return secure_run_command(command)


def handle_open_app(app_name: str) -> str:
    if app_name.lower() not in ALLOWED_APPS:
        return f"App '{app_name}' is not in the allowed list. Allowed: {', '.join(sorted(ALLOWED_APPS))}"
    try:
        subprocess.Popen(
            ["open", "-a", app_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return f"Opened {app_name}"
    except Exception as e:
        return f"Failed to open {app_name}: {e}"


def handle_schedule_task(
    name: str, command: str, when: str, repeat: bool = False
) -> str:
    # Sanitize name
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    if not safe_name:
        return "Invalid task name"

    label = f"com.nexusrunner.task.{safe_name}"
    plist_path = os.path.join(PLIST_DIR, f"{label}.plist")

    if repeat:
        # Parse cron-style: minute hour day month weekday
        parts = when.strip().split()
        if len(parts) != 5:
            return "Repeating schedule needs cron format: 'minute hour day month weekday' (e.g. '0 9 * * *')"

        cal_interval = {}
        labels = ["Minute", "Hour", "Day", "Month", "Weekday"]
        for val, key in zip(parts, labels):
            if val != "*":
                try:
                    cal_interval[key] = int(val)
                except ValueError:
                    return f"Invalid cron value '{val}' for {key}"

        interval_xml = "\n".join(
            f"            <key>{k}</key>\n            <integer>{v}</integer>"
            for k, v in cal_interval.items()
        )

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>{command}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
{interval_xml}
    </dict>
</dict>
</plist>"""
    else:
        # One-time: parse datetime
        try:
            dt = datetime.strptime(when.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return "One-time schedule needs format: 'YYYY-MM-DD HH:MM'"

        # Use at-style: write a script that runs once then unloads itself
        wrapper = f'{command} && launchctl unload "{plist_path}" && rm -f "{plist_path}"'
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>{wrapper}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Month</key>
        <integer>{dt.month}</integer>
        <key>Day</key>
        <integer>{dt.day}</integer>
        <key>Hour</key>
        <integer>{dt.hour}</integer>
        <key>Minute</key>
        <integer>{dt.minute}</integer>
    </dict>
</dict>
</plist>"""

    try:
        os.makedirs(PLIST_DIR, exist_ok=True)
        with open(plist_path, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", plist_path], capture_output=True, timeout=10)
        return f"Task '{safe_name}' scheduled: {when} ({'repeating' if repeat else 'one-time'})"
    except Exception as e:
        return f"Failed to schedule task: {e}"


def handle_list_scheduled_tasks() -> str:
    tasks = []
    if not os.path.isdir(PLIST_DIR):
        return "No scheduled tasks"

    for f in os.listdir(PLIST_DIR):
        if f.startswith("com.nexusrunner.task.") and f.endswith(".plist"):
            name = f.replace("com.nexusrunner.task.", "").replace(".plist", "")
            tasks.append(name)

    if not tasks:
        return "No scheduled tasks"
    return "Scheduled tasks: " + ", ".join(tasks)


def handle_remove_scheduled_task(name: str) -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    label = f"com.nexusrunner.task.{safe_name}"
    plist_path = os.path.join(PLIST_DIR, f"{label}.plist")

    if not os.path.isfile(plist_path):
        return f"Task '{safe_name}' not found"

    try:
        subprocess.run(["launchctl", "unload", plist_path], capture_output=True, timeout=10)
        os.remove(plist_path)
        return f"Task '{safe_name}' removed"
    except Exception as e:
        return f"Failed to remove task: {e}"


def handle_run_code(language: str, code: str) -> str:
    if language not in ("python", "bash"):
        return f"Unsupported language: {language}"

    cmd = ["python3", "-c", code] if language == "python" else ["bash", "-c", code]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=os.path.expanduser("~"),
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        output = output.strip()
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncated)"
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Code execution timed out (30s limit)"
    except Exception as e:
        return f"Execution error: {e}"


def handle_create_skill(name: str, description: str, language: str, code: str, platform: str = "") -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    if not safe_name:
        return "Invalid skill name"

    # Default to current OS if not specified
    if not platform:
        platform = CURRENT_PLATFORM

    # Pick the right subdirectory
    if platform == "common":
        target_dir = SKILLS_COMMON_DIR
    else:
        target_dir = os.path.join(SKILLS_DIR, platform)
        os.makedirs(target_dir, exist_ok=True)

    ext = ".py" if language == "python" else ".sh"
    skill_path = os.path.join(target_dir, safe_name + ext)
    meta_path = os.path.join(target_dir, safe_name + ".json")

    with open(skill_path, "w") as f:
        if language == "python":
            f.write("#!/usr/bin/env python3\n")
        else:
            f.write("#!/bin/bash\n")
        f.write(code)

    os.chmod(skill_path, 0o755)

    with open(meta_path, "w") as f:
        json.dump({
            "name": safe_name,
            "description": description,
            "language": language,
            "platform": platform,
            "created": datetime.now().isoformat(),
        }, f, indent=2)

    return f"Skill '{safe_name}' created ({language}, {platform})"


def _find_skill_file(safe_name: str) -> str | None:
    """Search OS-specific dir first, then common, for a skill script."""
    for search_dir in (SKILLS_OS_DIR, SKILLS_COMMON_DIR):
        for ext in (".py", ".sh"):
            path = os.path.join(search_dir, safe_name + ext)
            if os.path.isfile(path):
                return path
    return None


def _find_skill_meta(safe_name: str) -> str | None:
    """Search OS-specific dir first, then common, for a skill .json."""
    for search_dir in (SKILLS_OS_DIR, SKILLS_COMMON_DIR):
        path = os.path.join(search_dir, safe_name + ".json")
        if os.path.isfile(path):
            return path
    return None


def handle_run_skill(name: str, args: str = "") -> str:
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)

    skill_path = _find_skill_file(safe_name)
    if not skill_path:
        return f"Skill '{safe_name}' not found"

    cmd = [skill_path]
    if args:
        cmd.extend(args.split())

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=os.path.expanduser("~"),
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        output = output.strip()
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncated)"
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Skill timed out (60s limit)"
    except Exception as e:
        return f"Skill error: {e}"


def handle_list_skills() -> str:
    skills = []
    for search_dir in (SKILLS_OS_DIR, SKILLS_COMMON_DIR):
        if not os.path.isdir(search_dir):
            continue
        label = os.path.basename(search_dir)
        for f in sorted(os.listdir(search_dir)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(search_dir, f)) as mf:
                        meta = json.load(mf)
                    skills.append(f"{meta['name']}: {meta['description']} ({meta['language']}, {label})")
                except Exception:
                    continue

    if not skills:
        return "No skills created yet"
    return f"Skills (platform: {CURRENT_PLATFORM}):\n" + "\n".join(f"- {s}" for s in skills)


MEMORY_SECTIONS = {
    "facts": "## Facts",
    "preferences": "## Preferences",
    "knowledge": "## Knowledge",
}


def handle_save_memory(category: str, content: str) -> str:
    memory_path = os.path.join(SCRIPT_DIR, "memory.md")
    header = MEMORY_SECTIONS.get(category)
    if not header:
        return f"Unknown category: {category}"

    try:
        with open(memory_path) as f:
            text = f.read()
    except FileNotFoundError:
        text = "# Nexus Runner -- Long-Term Memory\n\n## Facts\n\n## Preferences\n\n## Knowledge\n"

    idx = text.find(header)
    if idx == -1:
        text += f"\n{header}\n\n- {content}\n"
    else:
        section_start = idx + len(header)
        next_header = text.find("\n## ", section_start)
        insert_at = len(text) if next_header == -1 else next_header

        # Remove placeholder text if present
        section_content = text[section_start:insert_at]
        placeholder = re.search(r'\(.*?\)', section_content)
        if placeholder and "Things" in placeholder.group() or placeholder and "User" in placeholder.group() or placeholder and "Useful" in placeholder.group():
            ps = section_start + placeholder.start()
            pe = section_start + placeholder.end()
            text = text[:ps] + "- " + content + text[pe:]
        else:
            text = text[:insert_at].rstrip() + "\n- " + content + "\n" + text[insert_at:]

    with open(memory_path, "w") as f:
        f.write(text)

    return f"Memory saved ({category}): {content}"


def handle_recall_memory(query: str = "") -> str:
    memory_path = os.path.join(SCRIPT_DIR, "memory.md")
    try:
        with open(memory_path) as f:
            text = f.read()
    except FileNotFoundError:
        return "No memories stored yet"

    if not query:
        return text

    # Simple keyword search across all memory entries
    lines = text.split("\n")
    matches = []
    query_lower = query.lower()
    for line in lines:
        if line.startswith("- ") and query_lower in line.lower():
            matches.append(line)

    if not matches:
        return f"No memories matching '{query}'"
    return "Matching memories:\n" + "\n".join(matches)


def handle_edit_soul(content: str) -> str:
    soul_path = os.path.join(SCRIPT_DIR, "soul.md")

    # Backup current soul
    backup_path = os.path.join(SCRIPT_DIR, "soul.md.bak")
    try:
        with open(soul_path) as f:
            with open(backup_path, "w") as bf:
                bf.write(f.read())
    except FileNotFoundError:
        pass

    with open(soul_path, "w") as f:
        f.write(content)

    return "Soul file replaced (backup saved as soul.md.bak)"


def handle_read_file(path: str) -> str:
    """Read a file and return its contents (text only, capped)."""
    # Expand ~ and resolve relative paths
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.path.expanduser("~"), expanded)

    if not os.path.isfile(expanded):
        return f"File not found: {path}"

    # Security: block sensitive paths
    blocked = [".ssh", ".aws", ".gnupg", ".env", "credentials", "secrets"]
    for b in blocked:
        if b in expanded.lower():
            return f"Access denied: cannot read files in sensitive paths"

    try:
        with open(expanded, errors="replace") as f:
            content = f.read(4000)
        if len(content) == 4000:
            content += "\n... (truncated at 4000 chars)"
        return content if content else "(empty file)"
    except Exception as e:
        return f"Error reading file: {e}"


def handle_search_spotify(query: str, type: str = "track") -> str:
    """Search Spotify using its URI scheme. Opens Spotify app with search results."""
    if type not in ("track", "artist", "album", "playlist"):
        type = "track"

    # Spotify URI scheme: spotify:search:query
    # URL-encode the query for safety
    from urllib.parse import quote
    encoded = quote(query)

    # Use the Spotify URI to open search directly in the app
    uri = f"spotify:search:{encoded}"
    try:
        subprocess.Popen(
            ["open", uri],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return f"Searching Spotify for {type}: '{query}'"
    except Exception as e:
        return f"Failed to search Spotify: {e}"


def handle_search_instagram(query: str, type: str = "search") -> str:
    """Search Instagram by opening it in the default browser."""
    from urllib.parse import quote

    query_clean = query.strip().lstrip("@#")

    if type == "user":
        url = f"https://www.instagram.com/{quote(query_clean)}/"
    elif type == "hashtag":
        url = f"https://www.instagram.com/explore/tags/{quote(query_clean)}/"
    else:
        url = f"https://www.instagram.com/explore/search/keyword/?q={quote(query_clean)}"

    try:
        subprocess.Popen(
            ["open", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if type == "user":
            return f"Opening Instagram profile: @{query_clean}"
        elif type == "hashtag":
            return f"Opening Instagram hashtag: #{query_clean}"
        return f"Searching Instagram for: '{query_clean}'"
    except Exception as e:
        return f"Failed to open Instagram: {e}"


def handle_browse(action: str, url: str = "", selector: str = "", value: str = "", direction: str = "down") -> str:
    """Control Brave browser via Chrome DevTools Protocol."""
    try:
        from browser import get_browser, open_brave_debug, close_browser
    except ImportError:
        return "Browser module not available (missing websocket-client?)"

    # Open/launch Brave with CDP
    if action == "open":
        result = open_brave_debug()
        # If a URL was also provided, navigate immediately
        if url:
            try:
                browser = get_browser()
                browser.navigate(url)
                title = browser.get_title() or "(loading)"
                return f"{result}. Navigated to {url} -- page title: {title}"
            except Exception as e:
                return f"{result}. Navigation failed: {e}"
        return f"{result}. Use browse(action='navigate', url='...') to go somewhere."

    # All other actions need a connection
    try:
        browser = get_browser()
    except RuntimeError as e:
        return f"Browser not connected: {e}. Use browse(action='open') first."
    except Exception as e:
        return f"Browser connection failed: {e}. Is Brave running with --remote-debugging-port=9222?"

    try:
        if action == "navigate":
            if not url:
                return "URL required for navigate"
            return browser.navigate(url)
        elif action == "click":
            if not selector:
                return "CSS selector required for click"
            return browser.click(selector)
        elif action == "fill":
            if not selector:
                return "CSS selector required for fill"
            return browser.fill(selector, value)
        elif action == "submit":
            return browser.submit(selector or "form")
        elif action == "scroll":
            return browser.scroll(direction)
        elif action == "get_text":
            return browser.get_text()
        elif action == "get_title":
            return browser.get_title() or "(no title)"
        elif action == "get_url":
            return browser.get_url() or "(unknown)"
        elif action == "list_links":
            links = browser.list_links()
            if not links:
                return "No links found"
            return "\n".join(f"- [{l.get('text', '')}]({l.get('href', '')})" for l in links)
        elif action == "list_inputs":
            inputs = browser.list_inputs()
            if not inputs:
                return "No input fields found"
            return "\n".join(
                f"- {i.get('tag')} type={i.get('type')} name={i.get('name')} id={i.get('id')} placeholder={i.get('placeholder')}"
                for i in inputs
            )
        elif action == "screenshot":
            path = browser.screenshot()
            return f"Screenshot saved to {path}"
        elif action == "wait_for":
            if not selector:
                return "CSS selector required for wait_for"
            return browser.wait_for(selector)
        elif action == "run_js":
            if not value:
                return "JS expression required in 'value' field"
            result = browser.js(value)
            return str(result) if result is not None else "(no return value)"
        elif action == "new_tab":
            return browser.new_tab(url or "about:blank")
        else:
            return f"Unknown browser action: {action}"
    except Exception as e:
        # Connection might be stale -- reset
        close_browser()
        return f"Browser error: {e}"


def handle_edit_settings(setting: str, value: str) -> str:
    """Edit avatar settings in avatars.py at runtime.
    Returns instruction for the brain to reload."""
    # We store runtime overrides in a JSON file
    overrides_path = os.path.join(SCRIPT_DIR, "settings_overrides.json")

    try:
        with open(overrides_path) as f:
            overrides = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        overrides = {}

    valid_settings = {"voice", "rate", "audio_device", "wake_duration", "query_duration", "greeting"}
    if setting not in valid_settings:
        return f"Unknown setting: {setting}. Valid: {', '.join(valid_settings)}"

    # For audio_device, resolve name to device ID
    if setting == "audio_device":
        from voice import list_audio_devices
        devices = list_audio_devices()
        # Try matching by name (case-insensitive partial match)
        matched = None
        for d in devices:
            if value.lower() in d["name"].lower() or d["name"].lower() in value.lower():
                matched = d
                break
        if matched:
            value = matched["id"]
            device_name = matched["name"]
        else:
            available = ", ".join(d["name"] for d in devices if d["id"])
            return f"Audio device '{value}' not found. Available: {available}"

    # Type conversion
    if setting in ("rate", "wake_duration", "query_duration"):
        try:
            value = int(value)
        except ValueError:
            return f"Setting '{setting}' must be a number"

    overrides[setting] = value

    with open(overrides_path, "w") as f:
        json.dump(overrides, f, indent=2)

    # Signal web.py to apply settings live via callback if registered
    if _settings_callback:
        _settings_callback(setting, value)

    if setting == "audio_device":
        return f"Audio output switched to '{device_name}'."
    return f"Setting '{setting}' updated to '{value}'."


# Callback for live settings updates (set by web.py)
_settings_callback = None

def register_settings_callback(cb):
    global _settings_callback
    _settings_callback = cb


# -- Dispatch --

HANDLERS = {
    "get_time": handle_get_time,
    "set_reminder": handle_set_reminder,
    "add_calendar_event": handle_add_calendar_event,
    "update_soul": handle_update_soul,
    "run_command": handle_run_command,
    "open_app": handle_open_app,
    "schedule_task": handle_schedule_task,
    "list_scheduled_tasks": handle_list_scheduled_tasks,
    "remove_scheduled_task": handle_remove_scheduled_task,
    "run_code": handle_run_code,
    "create_skill": handle_create_skill,
    "run_skill": handle_run_skill,
    "list_skills": handle_list_skills,
    "edit_soul": handle_edit_soul,
    "read_file": handle_read_file,
    "search_spotify": handle_search_spotify,
    "search_instagram": handle_search_instagram,
    "browse": handle_browse,
    "edit_settings": handle_edit_settings,
    "save_memory": handle_save_memory,
    "recall_memory": handle_recall_memory,
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with given arguments."""
    handler = HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return handler(**arguments)
    except TypeError as e:
        return f"Tool error ({name}): {e}"
    except Exception as e:
        return f"Tool error ({name}): {e}"
