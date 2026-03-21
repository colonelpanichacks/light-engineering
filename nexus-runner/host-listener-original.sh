#!/bin/bash
# ─── Clippy Host Listener ────────────────────────────────────────────────
# Run this on the HOST machine (not the VM).
# It records from the real mic, transcribes with Whisper, and when it
# hears "clippy", triggers the Clippy overlay inside the Claw Box VM.
# After Clippy responds, it enters conversation mode — recording follow-ups
# without needing the wake word again until Clippy dismisses.
#
# Prerequisites on host:
#   brew install ffmpeg openai-whisper
#   tart must be installed (it is if you're running the VM)
#
# Usage:
#   ./host-listener.sh              # auto-detect VM name
#   ./host-listener.sh clawbox-1    # specify VM name
# ─────────────────────────────────────────────────────────────────────────

set -uo pipefail

VM_NAME="${1:-}"
LISTEN_DURATION="${CLIPPY_WAKE_DURATION:-3}"
QUERY_DURATION="${CLIPPY_QUERY_DURATION:-5}"
FOLLOWUP_DURATION="${CLIPPY_FOLLOWUP_DURATION:-5}"
MAX_SILENCE=2  # exit conversation after N consecutive silent recordings
VOICE_TMP="${TMPDIR:-/tmp}/clippy-host-voice"
LOG_FILE="${TMPDIR:-/tmp}/clippy-host-listener.log"

SIGNAL_FILE="/tmp/clippy-listening"
INPUT_FILE="/tmp/clippy-input"

mkdir -p "$VOICE_TMP"

log() {
  echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Auto-detect VM name if not provided
if [[ -z "$VM_NAME" ]]; then
  VM_NAME=$(tart list --format json 2>/dev/null | python3 -c "
import json, sys
vms = json.load(sys.stdin)
running = [v['Name'] for v in vms if v.get('State') == 'running' or v.get('Running', False)]
if running: print(running[0])
" 2>/dev/null)
  if [[ -z "$VM_NAME" ]]; then
    echo "Error: Could not detect running VM. Pass the VM name as argument."
    echo "Usage: $0 <vm-name>"
    exit 1
  fi
  log "Auto-detected VM: $VM_NAME"
fi

# Verify tart can talk to the VM
if ! tart exec "$VM_NAME" -- echo "ok" &>/dev/null; then
  echo "Error: Cannot exec into VM '$VM_NAME'. Is it running with tart-guest-agent?"
  exit 1
fi
log "VM '$VM_NAME' is reachable via tart exec"

# Find the default mic device index
MIC_DEVICE=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep -i "microphone\|built-in\|MacBook" | head -1 | sed 's/.*\[\([0-9]*\)\].*/\1/')
if [[ -z "$MIC_DEVICE" ]]; then
  # Fallback: first audio device
  MIC_DEVICE=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep "AVFoundation audio" -A20 | grep "^\[" | head -1 | sed 's/.*\[\([0-9]*\)\].*/\1/')
fi
MIC_DEVICE="${MIC_DEVICE:-0}"
log "Using audio device: :${MIC_DEVICE}"

