#!/usr/bin/env python3
"""Nexus Runner -- Web dashboard (FastAPI + WebSocket)."""

import asyncio
import json
import os
import re
import subprocess
import time
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from brain import Brain
from avatars import AVATARS, load_avatar, get_boot_greeting
from voice import list_audio_devices
from tools import TOOLS, SKILLS_DIR, SKILLS_OS_DIR, SKILLS_COMMON_DIR, execute_tool
from tools import _find_skill_file, _find_skill_meta, register_settings_callback


def clean_for_voice(text: str) -> str:
    """Strip markdown/code/tags/emojis for spoken output."""
    from voice import clean_for_voice as _clean
    return _clean(text)


def speak(text: str, voice: str = "Zarvox", rate: int = 175,
          audio_device: str = ""):
    """Speak text using macOS say. Non-blocking via subprocess."""
    if not text:
        return
    text = clean_for_voice(text)[:5000]
    cmd = ["say", "-v", voice, "-r", str(rate)]
    if audio_device:
        cmd.extend(["-a", audio_device])
    cmd.append(text)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

app = FastAPI(title="Nexus Runner")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Mutable state dict (avoids global keyword issues)
state = {
    "avatar_name": os.environ.get("NEXUS_AVATAR", "clippy"),
    "avatar": None,
    "brain": None,
    "current_session_id": None,
    "audio_device": os.environ.get("NEXUS_AUDIO_DEVICE", ""),  # say -a device ID
    "alert_sound": os.environ.get("NEXUS_ALERT_SOUND", "Glass"),  # macOS system sound
    "generating": False,  # True while LLM is thinking
    "generation_task": None,  # asyncio.Task for current LLM call
    "last_user_msg_time": 0.0,  # for queue vs interrupt detection
    "soul_only": False,  # Only Soul at Boot mode
}
_INTERRUPT_WINDOW = 2.0  # seconds -- second Enter within this = interrupt
state["avatar"] = load_avatar(state["avatar_name"])
state["brain"] = Brain(model=OLLAMA_MODEL, host=OLLAMA_HOST, avatar=state["avatar"])


def _on_setting_changed(setting, value):
    """Callback from tools.py edit_settings to apply changes live."""
    avatar = state["avatar"]
    if setting == "voice":
        avatar["voice"] = value
    elif setting == "rate":
        avatar["rate"] = int(value)
    elif setting == "audio_device":
        state["audio_device"] = value
    elif setting == "greeting":
        avatar["greeting"] = value

register_settings_callback(_on_setting_changed)

connected: list[WebSocket] = []
overlay_clients: set = set()  # overlay processes that handle their own TTS
overlay_process: subprocess.Popen | None = None  # managed overlay subprocess


async def broadcast(data: dict):
    msg = json.dumps(data)
    for ws in connected[:]:
        try:
            await ws.send_text(msg)
        except Exception:
            connected.remove(ws)


def read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_file(path: str, content: str):
    with open(path, "w") as f:
        f.write(content)


def get_init_payload() -> dict:
    avatar = state["avatar"]
    brain = state["brain"]
    soul_path = os.path.join(SCRIPT_DIR, "soul.md")
    reminders_path = os.path.join(SCRIPT_DIR, "reminders.md")
    boot_greeting = get_boot_greeting(avatar)

    memory_path = os.path.join(SCRIPT_DIR, "memory.md")
    return {
        "type": "init",
        "avatar": state["avatar_name"],
        "avatars": list(AVATARS.keys()),
        "greeting": boot_greeting,
        "model": OLLAMA_MODEL,
        "soul": read_file(soul_path),
        "reminders": read_file(reminders_path),
        "memory": read_file(memory_path),
        "soul_only": state.get("soul_only", False),
        "history": brain.history[-50:],
        "sessions": _list_sessions(),
        "reminders_json": _load_reminders_json(),
        "tools": [t["function"]["name"] for t in TOOLS],
        "wake_word": avatar.get("wake_word", "nexus"),
        "wake_aliases": avatar.get("wake_aliases", [avatar.get("wake_word", "nexus")]),
        "settings": {
            "ollama_host": OLLAMA_HOST,
            "ollama_model": OLLAMA_MODEL,
            "wake_duration": int(os.environ.get("NEXUS_WAKE_DURATION", "3")),
            "query_duration": int(os.environ.get("NEXUS_QUERY_DURATION", "5")),
            "followup_duration": int(os.environ.get("NEXUS_FOLLOWUP_DURATION", "5")),
            "voice": avatar.get("voice", "Zarvox"),
            "rate": avatar.get("rate", 200),
            "greeting": avatar.get("greeting", ""),
            "temperature": brain.llm_options.get("temperature", 0.7),
            "top_k": brain.llm_options.get("top_k", 40),
            "top_p": brain.llm_options.get("top_p", 0.9),
            "num_predict": brain.llm_options.get("num_predict", 300),
            "repeat_penalty": brain.llm_options.get("repeat_penalty", 1.1),
            "audio_device": state["audio_device"],
            "alert_sound": state["alert_sound"],
        },
        "audio_devices": list_audio_devices(),
        "voices": avatar.get("voices", []),
    }


