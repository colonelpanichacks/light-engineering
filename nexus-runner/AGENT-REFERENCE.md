# Agent Reference — Nexus Runner Voice Pipeline

Core architecture for Nexus Runner, a standalone local voice agent running natively on Mac. No VM, no cloud.

## Core Architecture

```
Nexus Runner — Local Voice Agent Pipeline:

1. WAKE WORD DETECTION
   - Continuous 3-second recording chunks from Mac mic (ffmpeg + avfoundation)
   - Whisper (local, offline) transcription
   - Wake word filter: configurable per avatar (default: "nexus")
   - Hallucination filter: ignores "you", "thank you.", "thanks for watching."

2. QUERY CAPTURE
   - Inline query: "nexus what time is it" -> extracts "what time is it"
   - Bare wake word: "nexus" -> speaks prompt -> records 5s for query

3. CONVERSATION MODE (stateful follow-ups)
   - No wake word needed after initial activation
   - Records 5s follow-up chunks
   - Tracks consecutive silent recordings
   - Exits after 2 silent recordings or 30s timeout
   - Reset silence counter on any speech detected

4. LLM PROCESSING
   - Send query to Ollama (local)
   - Model configurable via env var (default: qwen3.5)
   - System prompt loaded from soul.md for personality

5. TTS OUTPUT
   - macOS `say` command (voice configurable per avatar)
   - HTTP server on port 3938 accepts POST /say with text body
   - 50KB body limit
```

## Avatars

Nexus Runner supports choosable avatars that change the wake word, voice, and personality.

| Avatar | Wake Word | Voice | Personality |
|--------|-----------|-------|-------------|
| nexus (default) | "nexus" | Daniel | Minimal, direct |
| clippy | "clippy" | Daniel | Classic Clippy energy |
| (add more) | ... | ... | ... |

Each avatar defines:
- **wake_word** — what triggers activation
- **voice** — macOS TTS voice name
- **prompt** — greeting when bare wake word is spoken (e.g. "Yes?")
- **soul** — path to personality/system prompt file

## Key Patterns

### Wake Word Detection Loop
```
while true:
  record 3s chunk from mic
  transcribe with Whisper
  filter hallucinations
  check for active avatar's wake word
  if found: extract query, enter conversation mode
```

### Conversation State Machine
```
States: IDLE -> LISTENING -> CONVERSATION -> IDLE

IDLE: Recording 3s chunks, scanning for wake word
LISTENING: Wake word heard, capturing initial query
CONVERSATION: Follow-up loop without wake word
  - Wait for agent to signal "ready"
  - Record follow-up
  - If silence: increment counter, exit at max
  - If speech: send to LLM, reset counter
```

### IPC Pattern (signal files)
```
/tmp/nexus-listening  — agent signals ready for follow-up
/tmp/nexus-input      — listener writes follow-up text for agent to read
```

## Environment Variables

| Variable | Default | What It Does |
|----------|---------|-------------|
| NEXUS_WAKE_DURATION | 3 | Seconds per wake word recording chunk |
| NEXUS_QUERY_DURATION | 5 | Seconds for initial query recording |
| NEXUS_FOLLOWUP_DURATION | 5 | Seconds for follow-up recordings |
| NEXUS_AVATAR | nexus | Active avatar (nexus, clippy, etc.) |
| OLLAMA_HOST | http://localhost:11434 | Ollama endpoint (local) |
| OLLAMA_MODEL | qwen3.5 | LLM model to use |
| PORT | 3938 | TTS HTTP server port |
| SAY_VOICE | Daniel | macOS voice (overridden by avatar) |
| SAY_RATE | 175 | Speech rate (wpm) |

## Prerequisites

```bash
brew install ffmpeg openai-whisper
# Ollama running locally with desired model pulled
```

## Lineage

Nexus Runner evolved from the Clippy voice agent that ran inside a Clawbox VM. The VM layer has been completely removed — everything runs natively on the Mac now. The original Clippy scripts are preserved as `host-listener-original.sh` and `host-say-server-original.sh` for reference.
