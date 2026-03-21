#!/usr/bin/env python3
"""Spotify control skill for Nexus Runner. Uses AppleScript via osascript to control Spotify on macOS."""

import subprocess
import sys


def osascript(script: str) -> str:
    """Run an AppleScript snippet and return stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    output = result.stdout.strip()
    if result.returncode != 0 and result.stderr.strip():
        return f"Error: {result.stderr.strip()}"
    return output


def spotify_command(action: str) -> str:
    """Send a simple command to Spotify (play, pause, next track, etc.)."""
    return osascript(f'tell application "Spotify" to {action}')


def get_now_playing() -> str:
    """Get current track info from Spotify."""
    script = '''
    tell application "Spotify"
        if player state is stopped then
            return "Spotify is stopped"
        end if
        set trackName to name of current track
        set trackArtist to artist of current track
        set trackAlbum to album of current track
        set trackDuration to duration of current track
        set trackPosition to player position
        set totalSec to trackDuration / 1000
        set totalMin to (totalSec div 60) as integer
        set totalRemSec to (totalSec mod 60) as integer
        set posMin to (trackPosition div 60) as integer
        set posRemSec to (trackPosition mod 60) as integer
        set posMinStr to posMin as string
        set posSecStr to posRemSec as string
        if posRemSec < 10 then set posSecStr to "0" & posSecStr
        set totMinStr to totalMin as string
        set totSecStr to totalRemSec as string
        if totalRemSec < 10 then set totSecStr to "0" & totSecStr
        return trackName & " by " & trackArtist & " (" & trackAlbum & ") [" & posMinStr & ":" & posSecStr & "/" & totMinStr & ":" & totSecStr & "]"
    end tell
    '''
    return osascript(script)


def set_volume(level: int) -> str:
    """Set Spotify volume to a value between 0 and 100."""
    level = max(0, min(100, level))
    osascript(f'tell application "Spotify" to set sound volume to {level}')
    return f"Volume set to {level}"


def get_volume() -> int:
    """Get current Spotify volume."""
    result = osascript('tell application "Spotify" to return sound volume')
    try:
        return int(result)
    except ValueError:
        return -1


def volume_up() -> str:
    """Increase volume by 10."""
    current = get_volume()
    if current < 0:
        return "Could not read volume"
    new_level = min(100, current + 10)
    return set_volume(new_level)


def volume_down() -> str:
    """Decrease volume by 10."""
    current = get_volume()
    if current < 0:
        return "Could not read volume"
    new_level = max(0, current - 10)
    return set_volume(new_level)


def search_and_play(query: str) -> str:
    """Search for a track/artist/album in Spotify and play it."""
    escaped = query.replace('"', '\\"').replace("'", "'\\''")
    script = f'''
    tell application "Spotify"
        activate
        delay 0.5
    end tell
    tell application "System Events"
        tell process "Spotify"
            keystroke "l" using command down
            delay 0.3
            keystroke "a" using command down
            keystroke "{escaped}"
            delay 1.5
            key code 36
        end tell
    end tell
    return "Searching for: {escaped}"
    '''
    # Alternative approach using Spotify URI which is more reliable
    uri_script = f'tell application "Spotify" to play track "spotify:search:{escaped}"'
    result = osascript(uri_script)
    if "Error" in result:
        # Fallback: open Spotify search URI
        subprocess.run(
            ["open", f"spotify:search:{query}"],
            capture_output=True, timeout=5,
        )
        return f"Opened Spotify search for: {query}"
    return f"Playing results for: {query}"


def set_shuffle(state: bool) -> str:
    """Enable or disable shuffle."""
    val = "true" if state else "false"
    osascript(f'tell application "Spotify" to set shuffling to {val}')
    return f"Shuffle {'on' if state else 'off'}"


def set_repeat(state: bool) -> str:
    """Enable or disable repeat."""
    val = "true" if state else "false"
    osascript(f'tell application "Spotify" to set repeating to {val}')
    return f"Repeat {'on' if state else 'off'}"


def like_track() -> str:
    """Add the current track to your library. Uses keyboard shortcut via System Events."""
    # Spotify does not expose a direct AppleScript command for saving to library.
    # We use the keyboard shortcut Cmd+Shift+S which is Spotify's "Save to Your Library" shortcut on macOS.
    script = '''
    tell application "Spotify" to activate
    delay 0.3
    tell application "System Events"
        tell process "Spotify"
            keystroke "s" using {command down, shift down}
        end tell
    end tell
    return "Liked current track (added to library)"
    '''
    return osascript(script)


def queue_track(query: str) -> str:
    """Add a track to the queue. NOTE: Spotify's AppleScript API does not support queue management."""
    return (
        f"Queue is not supported via AppleScript. "
        f"Use 'search {query}' to find and play the track instead, "
        f"or add to queue manually in the Spotify app."
    )