SESSIONS_DIR = os.path.join(SCRIPT_DIR, "sessions")
REMINDERS_JSON = os.path.join(SCRIPT_DIR, "reminders.json")
os.makedirs(SESSIONS_DIR, exist_ok=True)


# ── Session management ──

def _list_sessions() -> list[dict]:
    """List all saved sessions (id, name, date, message_count)."""
    sessions = []
    for f in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, f)) as fh:
                data = json.load(fh)
            sessions.append({
                "id": data.get("id", f[:-5]),
                "name": data.get("name", "Untitled"),
                "created": data.get("created", 0),
                "message_count": len(data.get("messages", [])),
                "avatar": data.get("avatar", ""),
            })
        except Exception:
            continue
    return sessions


def _save_session(name: str | None = None, sid: str | None = None) -> dict:
    """Save current brain history as a session. If sid is given, update that session.
    Returns session metadata."""
    brain = state["brain"]
    if not brain.history:
        return {"error": "No messages to save"}

    # Update existing session or create new
    if sid:
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', sid)
        path = os.path.join(SESSIONS_DIR, f"{safe_id}.json")
        if os.path.isfile(path):
            with open(path) as f:
                existing = json.load(f)
            if not name:
                name = existing.get("name", "Untitled")
            created = existing.get("created", time.time())
        else:
            sid = str(uuid.uuid4())[:8]
            created = time.time()
    else:
        sid = str(uuid.uuid4())[:8]
        created = time.time()

    # Auto-name from first user message if no name given
    if not name:
        for msg in brain.history:
            if msg["role"] == "user":
                name = msg["content"][:50].strip()
                break
        if not name:
            name = "Untitled"

    session = {
        "id": sid,
        "name": name,
        "created": created,
        "avatar": state["avatar_name"],
        "messages": brain.history[:],
    }
    path = os.path.join(SESSIONS_DIR, f"{sid}.json")
    with open(path, "w") as f:
        json.dump(session, f)

    state["current_session_id"] = sid
    return {"id": sid, "name": name, "created": session["created"],
            "message_count": len(session["messages"]), "avatar": session["avatar"]}


def _load_session(sid: str) -> dict | None:
    """Load a session by ID, restore it to brain history."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', sid)
    path = os.path.join(SESSIONS_DIR, f"{safe_id}.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        data = json.load(f)
    # Restore history
    state["brain"].history = data.get("messages", [])
    state["current_session_id"] = data.get("id")
    return data


def _delete_session(sid: str):
    """Delete a session file."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', sid)
    path = os.path.join(SESSIONS_DIR, f"{safe_id}.json")
    if os.path.isfile(path):
        os.remove(path)


def _rename_session(sid: str, new_name: str):
    """Rename a session."""
    safe_id = re.sub(r'[^a-zA-Z0-9_-]', '', sid)
    path = os.path.join(SESSIONS_DIR, f"{safe_id}.json")
    if not os.path.isfile(path):
        return
    with open(path) as f:
        data = json.load(f)
    data["name"] = new_name[:100]
    with open(path, "w") as f:
        json.dump(data, f)


# ── Structured reminders ──

def _load_reminders_json() -> list[dict]:
    try:
        with open(REMINDERS_JSON) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_reminders_json(reminders: list[dict]):
    with open(REMINDERS_JSON, "w") as f:
        json.dump(reminders, f, indent=2)
    # Also sync to reminders.md for the brain to read
    _sync_reminders_to_md(reminders)


