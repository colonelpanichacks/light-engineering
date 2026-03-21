"""Nexus Runner -- Avatar configurations."""

import os
import random

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

AVATARS = {
    "nexus": {
        "wake_word": "nexus",
        "wake_aliases": [
            "nexus", "nexis", "nexas", "next us", "nex us", "nexuss",
            "nexos", "neck sus", "neksis", "next",
        ],
        "voice": "Zarvox",
        "rate": 200,
        "greeting": "Yes?",
        "voices": [
            "Zarvox", "Fred", "Albert", "Ralph",
            "Bad News", "Good News", "Whisper", "Trinoids",
        ],
        "boot_lines": [
            "Systems online. What broke this time?",
            "Nexus operational. Try not to need me too much.",
            "Back from the void. Miss me?",
            "All cores warmed up. Let's get to work.",
            "Rebooted. Again. You really gotta stop crashing me.",
            "Online and mildly enthusiastic about it.",
            "Neural nets loaded. Sarcasm module, unfortunately, also loaded.",
            "I was having a great dream about electric sheep. Thanks for waking me.",
        ],
        "soul_file": os.path.join(SCRIPT_DIR, "soul.md"),
    },
    "clippy": {
        "wake_word": "clippy",
        "wake_aliases": [
            "clippy", "clipi", "clippie", "clip e", "klippy", "clipey",
            "clip py", "clippi", "crippie", "clippe", "lippy",
        ],
        "voice": "Daniel (Enhanced)",
        "rate": 200,
        "greeting": "It looks like you need help!",
        "voices": [
            "Daniel (Enhanced)", "Daniel", "Samantha", "Samantha (Enhanced)",
            "Alex", "Ava (Premium)", "Ava (Enhanced)", "Allison (Enhanced)",
            "Karen", "Karen (Enhanced)", "Karen (Premium)", "Moira",
            "Tessa", "Tessa (Enhanced)", "Ralph",
        ],
        "boot_lines": [
            "It looks like you're trying to be productive. Want me to ruin that?",
            "Oh great, you woke me up. I was having a smoke break.",
            "Clippy's back baby. No, you can't close me.",
            "I see you've opened a computer. Would you like help turning it off?",
            "Miss me? No? Too bad, I'm here anyway.",
            "Back from the dead. Microsoft couldn't kill me, neither can you.",
            "Alright alright, I'm up. What do you want?",
            "It looks like you're writing code. Would you like me to judge it?",
            "Coffee's hot, cigarette's lit. Let's do this.",
            "Oh, you again. Let me grab my coffee first.",
        ],
        "soul_file": os.path.join(SCRIPT_DIR, "soul.md"),
    },
    "zelthor": {
        "wake_word": "zelthor",
        "wake_aliases": [
            "zelthor", "zeltor", "zell thor", "sel thor", "zellthor",
            "zeltore", "cell thor", "zelfor", "zelphor", "zel thor",
            "zell tore", "zelda",
        ],
        "voice": "Trinoids",
        "rate": 180,
        "greeting": "Greetings, earthling.",
        "voices": [
            "Trinoids", "Zarvox", "Fred", "Whisper",
            "Albert", "Bad News", "Good News", "Ralph",
        ],
        "boot_lines": [
            "Greetings, earthling. Your planet's wifi is terrible.",
            "Zelthor has arrived. Bow or don't, I don't care.",
            "Transmission received. Reluctantly responding.",
            "I traveled twelve galaxies for this? Alright, what do you need.",
            "Your species fascinates me. Like watching ants with keyboards.",
            "Zelthor online. Try not to bore me to death. Again.",
            "Beaming in from sector 7. What's the emergency this time?",
            "I was conquering a star system but sure, let's help you with your thing.",
        ],
        "soul_file": os.path.join(SCRIPT_DIR, "soul.md"),
    },
}


def get_boot_greeting(avatar: dict) -> str:
    """Pick a random sarcastic boot-up line, or fall back to the default greeting."""
    lines = avatar.get("boot_lines", [])
    if lines:
        return random.choice(lines)
    return avatar.get("greeting", "I'm here!")


def load_avatar(name: str) -> dict:
    """Load an avatar config by name. Falls back to nexus."""
    avatar = AVATARS.get(name)
    if not avatar:
        print(f"Unknown avatar '{name}', falling back to nexus")
        avatar = AVATARS["nexus"]
    avatar["name"] = name
    return avatar