def list_audio_devices() -> str:
    """List available audio output devices via SwitchAudioSource."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-a", "-t", "output"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return "Error: SwitchAudioSource not found. Install with: brew install switchaudio-osx"
        devices = [d.strip() for d in result.stdout.strip().splitlines() if d.strip()]
        # Get current
        cur = subprocess.run(
            ["SwitchAudioSource", "-c", "-t", "output"],
            capture_output=True, text=True, timeout=5,
        )
        current = cur.stdout.strip()
        lines = []
        for d in devices:
            marker = " [active]" if d == current else ""
            lines.append(f"  {d}{marker}")
        return "Audio output devices:\n" + "\n".join(lines)
    except FileNotFoundError:
        return "SwitchAudioSource not installed. Run: brew install switchaudio-osx"


def switch_audio(device_name: str) -> str:
    """Switch system audio output to the named device."""
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-s", device_name, "-t", "output"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return f"Failed to switch: {result.stderr.strip()}"
        return output if output else f"Switched audio output to {device_name}"
    except FileNotFoundError:
        return "SwitchAudioSource not installed. Run: brew install switchaudio-osx"


def main():
    if len(sys.argv) < 2:
        print("Usage: spotify_control.py <command> [args]")
        print("Commands: play, pause, toggle, next, skip, previous, prev,")
        print("          volume up, volume down, volume <0-100>,")
        print("          what, now, current, playing,")
        print("          search <query>, shuffle on/off, repeat on/off,")
        print("          like, queue <query>,")
        print("          audio [device], devices")
        sys.exit(1)

    raw = " ".join(sys.argv[1:]).strip().lower()
    parts = raw.split(None, 1)
    cmd = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    if cmd in ("play",):
        if rest:
            print(search_and_play(rest))
        else:
            spotify_command("play")
            print("Playing")

    elif cmd in ("pause",):
        spotify_command("pause")
        print("Paused")

    elif cmd in ("toggle",):
        spotify_command("playpause")
        print("Toggled play/pause")

    elif cmd in ("next", "skip"):
        spotify_command("next track")
        print("Skipped to next track")

    elif cmd in ("previous", "prev"):
        spotify_command("previous track")
        print("Back to previous track")

    elif cmd == "volume":
        if rest == "up":
            print(volume_up())
        elif rest == "down":
            print(volume_down())
        else:
            try:
                level = int(rest)
                print(set_volume(level))
            except ValueError:
                print(f"Invalid volume: '{rest}'. Use 'up', 'down', or a number 0-100.")

    elif cmd in ("what", "now", "current", "playing"):
        print(get_now_playing())

    elif cmd == "search":
        if not rest:
            print("Usage: spotify_control.py search <query>")
        else:
            print(search_and_play(rest))

    elif cmd == "shuffle":
        if rest in ("on", "true", "1"):
            print(set_shuffle(True))
        elif rest in ("off", "false", "0"):
            print(set_shuffle(False))
        else:
            print("Usage: shuffle on/off")

    elif cmd == "repeat":
        if rest in ("on", "true", "1"):
            print(set_repeat(True))
        elif rest in ("off", "false", "0"):
            print(set_repeat(False))
        else:
            print("Usage: repeat on/off")

    elif cmd == "like":
        print(like_track())

    elif cmd == "queue":
        if not rest:
            print("Usage: spotify_control.py queue <query>")
        else:
            print(queue_track(rest))

    elif cmd in ("audio", "output", "speaker", "speakers"):
        if not rest:
            print(list_audio_devices())
        else:
            print(switch_audio(rest))

    elif cmd == "devices":
        print(list_audio_devices())

    else:
        print(f"Unknown command: {cmd}")
        print("Try: play, pause, toggle, next, previous, volume, what, search, shuffle, repeat, like, queue, audio, devices")
        sys.exit(1)


if __name__ == "__main__":
    main()
