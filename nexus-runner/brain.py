"""Nexus Runner -- LLM brain (Ollama client + conversation + tool orchestration)."""

import os
import re

import httpx

from tools import TOOLS, execute_tool

MAX_HISTORY = 16
MAX_TOOL_LOOPS = 3
OLLAMA_TIMEOUT = 120
MEMORY_BUDGET = 1500  # max chars of memory.md to inject into system prompt
SUMMARY_TRIGGER = 12  # summarize when history exceeds this before capping


class Brain:
    def __init__(self, model: str, host: str, avatar: dict):
        self.model = model
        self.host = host.rstrip("/")
        self.avatar = avatar
        self.history: list[dict] = []
        self._context_summary = ""  # rolling summary of older conversation
        self.soul = self._load_soul()
        self.client = httpx.Client(timeout=OLLAMA_TIMEOUT)
        self.soul_only = False  # when True, only soul is used as system prompt
        self.llm_options = {
            "num_predict": 300,
            "temperature": 0.7,
            "top_k": 40,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
        }

    def _load_soul(self) -> str:
        soul_path = self.avatar.get("soul_file", "")
        if soul_path and os.path.isfile(soul_path):
            with open(soul_path) as f:
                return f.read()
        return ""

    def _load_reminders(self) -> str:
        reminders_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "reminders.md"
        )
        if os.path.isfile(reminders_path):
            with open(reminders_path) as f:
                text = f.read()
            # Only include if there are active reminders
            if "(none yet)" not in text:
                return text
        return ""

    def _load_memory(self) -> str:
        memory_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "memory.md"
        )
        if os.path.isfile(memory_path):
            with open(memory_path) as f:
                text = f.read()
            # Only include if there's actual content beyond headers
            lines = [l for l in text.split("\n") if l.startswith("- ")]
            if not lines:
                return ""
            # Budget: if memory is too large, keep section headers + most recent entries
            if len(text) > MEMORY_BUDGET:
                sections = re.split(r'(^## .+$)', text, flags=re.MULTILINE)
                trimmed = []
                for part in sections:
                    if part.startswith("## "):
                        trimmed.append(part)
                    else:
                        # Keep last N entries per section to fit budget
                        entry_lines = [l for l in part.strip().split("\n") if l.startswith("- ")]
                        # Take last 5 per section max
                        trimmed.append("\n".join(entry_lines[-5:]))
                text = "\n".join(trimmed)
                if len(text) > MEMORY_BUDGET:
                    text = text[-MEMORY_BUDGET:]
            return text
        return ""

    def _build_system_prompt(self) -> str:
        parts = []

        # Identity
        parts.append(self.soul)

        # Soul-only mode: just the soul file, nothing else
        if self.soul_only:
            return "\n".join(parts)

        # Active avatar identity -- tell the LLM who it is right now
        avatar_name = self.avatar.get("name", "nexus")
        parts.append(
            f"\n--- ACTIVE AVATAR ---\n"
            f"You are currently '{avatar_name}'. Stay in character as {avatar_name}. "
            f"Your wake word is '{self.avatar.get('wake_word', avatar_name)}'. "
            f"Your voice is '{self.avatar.get('voice', 'Daniel')}'."
        )

        # Rolling conversation summary (from older messages)
        if self._context_summary:
            parts.append(
                "\n--- CONVERSATION CONTEXT ---\n"
                "Summary of earlier conversation:\n" + self._context_summary
            )

        # Long-term memory
        memory = self._load_memory()
        if memory:
            parts.append("\n--- LONG-TERM MEMORY ---\n" + memory)

        # Active reminders
        reminders = self._load_reminders()
        if reminders:
            parts.append("\n--- ACTIVE REMINDERS ---\n" + reminders)

        # Tool usage guidance
        parts.append(
            "\n--- INSTRUCTIONS ---\n"
            "You are a local AI assistant. Keep responses concise and conversational.\n"
            "When the user asks for code, ALWAYS include the actual code inside a fenced code block "
            "with the language tag, like ```python\\ncode here\\n```. Never return empty code fences.\n"
            "Use tools proactively when appropriate:\n"
            "- get_time: current date/time\n"
            "- set_reminder: save a reminder with a trigger\n"
            "- add_calendar_event: create Apple Calendar events\n"
            "- update_soul: remember user preferences and info\n"
            "- edit_soul: rewrite the entire soul file\n"
            "- edit_settings: change voice, rate, greetings, durations\n"
            "- run_command: run allowlisted shell commands\n"
            "- open_app: launch Mac apps by name\n"
            "- search_spotify: search Spotify for songs, artists, albums, or playlists (opens Spotify app)\n"
            "- search_instagram: look up Instagram profiles, hashtags, or search content (opens browser)\n"
            "- browse: control Brave browser -- navigate, click, fill forms, scroll, read page text, take screenshots, run JS. Use action='open' with url='https://...' to launch and navigate in one step. If Brave is already running it will be restarted with CDP automatically.\n"
            "- run_code: execute Python or bash code blocks\n"
            "- create_skill: save a reusable script as a named skill (set platform='common' for OS-agnostic, otherwise defaults to current OS)\n"
            "- run_skill: execute a saved skill (searches current OS skills first, then common)\n"
            "- list_skills: show all saved skills available on this platform\n"
            "- schedule_task: schedule a command at a time (one-time or repeating)\n"
            "- list_scheduled_tasks: show scheduled tasks\n"
            "- remove_scheduled_task: delete a scheduled task\n"
            "- read_file: read a file's contents when given a file path\n"
            "- save_memory: store important facts/preferences/knowledge to long-term memory (persists across sessions)\n"
            "- recall_memory: search long-term memory for relevant info\n"
            "IMPORTANT: When the user tells you something important about themselves, their preferences,\n"
            "or useful information, proactively use save_memory to remember it across sessions.\n"
            "Never fabricate tool results. If a tool fails, say so honestly."
        )

        return "\n".join(parts)

    def _summarize_and_trim(self):
        """Summarize older messages into a rolling context summary, keep recent ones."""
        # Take the oldest messages that we're about to drop
        keep = MAX_HISTORY - 4  # keep this many recent messages
        to_summarize = self.history[:-keep]
        if not to_summarize:
            return

        # Build a compact representation of old messages
        convo_text = "\n".join(
            f"{m['role']}: {m['content'][:200]}"
            for m in to_summarize
            if m.get("role") in ("user", "assistant") and m.get("content")
        )
        if not convo_text:
            self.history = self.history[-keep:]
            return

        # Ask the LLM to summarize (quick, low-token call)
        prev = f"Previous summary: {self._context_summary}\n\n" if self._context_summary else ""
        summary_prompt = (
            f"{prev}"
            f"Summarize this conversation into 2-3 concise sentences. "
            f"Focus on: what the user asked about, key decisions made, "
            f"and any preferences or facts learned about the user.\n\n"
            f"{convo_text}"
        )
        try:
            resp = self.client.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": summary_prompt}],
                    "stream": False,
                    "options": {"num_predict": 100, "temperature": 0.3},
                },
            )
            result = resp.json().get("message", {}).get("content", "")
            # Strip think tags
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
            if result:
                self._context_summary = result
                print(f"[brain] Summarized {len(to_summarize)} messages: {result[:80]}...")
        except Exception as e:
            print(f"[brain] Summary failed (non-fatal): {e}")

        # Keep only recent messages
        self.history = self.history[-keep:]

    def reload_soul(self):
        """Re-read soul.md (called after update_soul tool modifies it)."""
        self.soul = self._load_soul()

    def warmup(self):
        """Pre-load the model with system prompt + tools so first query is fast.
        Sends a minimal prompt to force Ollama to cache the model context."""
        system_prompt = self._build_system_prompt()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "ready"},
        ]
        try:
            # Quick call with minimal output just to warm the KV cache
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {**self.llm_options, "num_predict": 1},
                "keep_alive": "10m",
            }
            if TOOLS:
                payload["tools"] = TOOLS
            self.client.post(f"{self.host}/api/chat", json=payload)
            print(f"[brain] Warmed up {self.model} with system prompt + {len(TOOLS)} tools")
        except Exception as e:
            print(f"[brain] Warmup failed (non-fatal): {e}")

    def think(self, query: str) -> str:
        """Process a user query and return the response text."""
        # Only append if not already there (web.py may pre-append for instant session save)
        if not self.history or self.history[-1].get("content") != query or self.history[-1].get("role") != "user":
            self.history.append({"role": "user", "content": query})

        # Rolling summary: summarize old messages before dropping them
        if len(self.history) > SUMMARY_TRIGGER:
            self._summarize_and_trim()

        # Cap history (safety net)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

        # Build messages with system prompt
        system_prompt = self._build_system_prompt()
        messages = [{"role": "system", "content": system_prompt}] + self.history

        # Tool call loop
        use_tools = None if self.soul_only else TOOLS
        soul_updated = False
        for _ in range(MAX_TOOL_LOOPS):
            response = self._call_ollama(messages, tools=use_tools)

            text = response.get("content", "") or ""
            tool_calls = response.get("tool_calls", [])

            if not tool_calls:
                break

            # Execute tools
            messages.append(response)  # assistant message with tool_calls
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})

                result = execute_tool(name, args)

                if name == "update_soul":
                    soul_updated = True

                messages.append({
                    "role": "tool",
                    "content": result,
                })

        if soul_updated:
            self.reload_soul()

        # Strip <think> tags (qwen3.5 internal reasoning) but preserve code blocks
        stripped = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        # If stripping think tags leaves nothing, the actual response was inside the tags
        # Extract it -- qwen3.5 sometimes wraps the whole response in <think>
        if not stripped and '<think>' in text:
            stripped = re.sub(r'</?think>', '', text).strip()
        text = stripped

        self.history.append({"role": "assistant", "content": text})
        return text

    def _call_ollama(self, messages: list[dict], tools: list | None = None) -> dict:
        """Make a synchronous call to Ollama chat API."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": dict(self.llm_options),
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = self.client.post(f"{self.host}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {})
        except httpx.TimeoutException:
            return {"content": "I took too long thinking about that. Try again?"}
        except Exception as e:
            return {"content": f"I had trouble connecting to my brain. Error: {e}"}

    def _clean_for_voice(self, text: str) -> str:
        """Strip thinking tags and artifacts for spoken output."""
        # Remove <think>...</think> blocks (qwen3.5 extended thinking)
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        # Remove any remaining XML-style tags
        text = re.sub(r'<[^>]+>', '', text)
        # Remove markdown artifacts
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # bold
        text = re.sub(r'\*([^*]+)\*', r'\1', text)  # italic
        text = re.sub(r'`[^`]+`', '', text)  # inline code
        text = re.sub(r'```[\s\S]*?```', '', text)  # code blocks
        text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)  # headers
        text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)  # bullets
        # Collapse whitespace
        text = re.sub(r'\n{2,}', '. ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def verify_connection(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            resp = self.client.get(f"{self.host}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False