def _sync_reminders_to_md(reminders: list[dict]):
    """Write structured reminders into reminders.md for the brain."""
    lines = ["# Nexus Runner Reminders\n"]
    lines.append("## Active Reminders\n")
    if not reminders:
        lines.append("(none yet)\n")
    else:
        for r in reminders:
            trigger = r.get("trigger_type", "keyword")
            if trigger == "time":
                time_str = r.get("time", "09:00")
                days = r.get("days", [])
                day_str = ", ".join(days) if days else "daily"
                lines.append(f"- trigger: {day_str} at {time_str}")
            elif trigger == "startup":
                lines.append("- trigger: startup")
            else:
                lines.append(f"- trigger: {r.get('keyword', 'always')}")
            lines.append(f"  content: {r.get('content', '')}")
            lines.append(f"  created: {r.get('created', '')}")
            expires = r.get("expires", "")
            lines.append(f"  expires: {expires or 'never'}")
            lines.append("")
    reminders_path = os.path.join(SCRIPT_DIR, "reminders.md")
    write_file(reminders_path, "\n".join(lines))


def _list_skills() -> list[dict]:
    """List all skills with metadata from OS-specific + common dirs."""
    skills = []
    for search_dir in (SKILLS_OS_DIR, SKILLS_COMMON_DIR):
        if not os.path.isdir(search_dir):
            continue
        for f in sorted(os.listdir(search_dir)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(search_dir, f)) as mf:
                        meta = json.load(mf)
                    skills.append(meta)
                except Exception:
                    continue
    return skills


def _get_skill(name: str) -> dict | None:
    """Get a skill's metadata and code."""
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    meta_path = _find_skill_meta(safe_name)
    if not meta_path:
        return None

    with open(meta_path) as f:
        meta = json.load(f)

    script_path = _find_skill_file(safe_name)
    if script_path:
        with open(script_path) as f:
            code = f.read()
        # Strip shebang
        lines = code.split("\n")
        if lines and lines[0].startswith("#!"):
            code = "\n".join(lines[1:])
        meta["code"] = code

    return meta


