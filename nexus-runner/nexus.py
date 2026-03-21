#!/usr/bin/env python3
"""Nexus Runner -- Lightweight local voice agent for macOS.

Voice queries are sent to web.py via WebSocket so they appear in the chat
dashboard, trigger the overlay, and save to sessions -- same as typed messages.
"""

import json
import os
import re
import sys
import threading

import websockets
import asyncio

from avatars import load_avatar
from voice import find_mic, listen, speak

# -- Config from environment --
AVATAR_NAME = os.environ.get("NEXUS_AVATAR", "clippy")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5")
WAKE_DURATION = int(os.environ.get("NEXUS_WAKE_DURATION", "2"))
QUERY_DURATION = int(os.environ.get("NEXUS_QUERY_DURATION", "5"))
FOLLOWUP_DURATION = int(os.environ.get("NEXUS_FOLLOWUP_DURATION", "4"))
MAX_SILENCE = 2
WS_URL = os.environ.get("NEXUS_WS_URL", "ws://localhost:5555/ws")


def extract_query(text: str, avatar: dict) -> str | None:
    """Check for wake word or aliases in text. Returns query (may be empty) or None.
    Uses exact substring match against all aliases for fuzzy wake word detection."""
    lower = text.lower()

    # Check all aliases (includes the primary wake word)
    aliases = avatar.get("wake_aliases", [avatar["wake_word"]])
    matched = None
    for alias in aliases:
        if alias.lower() in lower:
            matched = alias
            break

    if matched is None:
        return None

    # Strip the matched alias and surrounding punctuation/whitespace
    query = re.sub(
        rf'{re.escape(matched)}[,.\!\? ]*',
        '', lower, count=1, flags=re.IGNORECASE,
    ).strip()
    return query


def log(msg: str):
    print(f"  [{msg}]", flush=True)


class VoiceClient:
    """Connects to web.py WebSocket and sends voice queries through the dashboard."""

    def __init__(self, avatar: dict):
        self.avatar = avatar
        self.ws = None
        self.loop = None
        self.connected = threading.Event()
        self.response_event = threading.Event()
        self.last_response = None
        self.voice = avatar.get("voice", "Zarvox")
        self.rate = avatar.get("rate", 200)

    def start(self):
        """Start WebSocket connection in background thread."""
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        # Wait for connection
        self.connected.wait(timeout=5)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    async def _connect(self):
        try:
            async with websockets.connect(WS_URL) as ws:
                self.ws = ws
                self.connected.set()
                log("Connected to web dashboard")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")

                    # Pick up init settings
                    if msg_type == "init":
                        settings = msg.get("settings", {})
                        self.voice = settings.get("voice", self.voice)
                        self.rate = settings.get("rate", self.rate)

                    # Assistant response -- this is what we speak
                    elif msg_type == "message" and msg.get("role") == "assistant":
                        self.last_response = msg.get("content", "")
                        self.response_event.set()

                    # Settings update
                    elif msg_type == "avatar_switched":
                        settings = msg.get("settings", {})
                        self.voice = settings.get("voice", self.voice)
                        self.rate = settings.get("rate", self.rate)

        except Exception as e:
            log(f"WebSocket error: {e}")
            self.connected.set()  # unblock main thread

    def send_query(self, query: str) -> str | None:
        """Send a voice query to web.py and wait for the response."""
        if not self.ws or not self.connected.is_set():
            return None

        self.response_event.clear()
        self.last_response = None

        # Send via the event loop -- voice: false so web.py doesn't TTS
        # (nexus.py handles TTS itself with blocking speak() to avoid overlap)
        future = asyncio.run_coroutine_threadsafe(
            self.ws.send(json.dumps({
                "type": "chat",
                "content": query,
                "voice": False,
            })),
            self.loop,
        )
        future.result(timeout=5)

        # Wait for response (Ollama can be slow)
        self.response_event.wait(timeout=120)
        return self.last_response


def main():
    avatar = load_avatar(AVATAR_NAME)
    wake_word = avatar["wake_word"]

    print(f"\n  Nexus Runner")
    print(f"  Avatar: {avatar['name']} | Model: {OLLAMA_MODEL}")
    print(f"  Say \"{wake_word}\" to begin.\n")

    # Find mic
    mic = find_mic()
    log(f"Mic device: :{mic}")

    # Connect to web dashboard
    client = VoiceClient(avatar)
    client.start()

    if not client.connected.is_set():
        log("Could not connect to web dashboard. Make sure web.py is running.")
        sys.exit(1)

    log("Listening...")

    # -- Main loop --
    while True:
        try:
            # IDLE: scan for wake word
            text = listen(duration=WAKE_DURATION)
            if not text:
                continue

            query = extract_query(text, avatar)
            if query is None:
                continue

            log(f"Wake word detected: {text}")

            # If bare wake word, speak greeting and record query
            if not query:
                speak(avatar["greeting"], avatar["voice"], avatar["rate"])
                query = listen(duration=QUERY_DURATION)
                if not query:
                    continue

            log(f"Query: {query}")

            # Send to web.py -- this makes it appear in chat, trigger overlay, save session
            response = client.send_query(query)
            if response:
                log(f"Response: {response[:80]}...")
                # Blocking TTS -- prevents overlap with next recording
                speak(response, client.voice, client.rate)
            else:
                log("No response from server")

            # -- Conversation mode --
            silence_count = 0
            while silence_count < MAX_SILENCE:
                followup = listen(duration=FOLLOWUP_DURATION)
                if not followup:
                    silence_count += 1
                    log(f"Silence ({silence_count}/{MAX_SILENCE})")
                    continue

                silence_count = 0
                log(f"Follow-up: {followup}")
                response = client.send_query(followup)
                if response:
                    log(f"Response: {response[:80]}...")
                    speak(response, client.voice, client.rate)

            log("Back to listening...")

        except KeyboardInterrupt:
            print("\n\n  Nexus Runner stopped.\n")
            sys.exit(0)


if __name__ == "__main__":
    main()
