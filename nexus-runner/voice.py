"""Nexus Runner -- Voice I/O (mic recording, Whisper STT, macOS TTS)."""

import os
import re
import subprocess
import tempfile

# Whisper hallucinations on silence
HALLUCINATIONS = {
    "you", "thank you.", "thanks for watching.", ".", "",
    "thanks.", "bye.", "goodbye.", "thank you for watching.",
    "the end.", "so,", "i'm sorry.", "pause", "blank audio",
    "silence", "coughing", "music", "applause", "laughter",
    "breathing", "sighing", "inaudible", "noise",
}

_mic_device = None
_tmp_dir = None


def _get_tmp_dir() -> str:
    global _tmp_dir
    if _tmp_dir is None:
        _tmp_dir = tempfile.mkdtemp(prefix="nexus-voice-")
    return _tmp_dir


def find_mic() -> str:
    """Detect the default microphone device index for ffmpeg avfoundation."""
    global _mic_device
    if _mic_device is not None:
        return _mic_device

    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stderr

        # Look for microphone or known audio device
        for line in output.splitlines():
            lower = line.lower()
            if any(kw in lower for kw in ["microphone", "built-in", "macbook", "maono"]):
                match = re.search(r'\[(\d+)\]', line)
                if match:
                    _mic_device = match.group(1)
                    return _mic_device

        # Fallback: first audio input device after "AVFoundation audio devices:"
        in_audio = False
        for line in output.splitlines():
            if "audio devices" in line.lower():
                in_audio = True
                continue
            if in_audio:
                match = re.search(r'\[(\d+)\]', line)
                if match:
                    _mic_device = match.group(1)
                    return _mic_device
    except Exception:
        pass

    _mic_device = "0"
    return _mic_device


def listen(duration: int = 3) -> str | None:
    """Record from mic and transcribe with Whisper. Returns text or None."""
    mic = find_mic()
    tmp = _get_tmp_dir()
    audio_file = os.path.join(tmp, f"rec_{os.getpid()}_{id(duration)}.wav")

    # Record
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "avfoundation",
                "-i", f":{mic}",
                "-t", str(duration),
                "-ar", "16000",
                "-ac", "1",
                audio_file,
            ],
            capture_output=True, timeout=duration + 10,
        )
    except subprocess.TimeoutExpired:
        _cleanup(audio_file)
        return None

    if not os.path.isfile(audio_file):
        return None

    # Transcribe with whisper-cpp (Metal-accelerated, ~10x faster than Python whisper)
    model_path = os.environ.get(
        "WHISPER_MODEL",
        os.path.expanduser("~/.local/share/whisper-cpp/ggml-base.en.bin"),
    )
    try:
        result = subprocess.run(
            [
                "whisper-cli",
                "-m", model_path,
                "-f", audio_file,
                "-l", "en",
                "-nt",  # no timestamps
            ],
            capture_output=True, text=True, timeout=10,
        )
        raw = result.stdout
    except subprocess.TimeoutExpired:
        _cleanup(audio_file)
        return None

    # whisper-cli outputs text directly to stdout
    text = raw.strip() if raw else None

    # Clean up
    _cleanup(audio_file)

    if not text or len(text) < 2:
        return None

    # Filter hallucinations
    cleaned = text.lower().strip().rstrip('.').strip()
    if cleaned in HALLUCINATIONS or text.lower() in HALLUCINATIONS:
        return None
    # Filter bracketed/parenthesized artifacts like [Pause], (coughing), [BLANK_AUDIO]
    if re.match(r'^[\[\(].*[\]\)]$', text.strip()):
        return None
    if len(cleaned) < 3:
        return None

    return text


def _strip_emojis(text: str) -> str:
    """Remove emoji characters so TTS doesn't try to read them."""
    return re.sub(
        r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
        r'\U0001F1E0-\U0001F1FF\U0001FA00-\U0001FAFF\U00002702-\U000027B0'
        r'\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF'
        r'\U00002700-\U000027BF\U0000231A-\U0000231B\U000023E9-\U000023F3'
        r'\U000023F8-\U000023FA\U000025AA-\U000025AB\U000025B6\U000025C0'
        r'\U000025FB-\U000025FE\U00002934-\U00002935\U00002B05-\U00002B07'
        r'\U00002B1B-\U00002B1C\U00002B50\U00002B55\U00003030\U0000303D'
        r'\U00003297\U00003299\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF]+',
        '', text
    )


def clean_for_voice(text: str) -> str:
    """Strip markdown/code/tags/emojis for spoken output."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'`[^`]+`', '', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = _strip_emojis(text)
    text = re.sub(r'\n{2,}', '. ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def list_audio_devices() -> list[dict]:
    """List audio output devices available to macOS say."""
    devices = [{"id": "", "name": "System Default"}]
    try:
        r = subprocess.run(["say", "-a", "?"], capture_output=True, text=True, timeout=5)
        for line in (r.stdout + r.stderr).splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                devices.append({"id": parts[0], "name": parts[1]})
    except Exception:
        pass
    return devices


def speak(text: str, voice: str = "Zarvox", rate: int = 200,
          audio_device: str = "") -> None:
    """Speak text using macOS say. Blocks until done.
    audio_device: numeric device ID for say -a, or empty for system default."""
    if not text:
        return
    text = clean_for_voice(text)[:5000]
    cmd = ["say", "-v", voice, "-r", str(rate)]
    if audio_device:
        cmd.extend(["-a", audio_device])
    cmd.append(text)
    try:
        subprocess.run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        pass


def _cleanup(*files):
    for f in files:
        try:
            os.remove(f)
        except OSError:
            pass