def _delete_skill(name: str):
    """Delete a skill's files from whichever directory it lives in."""
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    for search_dir in (SKILLS_OS_DIR, SKILLS_COMMON_DIR):
        for ext in (".json", ".py", ".sh"):
            path = os.path.join(search_dir, safe_name + ext)
            if os.path.isfile(path):
                os.remove(path)


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Accept audio from browser mic, transcribe with whisper-cli, return text."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    try:
        raw_data = await audio.read()
        # Browser sends webm/opus -- convert to 16kHz mono WAV via ffmpeg
        webm_path = tmp_path.replace(".wav", ".webm")
        with open(webm_path, "wb") as f:
            f.write(raw_data)
        convert = subprocess.run(
            ["ffmpeg", "-y", "-i", webm_path, "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True, timeout=10,
        )
        if convert.returncode != 0:
            return JSONResponse({"text": "", "error": "audio conversion failed"})

        # Transcribe with whisper-cli
        model_path = os.environ.get(
            "WHISPER_MODEL",
            os.path.expanduser("~/.local/share/whisper-cpp/ggml-base.en.bin"),
        )
        result = subprocess.run(
            ["whisper-cli", "-m", model_path, "-f", tmp_path, "-l", "en", "-nt"],
            capture_output=True, text=True, timeout=15,
        )
        text = result.stdout.strip() if result.stdout else ""

        # Debug: uncomment to see raw whisper output
        # if text: print(f"[whisper] Raw: {text!r}", flush=True)

        # Filter whisper hallucinations
        HALLUCINATIONS = {
            "you", "thank you.", "thanks for watching.", ".", "",
            "thanks.", "bye.", "goodbye.", "thank you for watching.",
            "the end.", "so,", "i'm sorry.", "pause", "blank audio",
            "silence", "coughing", "music", "applause", "laughter",
            "breathing", "sighing", "inaudible", "noise",
        }
        cleaned = text.lower().strip().rstrip('.').strip()
        # Filter exact matches
        if cleaned in HALLUCINATIONS or text.lower() in HALLUCINATIONS:
            text = ""
        # Filter bracketed/parenthesized/italicized whisper artifacts like [Pause], (coughing), *screams*
        elif re.match(r'^[\[\(\*].*[\]\)\*]$', text.strip()):
            text = ""
        # Filter if too short (single word noise)
        elif len(cleaned) < 3:
            text = ""

        # Debug: uncomment to see filtered whisper output
        # if not text:
        #     raw = repr(result.stdout.strip()) if result.stdout else "empty"
        #     print(f"[whisper] Filtered (raw was {raw})", flush=True)
        return JSONResponse({"text": text})
    except Exception as e:
        print(f"[web] Transcribe error: {e}", flush=True)
        return JSONResponse({"text": "", "error": str(e)})
    finally:
        for p in [tmp_path, tmp_path.replace(".wav", ".webm")]:
            try:
                os.remove(p)
            except OSError:
                pass


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected.append(ws)

    await ws.send_text(json.dumps(get_init_payload()))

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type", "")

            if msg_type == "chat":
                query = data.get("content", "").strip()
                if not query:
                    continue

                # If already generating, wait for it to finish first
                if state["generating"]:
                    task = state.get("generation_task")
                    if task and not task.done():
                        await broadcast({"type": "queued"})
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    state["generating"] = False
                    state["generation_task"] = None

                # Append user message to history
                state["brain"].history.append({"role": "user", "content": query})

                # Auto-save session instantly on user message
                session_meta = _save_session(sid=state.get("current_session_id"))
                if "error" not in session_meta:
                    sessions = _list_sessions()
                    await ws.send_text(json.dumps({
                        "type": "session_autosaved",
                        "session": session_meta,
                        "sessions": sessions,
                    }))

                await broadcast({
                    "type": "message",
                    "role": "user",
                    "content": query,
                })

                # Start generation
                voice_on = bool(data.get("voice", False))

                async def _generate(q, vo):
                    try:
                        state["generating"] = True
                        await broadcast({"type": "thinking"})

                        brain = state["brain"]
                        avatar = state["avatar"]
                        loop = asyncio.get_event_loop()
                        response = await loop.run_in_executor(None, brain.think, q)

                        print(f"[web] voice={vo}, response={response[:60]}...", flush=True)
                        await broadcast({
                            "type": "message",
                            "role": "assistant",
                            "name": avatar.get("name", "nexus"),
                            "content": response,
                            "voice": vo,
                            "voice_name": avatar.get("voice", "Zarvox"),
                            "rate": avatar.get("rate", 175),
                        })

                        # Only speak from web.py if overlay is NOT connected
                        # (overlay handles its own TTS so it can track when speech ends)
                        overlay_active = bool(overlay_clients) or (overlay_process and overlay_process.poll() is None)
                        if vo and not overlay_active:
                            speak(
                                response,
                                voice=avatar.get("voice", "Zarvox"),
                                rate=avatar.get("rate", 200),
                                audio_device=state.get("audio_device", ""),
                            )

                        # Auto-save session after each exchange
                        session_meta = _save_session(sid=state.get("current_session_id"))
                        if "error" not in session_meta:
                            sessions = _list_sessions()
                            await broadcast({
                                "type": "session_autosaved",
                                "session": session_meta,
                                "sessions": sessions,
                            })
                    except asyncio.CancelledError:
                        print(f"[web] Generation cancelled", flush=True)
                    finally:
                        state["generating"] = False
                        state["generation_task"] = None

                task = asyncio.create_task(_generate(query, voice_on))
                state["generation_task"] = task

            elif msg_type == "save_settings":
                new_settings = data.get("settings", {})
                avatar = state["avatar"]
                brain = state["brain"]

                # Apply voice/speech settings to avatar
                if "voice" in new_settings:
                    avatar["voice"] = new_settings["voice"]
                if "rate" in new_settings:
                    avatar["rate"] = int(new_settings["rate"])
                if "greeting" in new_settings:
                    avatar["greeting"] = new_settings["greeting"]
                if "audio_device" in new_settings:
                    state["audio_device"] = new_settings["audio_device"]
                if "alert_sound" in new_settings:
                    state["alert_sound"] = new_settings["alert_sound"]

                # Apply LLM options to brain
                for key in ("temperature", "top_k", "top_p", "num_predict", "repeat_penalty"):
                    if key in new_settings:
                        brain.llm_options[key] = new_settings[key]

                # Update status bar
                document_settings = {
                    "voice": avatar.get("voice", "Zarvox"),
                    "rate": avatar.get("rate", 200),
                }
                await broadcast({"type": "settings_saved", "settings": document_settings})
                print(f"[web] Settings saved: voice={avatar.get('voice')}, temp={brain.llm_options.get('temperature')}", flush=True)

            elif msg_type == "save_soul":
                content = data.get("content", "")
                soul_path = os.path.join(SCRIPT_DIR, "soul.md")
                # Backup before overwrite
                if os.path.isfile(soul_path):
                    backup_path = os.path.join(SCRIPT_DIR, "soul.md.bak")
                    with open(soul_path) as sf:
                        with open(backup_path, "w") as bf:
                            bf.write(sf.read())
                write_file(soul_path, content)
                state["brain"].reload_soul()
                await broadcast({"type": "soul_saved"})

            elif msg_type == "save_reminders":
                content = data.get("content", "")
                write_file(os.path.join(SCRIPT_DIR, "reminders.md"), content)
                await broadcast({"type": "reminders_saved"})

            elif msg_type == "test_voice":
                voice = data.get("voice", state["avatar"].get("voice", "Zarvox"))
                rate = state["avatar"].get("rate", 200)
                speak("take me to your leader", voice=voice, rate=rate,
                      audio_device=state.get("audio_device", ""))

            elif msg_type == "switch_avatar":
                name = data.get("avatar", "nexus")
                state["avatar_name"] = name
                state["avatar"] = load_avatar(name)
                state["brain"] = Brain(
                    model=OLLAMA_MODEL, host=OLLAMA_HOST, avatar=state["avatar"]
                )
                await broadcast({
                    "type": "avatar_switched",
                    "avatar": name,
                    "wake_word": state["avatar"].get("wake_word", name),
                    "wake_aliases": state["avatar"].get("wake_aliases", [name]),
                    "settings": {
                        "voice": state["avatar"].get("voice", "Zarvox"),
                        "rate": state["avatar"].get("rate", 175),
                    },
                    "voices": state["avatar"].get("voices", []),
                })

            elif msg_type == "clear_history":
                # Cancel any running generation
                task = state.get("generation_task")
                if task and not task.done():
                    task.cancel()
                state["generating"] = False
                state["generation_task"] = None
                state["brain"].history.clear()
                await broadcast({"type": "history_cleared"})

            elif msg_type == "list_skills":
                skills = _list_skills()
                await ws.send_text(json.dumps({"type": "skills_list", "skills": skills}))

            elif msg_type == "get_skill":
                skill = _get_skill(data.get("name", ""))
                if skill:
                    await ws.send_text(json.dumps({"type": "skill_detail", "skill": skill}))

            elif msg_type == "save_skill":
                result = execute_tool("create_skill", {
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "language": data.get("language", "python"),
                    "code": data.get("code", ""),
                    "platform": data.get("platform", ""),
                })
                await ws.send_text(json.dumps({"type": "skill_saved", "result": result}))

            elif msg_type == "run_skill":
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, execute_tool, "run_skill", {"name": data.get("name", "")}
                )
                await ws.send_text(json.dumps({"type": "skill_output", "output": result}))

            elif msg_type == "delete_skill":
                name = data.get("name", "")
                _delete_skill(name)
                await ws.send_text(json.dumps({"type": "skill_deleted", "name": name}))

            elif msg_type == "add_reminder":
                reminders = _load_reminders_json()
                reminder = {
                    "id": str(uuid.uuid4())[:8],
                    "content": data.get("content", ""),
                    "trigger_type": data.get("trigger_type", "keyword"),
                    "time": data.get("time", ""),
                    "days": data.get("days", []),
                    "keyword": data.get("keyword", ""),
                    "expires": data.get("expires", ""),
                    "created": time.strftime("%Y-%m-%d"),
                }
                reminders.append(reminder)
                _save_reminders_json(reminders)
                state["brain"].reload_soul()

                # Add to Apple Calendar for time-based reminders
                if reminder["trigger_type"] == "time" and reminder.get("time"):
                    try:
                        cal_time = reminder["time"]
                        cal_days = reminder.get("days", [])
                        cal_title = f"Nexus: {reminder['content'][:50]}"
                        # Add calendar event via osascript
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None, execute_tool, "add_calendar_event", {
                                "title": cal_title,
                                "date": time.strftime("%Y-%m-%d"),
                                "time": cal_time,
                                "notes": reminder["content"],
                            }
                        )
                    except Exception as e:
                        print(f"[web] Calendar add failed: {e}", flush=True)

                await ws.send_text(json.dumps({
                    "type": "reminders_updated",
                    "reminders": reminders,
                }))

            elif msg_type == "delete_reminder":
                rid = data.get("id", "")
                reminders = _load_reminders_json()
                reminders = [r for r in reminders if r.get("id") != rid]
                _save_reminders_json(reminders)
                state["brain"].reload_soul()
                await ws.send_text(json.dumps({
                    "type": "reminders_updated",
                    "reminders": reminders,
                }))

            elif msg_type == "list_scheduled_tasks":
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, execute_tool, "list_scheduled_tasks", {}
                )
                # Parse task names from the result string
                tasks = []
                if result and "No scheduled tasks" not in result:
                    for name in result.replace("Scheduled tasks: ", "").split(", "):
                        name = name.strip()
                        if name:
                            # Try to read plist for details
                            plist_path = os.path.join(
                                os.path.expanduser("~/Library/LaunchAgents"),
                                f"com.nexusrunner.task.{name}.plist"
                            )
                            task_info = {"name": name, "command": "", "when": "", "repeat": False}
                            if os.path.isfile(plist_path):
                                try:
                                    with open(plist_path) as pf:
                                        content = pf.read()
                                    # Extract command
                                    import re as _re
                                    cmd_match = _re.search(r'<string>(.+?)</string>\s*</array>', content)
                                    if cmd_match:
                                        task_info["command"] = cmd_match.group(1).split(" && launchctl")[0]
                                    task_info["repeat"] = "launchctl unload" not in content
                                except Exception:
                                    pass
                            tasks.append(task_info)
                await ws.send_text(json.dumps({"type": "scheduled_tasks", "tasks": tasks}))

            elif msg_type == "add_scheduled_task":
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, execute_tool, "schedule_task", {
                        "name": data.get("name", ""),
                        "command": data.get("command", ""),
                        "when": data.get("when", ""),
                        "repeat": data.get("repeat", False),
                    }
                )
                await ws.send_text(json.dumps({"type": "task_added", "result": result}))

            elif msg_type == "remove_scheduled_task":
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, execute_tool, "remove_scheduled_task", {
                        "name": data.get("name", ""),
                    }
                )
                await ws.send_text(json.dumps({"type": "task_removed", "result": result}))

            elif msg_type == "list_sessions":
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))

            elif msg_type == "save_session":
                result = _save_session(data.get("name"))
                await ws.send_text(json.dumps({"type": "session_saved", "session": result}))

            elif msg_type == "load_session":
                session = _load_session(data.get("id", ""))
                if session:
                    await ws.send_text(json.dumps({
                        "type": "session_loaded",
                        "session": {
                            "id": session["id"],
                            "name": session["name"],
                        },
                        "history": session["messages"][-50:],
                    }))

            elif msg_type == "delete_session":
                sid = data.get("id", "")
                _delete_session(sid)
                # If we deleted the active session, clear chat
                if state.get("current_session_id") == sid:
                    state["brain"].history.clear()
                    state["current_session_id"] = None
                    await broadcast({"type": "history_cleared"})
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))

            elif msg_type == "rename_session":
                _rename_session(data.get("id", ""), data.get("name", ""))
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))

            elif msg_type == "new_session":
                # Cancel any running generation
                task = state.get("generation_task")
                if task and not task.done():
                    task.cancel()
                state["generating"] = False
                state["generation_task"] = None
                # Auto-save current if it has messages
                if state["brain"].history:
                    _save_session(sid=state.get("current_session_id"))
                state["brain"].history.clear()
                state["brain"]._context_summary = ""
                state["current_session_id"] = None
                await broadcast({"type": "new_session_started"})
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))

            elif msg_type == "save_memory_raw":
                content = data.get("content", "")
                memory_path = os.path.join(SCRIPT_DIR, "memory.md")
                write_file(memory_path, content)
                state["brain"].reload_soul()
                await broadcast({"type": "memory_saved", "memory": content})

            elif msg_type == "add_memory":
                category = data.get("category", "Facts")
                content = data.get("content", "").strip()
                if content:
                    memory_path = os.path.join(SCRIPT_DIR, "memory.md")
                    mem_text = read_file(memory_path)
                    section_header = f"## {category}"
                    if section_header in mem_text:
                        # Insert after the section header
                        lines = mem_text.split("\n")
                        new_lines = []
                        inserted = False
                        for line in lines:
                            new_lines.append(line)
                            if not inserted and line.strip() == section_header:
                                new_lines.append("")
                                new_lines.append(f"- {content}")
                                inserted = True
                        mem_text = "\n".join(new_lines)
                    else:
                        mem_text += f"\n{section_header}\n\n- {content}\n"
                    write_file(memory_path, mem_text)
                    await broadcast({"type": "memory_saved", "memory": mem_text})

            elif msg_type == "delete_memory":
                line_text = data.get("text", "").strip()
                if line_text:
                    memory_path = os.path.join(SCRIPT_DIR, "memory.md")
                    mem_text = read_file(memory_path)
                    lines = mem_text.split("\n")
                    lines = [l for l in lines if l.strip() != f"- {line_text}"]
                    mem_text = "\n".join(lines)
                    write_file(memory_path, mem_text)
                    await broadcast({"type": "memory_saved", "memory": mem_text})

            elif msg_type == "list_memories":
                memory_path = os.path.join(SCRIPT_DIR, "memory.md")
                await ws.send_text(json.dumps({
                    "type": "memories_list",
                    "memory": read_file(memory_path),
                }))

            elif msg_type == "ingest_memory":
                # Save memory if provided, then reboot
                content = data.get("content", "")
                if content:
                    memory_path = os.path.join(SCRIPT_DIR, "memory.md")
                    write_file(memory_path, content)

                # Kill current generation
                task = state.get("generation_task")
                if task and not task.done():
                    task.cancel()
                state["generating"] = False
                state["generation_task"] = None

                # Rebuild brain
                state["brain"] = Brain(
                    model=OLLAMA_MODEL, host=OLLAMA_HOST, avatar=state["avatar"]
                )
                state["brain"].soul_only = state.get("soul_only", False)
                state["brain"].history.clear()
                state["current_session_id"] = None

                import threading
                threading.Thread(target=state["brain"].warmup, daemon=True).start()

                await broadcast({"type": "llm_rebooted"})
                await broadcast({"type": "new_session_started"})
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))
                print("[web] Memories ingested, LLM rebooted", flush=True)

            elif msg_type == "set_soul_only":
                state["soul_only"] = bool(data.get("enabled", False))
                state["brain"].soul_only = state["soul_only"]
                await broadcast({"type": "soul_only_set", "enabled": state["soul_only"]})
                print(f"[web] Soul-only mode: {state['soul_only']}", flush=True)

            elif msg_type == "ingest_soul":
                # Save soul, then reboot the LLM with it
                content = data.get("content", "")
                soul_path = os.path.join(SCRIPT_DIR, "soul.md")
                if content:
                    if os.path.isfile(soul_path):
                        backup_path = os.path.join(SCRIPT_DIR, "soul.md.bak")
                        with open(soul_path) as sf:
                            with open(backup_path, "w") as bf:
                                bf.write(sf.read())
                    write_file(soul_path, content)

                # Kill current generation
                task = state.get("generation_task")
                if task and not task.done():
                    task.cancel()
                state["generating"] = False
                state["generation_task"] = None

                # Rebuild brain from scratch
                state["brain"] = Brain(
                    model=OLLAMA_MODEL, host=OLLAMA_HOST, avatar=state["avatar"]
                )
                state["brain"].soul_only = state.get("soul_only", False)
                state["current_session_id"] = None

                # Warmup in background
                import threading
                threading.Thread(target=state["brain"].warmup, daemon=True).start()

                await broadcast({"type": "soul_ingested"})
                await broadcast({"type": "new_session_started"})
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))
                print("[web] Soul ingested, LLM rebooted", flush=True)

            elif msg_type == "reboot_llm":
                # Kill current generation
                task = state.get("generation_task")
                if task and not task.done():
                    task.cancel()
                state["generating"] = False
                state["generation_task"] = None

                # Rebuild brain
                state["brain"] = Brain(
                    model=OLLAMA_MODEL, host=OLLAMA_HOST, avatar=state["avatar"]
                )
                state["brain"].soul_only = state.get("soul_only", False)
                state["brain"].history.clear()
                state["current_session_id"] = None

                # Warmup in background
                import threading
                threading.Thread(target=state["brain"].warmup, daemon=True).start()

                await broadcast({"type": "llm_rebooted"})
                await broadcast({"type": "new_session_started"})
                sessions = _list_sessions()
                await ws.send_text(json.dumps({"type": "sessions_list", "sessions": sessions}))
                print("[web] LLM killed and rebooted", flush=True)

            elif msg_type == "start_overlay":
                global overlay_process
                # Check if any overlay is already running (lock file test)
                import fcntl as _fcntl
                _already_running = False
                try:
                    _lf = open("/tmp/nexus-overlay.lock", "r+")
                    _fcntl.flock(_lf, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    _fcntl.flock(_lf, _fcntl.LOCK_UN)
                    _lf.close()
                except BlockingIOError:
                    _already_running = True  # lock held = overlay running
                except FileNotFoundError:
                    pass  # no lock file = no overlay running
                except Exception:
                    pass

                if _already_running:
                    print("[web] Overlay already running (lock held), skipping", flush=True)
                elif overlay_process is None or overlay_process.poll() is not None:
                    overlay_script = os.path.join(SCRIPT_DIR, "overlay.py")
                    if os.path.isfile(overlay_script):
                        overlay_process = subprocess.Popen(
                            ["python3", overlay_script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        )
                        print("[web] Overlay process started", flush=True)

            elif msg_type == "stop_overlay":
                if overlay_process and overlay_process.poll() is None:
                    overlay_process.terminate()
                    overlay_process = None
                    print("[web] Overlay process stopped", flush=True)

            elif msg_type == "overlay_connect":
                overlay_clients.add(ws)
                print("[web] Overlay client connected", flush=True)

            elif msg_type == "refresh":
                await ws.send_text(json.dumps(get_init_payload()))

    except WebSocketDisconnect:
        if ws in connected:
            connected.remove(ws)
        overlay_clients.discard(ws)
    except Exception:
        if ws in connected:
            connected.remove(ws)
        overlay_clients.discard(ws)


def play_alert_sound(sound_name: str = "Glass"):
    """Play a macOS system alert sound."""
    sound_path = f"/System/Library/Sounds/{sound_name}.aiff"
    if os.path.isfile(sound_path):
        subprocess.Popen(
            ["afplay", sound_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


async def check_reminders():
    """Background task: check time-based reminders every 30 seconds."""
    import datetime
    fired_today = set()  # track (id, hour:minute) to avoid re-firing
    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.datetime.now()
            day_name = now.strftime("%a").lower()[:3]  # mon, tue, etc
            current_time = now.strftime("%H:%M")
            reminders = _load_reminders_json()

            for r in reminders:
                if r.get("trigger_type") != "time":
                    continue
                fire_key = f"{r.get('id','')}-{current_time}"
                if fire_key in fired_today:
                    continue
                reminder_time = r.get("time", "")
                if reminder_time != current_time:
                    continue
                # Check day filter
                days = r.get("days", [])
                if days and day_name not in days:
                    continue
                # Check expiry
                expires = r.get("expires", "")
                if expires and now.strftime("%Y-%m-%d") > expires:
                    continue

                # Fire the reminder
                fired_today.add(fire_key)
                content = r.get("content", "Reminder!")
                alert_sound = state.get("alert_sound", "Glass")
                play_alert_sound(alert_sound)

                # Broadcast to overlay + dashboard
                await broadcast({
                    "type": "message",
                    "role": "assistant",
                    "content": f"Reminder: {content}",
                    "voice": True,
                    "voice_name": state["avatar"].get("voice", "Zarvox"),
                    "rate": state["avatar"].get("rate", 200),
                })
                speak(
                    f"Reminder. {content}",
                    voice=state["avatar"].get("voice", "Zarvox"),
                    rate=state["avatar"].get("rate", 200),
                    audio_device=state.get("audio_device", ""),
                )
                print(f"[web] Fired reminder: {content}", flush=True)

            # Reset fired set at midnight
            if current_time == "00:00":
                fired_today.clear()
        except Exception as e:
            print(f"[web] Reminder check error: {e}", flush=True)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(check_reminders())


if __name__ == "__main__":
    import uvicorn
    # Warm up the LLM with system prompt, memories, and tools
    import threading
    threading.Thread(target=state["brain"].warmup, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=5555)