# Record and transcribe a chunk, return text in $TRANSCRIBED
record_and_transcribe() {
  local duration="$1"
  local label="$2"
  TRANSCRIBED=""

  local audio_file="$VOICE_TMP/${label}_$(date +%s).wav"
  ffmpeg -y -f avfoundation -i ":${MIC_DEVICE}" -t "$duration" -ar 16000 -ac 1 "$audio_file" 2>/dev/null
  if [[ ! -f "$audio_file" ]]; then
    log "Recording failed"
    return 1
  fi

  whisper "$audio_file" --model base --language en --output_format txt --output_dir "$VOICE_TMP" 2>/dev/null
  local basename=$(basename "$audio_file" .wav)
  local txt_file="$VOICE_TMP/${basename}.txt"

  if [[ ! -f "$txt_file" ]]; then
    rm -f "$audio_file"
    return 1
  fi

  TRANSCRIBED=$(sed 's/\[.*-->.*\]//g' "$txt_file" | tr -d '\n' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
  rm -f "$audio_file" "$txt_file"

  # Filter out Whisper hallucinations on silence
  local lower=$(echo "$TRANSCRIBED" | tr '[:upper:]' '[:lower:]')
  if [[ "$lower" == "you" || "$lower" == "thank you." || "$lower" == "thanks for watching." || "$lower" == "." || ${#TRANSCRIBED} -lt 2 ]]; then
    TRANSCRIBED=""
  fi
}

# Ollama on host (your Mac) – VM reaches it at this IP
OLLAMA_HOST_FOR_VM="${OLLAMA_HOST_FOR_VM:-http://192.168.1.91:11434}"
OLLAMA_MODEL_FOR_VM="${OLLAMA_MODEL_FOR_VM:-qwen3-coder:30b}"

trigger_clippy() {
  local query="$1"
  log "Triggering Clippy in VM with: $query"
  # Escape single quotes in query for safe use inside bash -c ''
  local query_escaped="${query//\'/\'\\\'\'}"
  tart exec "$VM_NAME" -- bash -c "
    export OLLAMA_HOST='$OLLAMA_HOST_FOR_VM'
    export OLLAMA_MODEL='$OLLAMA_MODEL_FOR_VM'
    pkill -f clippy-overlay 2>/dev/null
    /Users/clawbox-1/.clippy/desktop/ClippyOverlay.app/Contents/MacOS/clippy-overlay \"$query_escaped\" &
  " &>/dev/null &
}

send_followup() {
  local query="$1"
  log "Sending follow-up to Clippy: $query"
  local query_escaped="${query//\'/\'\\\'\'}"
  tart exec "$VM_NAME" -- bash -c "
    echo '$query_escaped' > $INPUT_FILE
  " &>/dev/null
}

clippy_is_listening() {
  # Check if the signal file exists in the VM
  tart exec "$VM_NAME" -- test -f "$SIGNAL_FILE" &>/dev/null
}

# ─── Conversation mode ──────────────────────────────────────────────────
# After Clippy responds, keep recording follow-ups until silence or dismiss

conversation_mode() {
  log "Entering conversation mode"
  local silence_count=0

  while true; do
    # Wait for Clippy to signal it's ready for follow-up
    log "Waiting for Clippy to be ready..."
    local wait_count=0
    while ! clippy_is_listening; do
      sleep 1
      wait_count=$((wait_count + 1))
      if [[ $wait_count -ge 30 ]]; then
        log "Clippy didn't signal ready after 30s, exiting conversation"
        return
      fi
    done

    log "Clippy is listening — recording follow-up (${FOLLOWUP_DURATION}s)"

    # Record follow-up (no wake word needed)
    record_and_transcribe "$FOLLOWUP_DURATION" "followup"

    if [[ -z "$TRANSCRIBED" ]]; then
      silence_count=$((silence_count + 1))
      log "Silent recording ($silence_count/$MAX_SILENCE)"
      if [[ $silence_count -ge $MAX_SILENCE ]]; then
        log "Max silence reached, exiting conversation"
        return
      fi
      continue
    fi

    # Got something — reset silence counter and send to Clippy
    silence_count=0
    log "Follow-up: $TRANSCRIBED"
    send_followup "$TRANSCRIBED"

    # Wait a bit for Clippy to process before polling again
    sleep 2
  done
}

# ─── Main wake word loop ────────────────────────────────────────────────

log "Clippy Host Listener starting — say 'clippy' to summon"
log "Recording ${LISTEN_DURATION}s chunks from device :${MIC_DEVICE}"

while true; do
  # Record a short chunk from the real mic
  record_and_transcribe "$LISTEN_DURATION" "wake"

  [[ -z "$TRANSCRIBED" ]] && continue

  lower_text=$(echo "$TRANSCRIBED" | tr '[:upper:]' '[:lower:]')

  # Check for wake word
  if [[ "$lower_text" != *"clippy"* ]]; then
    continue
  fi

  log "Wake word detected: $TRANSCRIBED"

  # Strip wake word to get inline query
  query=$(echo "$lower_text" | sed 's/clippy[,.\! ]*//' | sed 's/^[[:space:]]*//')

  if [[ -z "$query" ]]; then
    # Just said "clippy" — speak prompt on HOST (you hear it)
    say -v "Daniel" -r 175 "Yes?" &
    record_and_transcribe "$QUERY_DURATION" "initial"
    query="$TRANSCRIBED"
  fi

  [[ -z "$query" ]] && continue

  log "Query: $query"
  trigger_clippy "$query"

  # Enter conversation mode — keep listening for follow-ups
  sleep 3
  conversation_mode

  log "Back to wake word detection"
done
