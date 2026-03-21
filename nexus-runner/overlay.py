#!/usr/bin/env python3 -u
"""
overlay.py - Desktop overlay for Nexus Runner avatars (Clippy + Nexus robot).

Connects to nexus-runner's WebSocket server and displays a floating avatar
sprite with a speech bubble that streams text word-by-word while TTS plays.
Draggable, transparent, no dock icon. Works with any avatar.
"""

import json
import math
import os
import random
import re
import subprocess
import tempfile
import threading
import asyncio
import signal
import sys
import time

import fcntl
import objc
import AppKit
import Foundation
from PyObjCTools import AppHelper

import websockets

# Global lock file handle -- must stay open for lifetime of process
_lock_fh = None

# Sound effects using macOS system sounds
SOUND_WAKE = "/System/Library/Sounds/Blow.aiff"      # wake up chime
SOUND_LISTEN = "/System/Library/Sounds/Tink.aiff"     # listening ping


def play_sound(path):
    """Play a sound file asynchronously via NSSound."""
    snd = AppKit.NSSound.alloc().initWithContentsOfFile_byReference_(path, True)
    if snd:
        snd.play()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WS_URL = "ws://localhost:5555/ws"
RECONNECT_DELAY = 3          # seconds between reconnect attempts
SCALE = 7                    # each pixel = 7x7 points  (16*7 = 112)
SPRITE_W = 16 * SCALE        # 112 base width
SPRITE_H = 16 * SCALE        # 112 base height (overridden per avatar)
SPRITE_SIZE = SPRITE_W       # backwards compat alias
BUBBLE_WIDTH = 280
BUBBLE_PADDING = 14
TAIL_SIZE = 10               # speech-bubble tail width
HIDE_DELAY = 6.0             # seconds after last word before auto-hide
WORD_INTERVAL = 0.07         # default seconds between words (overridden by TTS rate)

# Default voice settings (overridden by server init message)
DEFAULT_VOICE = "Samantha"
DEFAULT_RATE = 180

# ---------------------------------------------------------------------------
# Clippy pixel data  (x, y, hex_color) -- paperclip + mustache + cigarette + coffee
# Matches the SVG icon: wire paperclip body, googly eyes, handlebar mustache,
# cigarette with ember + smoke, coffee mug with steam
# ---------------------------------------------------------------------------
CLIPPY_PIXELS = [
    # Paperclip top curve (silver wire)
    (6,0,"c0c0c0"),(7,0,"c0c0c0"),(8,0,"c0c0c0"),(9,0,"c0c0c0"),
    (5,1,"b0b0b0"),(10,1,"b0b0b0"),
    (5,2,"b0b0b0"),(10,2,"b0b0b0"),
    # Googly eyes -- white with dark pupil
    (5,2,"ffffff"),(6,2,"ffffff"),(7,2,"ffffff"),
    (9,2,"ffffff"),(10,2,"ffffff"),(11,2,"ffffff"),
    (5,3,"ffffff"),(6,3,"ffffff"),(7,3,"ffffff"),
    (9,3,"ffffff"),(10,3,"ffffff"),(11,3,"ffffff"),
    (5,4,"ffffff"),(6,4,"ffffff"),(7,4,"ffffff"),
    (9,4,"ffffff"),(10,4,"ffffff"),(11,4,"ffffff"),
    # Eye outlines (gray ring)
    (5,2,"888888"),(7,2,"888888"),
    (5,4,"888888"),(6,4,"888888"),(7,4,"888888"),
    (5,3,"888888"),(7,3,"888888"),
    (9,2,"888888"),(11,2,"888888"),
    (9,4,"888888"),(10,4,"888888"),(11,4,"888888"),
    (9,3,"888888"),(11,3,"888888"),
    # Pupils (dark, slightly right-of-center for personality)
    (6,3,"222222"),(7,3,"222222"),
    (10,3,"222222"),(11,3,"222222"),
    # Handlebar mustache (medium, curling)
    (5,5,"4a3728"),(6,5,"4a3728"),(7,5,"4a3728"),(8,5,"4a3728"),
    (9,5,"4a3728"),(10,5,"4a3728"),(11,5,"4a3728"),
    (4,5,"3a2718"),(12,5,"3a2718"),  # curl tips
    (4,4,"3a2718"),(12,4,"3a2718"),  # curl up
    # Cigarette (from mouth, sticking out right)
    (10,5,"d4c89a"),  # filter at mouth
    (11,5,"f5f0e0"),(12,5,"f5f0e0"),(13,5,"f5f0e0"),(14,5,"f5f0e0"),
    (15,5,"ff8c00"),  # ember tip
    # Smoke wisps
    (15,4,"aaaaaa"),(14,3,"aaaaaa"),(15,2,"aaaaaa"),
    # Paperclip wire body (vertical sections)
    (5,6,"b0b0b0"),(10,6,"b0b0b0"),
    (5,7,"c0c0c0"),(10,7,"c0c0c0"),
    (5,8,"b0b0b0"),(6,8,"b0b0b0"),(7,8,"b0b0b0"),
    (8,8,"b0b0b0"),(9,8,"b0b0b0"),(10,8,"b0b0b0"),
    (6,9,"c0c0c0"),(9,9,"c0c0c0"),
    (6,10,"b0b0b0"),(9,10,"b0b0b0"),
    (6,11,"b8b8b8"),(7,11,"b8b8b8"),(8,11,"b8b8b8"),(9,11,"b8b8b8"),
    (7,12,"a8a8a8"),(8,12,"a8a8a8"),
    (7,13,"b0b0b0"),(8,13,"b0b0b0"),
    (6,14,"909090"),(7,14,"909090"),(8,14,"909090"),(9,14,"909090"),
    (6,15,"888888"),(7,15,"888888"),(8,15,"888888"),(9,15,"888888"),
    # Coffee mug (left side, held by clip)
    (1,8,"8b4513"),(2,8,"8b4513"),(3,8,"8b4513"),
    (1,9,"8b4513"),(2,9,"8b4513"),(3,9,"8b4513"),
    (1,10,"8b4513"),(2,10,"8b4513"),(3,10,"8b4513"),
    (1,11,"8b4513"),(2,11,"8b4513"),(3,11,"8b4513"),
    # Mug handle
    (0,9,"6b3503"),(0,10,"6b3503"),
    # Coffee surface
    (1,8,"3a1f0a"),(2,8,"3a1f0a"),(3,8,"3a1f0a"),
    # Steam
    (2,7,"aaaaaa"),(1,6,"aaaaaa"),
]

# ---------------------------------------------------------------------------
# Nexus robot pixel data (16x16) -- matches SVG: antenna, rectangular head,
# green eye slits, mouth grille, ear bolts, chest light
# ---------------------------------------------------------------------------
NEXUS_PIXELS = [
    # Antenna ball (green)
    (7,0,"00ff88"),(8,0,"00ff88"),
    # Antenna stem
    (7,1,"888888"),(8,1,"888888"),
    # Head top
    (4,2,"555555"),(5,2,"555555"),(6,2,"555555"),(7,2,"555555"),
    (8,2,"555555"),(9,2,"555555"),(10,2,"555555"),(11,2,"555555"),
    # Head side + face
    (3,3,"666666"),(4,3,"666666"),(5,3,"666666"),(6,3,"666666"),
    (7,3,"666666"),(8,3,"666666"),(9,3,"666666"),(10,3,"666666"),
    (11,3,"666666"),(12,3,"666666"),
    # Eye sockets (dark)
    (3,4,"666666"),(12,4,"666666"),
    (4,4,"222222"),(5,4,"222222"),(6,4,"222222"),
    (9,4,"222222"),(10,4,"222222"),(11,4,"222222"),
    # Eye glow (green rectangles, matching SVG)
    (5,4,"00ff88"),(6,4,"00ff88"),
    (10,4,"00ff88"),(11,4,"00ff88"),
    (3,5,"666666"),(12,5,"666666"),
    (4,5,"222222"),(5,5,"222222"),(6,5,"222222"),
    (9,5,"222222"),(10,5,"222222"),(11,5,"222222"),
    (5,5,"00cc66"),(6,5,"00cc66"),
    (10,5,"00cc66"),(11,5,"00cc66"),
    # Ear bolts
    (2,4,"777777"),(2,5,"888888"),
    (13,4,"777777"),(13,5,"888888"),
    # Mouth grille (horizontal bar with vertical slits)
    (3,6,"666666"),(4,6,"333333"),(5,6,"555555"),(6,6,"333333"),
    (7,6,"555555"),(8,6,"333333"),(9,6,"555555"),(10,6,"333333"),
    (11,6,"555555"),(12,6,"666666"),
    # Jaw
    (3,7,"555555"),(4,7,"555555"),(5,7,"555555"),(6,7,"555555"),
    (7,7,"555555"),(8,7,"555555"),(9,7,"555555"),(10,7,"555555"),
    (11,7,"555555"),(12,7,"555555"),
    # Neck
    (6,8,"777777"),(7,8,"777777"),(8,8,"777777"),(9,8,"777777"),
    # Body (chest plate)
    (3,9,"444444"),(4,9,"444444"),(5,9,"444444"),(6,9,"444444"),
    (7,9,"444444"),(8,9,"444444"),(9,9,"444444"),(10,9,"444444"),
    (11,9,"444444"),(12,9,"444444"),
    (2,10,"555555"),(3,10,"555555"),(4,10,"555555"),(5,10,"555555"),
    (6,10,"333333"),(7,10,"ff2d55"),(8,10,"ff2d55"),(9,10,"333333"),
    (10,10,"555555"),(11,10,"555555"),(12,10,"555555"),(13,10,"555555"),
    (2,11,"666666"),(3,11,"666666"),(4,11,"666666"),(5,11,"666666"),
    (6,11,"666666"),(7,11,"666666"),(8,11,"666666"),(9,11,"666666"),
    (10,11,"666666"),(11,11,"666666"),(12,11,"666666"),(13,11,"666666"),
    (3,12,"555555"),(4,12,"555555"),(5,12,"555555"),(6,12,"555555"),
    (7,12,"555555"),(8,12,"555555"),(9,12,"555555"),(10,12,"555555"),
    (11,12,"555555"),(12,12,"555555"),
    # Arms
    (1,10,"888888"),(1,11,"888888"),
    (14,10,"888888"),(14,11,"888888"),
    (0,11,"999999"),(15,11,"999999"),
    # Legs
    (4,13,"777777"),(5,13,"777777"),(6,13,"777777"),
    (9,13,"777777"),(10,13,"777777"),(11,13,"777777"),
    (4,14,"666666"),(5,14,"666666"),(6,14,"666666"),
    (9,14,"666666"),(10,14,"666666"),(11,14,"666666"),
    # Feet
    (3,15,"555555"),(4,15,"555555"),(5,15,"555555"),(6,15,"555555"),
    (9,15,"555555"),(10,15,"555555"),(11,15,"555555"),(12,15,"555555"),
]

# ---------------------------------------------------------------------------
# SVG data for avatars -- matches the web dashboard icons exactly
# ---------------------------------------------------------------------------
AVATAR_SVGS = {
    "clippy": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 50 68" width="100" height="136" preserveAspectRatio="xMidYMid meet">
        <!-- Paperclip wire body -->
        <path d="M16 58V16a9 9 0 0 1 18 0v32a6 6 0 0 1-12 0V20a2.5 2.5 0 0 1 5 0v24"
              fill="none" stroke="#b8b8b8" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M17 56V17a8 8 0 0 1 16 0v30"
              fill="none" stroke="#d0d0d0" stroke-width="1" stroke-linecap="round" opacity="0.5"/>
        <!-- Left eye (round, bloodshot, half above head) -->
        <circle cx="19" cy="7" r="4.5" fill="#fff" stroke="#888" stroke-width="0.8"/>
        <line x1="15.2" y1="6" x2="16.8" y2="7" stroke="#cc3333" stroke-width="0.4" opacity="0.6"/>
        <line x1="15.8" y1="4" x2="17.3" y2="5.5" stroke="#cc3333" stroke-width="0.3" opacity="0.5"/>
        <circle cx="20" cy="6.5" r="1.8" fill="#222"/>
        <circle cx="20.5" cy="5.8" r="0.6" fill="#fff"/>
        <!-- Right eye (round, bloodshot, half above head) -->
        <circle cx="31" cy="7" r="4.5" fill="#fff" stroke="#888" stroke-width="0.8"/>
        <line x1="34.8" y1="6" x2="33.2" y2="7" stroke="#cc3333" stroke-width="0.4" opacity="0.6"/>
        <line x1="34.2" y1="4" x2="32.7" y2="5.5" stroke="#cc3333" stroke-width="0.3" opacity="0.5"/>
        <circle cx="32" cy="6.5" r="1.8" fill="#222"/>
        <circle cx="32.5" cy="5.8" r="0.6" fill="#fff"/>
        <!-- Brow lines (raised above eyes) -->
        <path d="M15 2.5 Q19 1 22 2" fill="none" stroke="#888" stroke-width="1" stroke-linecap="round"/>
        <path d="M28 2 Q31 1 35 2.5" fill="none" stroke="#888" stroke-width="1" stroke-linecap="round"/>
        <!-- Mouth -->
        <path d="M23 17 Q26 18.5 29 17" fill="none" stroke="#666" stroke-width="1.2" stroke-linecap="round"/>
        <!-- Sam Elliott walrus mustache -->
        <path d="M20 14 Q26 12.5 32 14" fill="none" stroke="#4a3218" stroke-width="2.5" stroke-linecap="round"/>
        <path d="M20.5 15 Q26 14 31.5 15" fill="none" stroke="#5a4228" stroke-width="2" stroke-linecap="round"/>
        <path d="M23 13.7 Q26 13 29 13.7" fill="none" stroke="#8a8078" stroke-width="1" stroke-linecap="round" opacity="0.6"/>
        <!-- Left droop -->
        <path d="M20 14 Q18 16 17 18.5" fill="none" stroke="#4a3218" stroke-width="2" stroke-linecap="round"/>
        <path d="M20.5 15 Q19 17 18 18.5" fill="none" stroke="#5a4228" stroke-width="1.5" stroke-linecap="round"/>
        <!-- Right droop -->
        <path d="M32 14 Q34 16 35 18.5" fill="none" stroke="#4a3218" stroke-width="2" stroke-linecap="round"/>
        <path d="M31.5 15 Q33 17 34 18.5" fill="none" stroke="#5a4228" stroke-width="1.5" stroke-linecap="round"/>
        <!-- Cigarette -->
        <g transform="rotate(12, 28, 16.5)">
            <rect x="28" y="15.5" width="3" height="2.2" rx="0.6" fill="#d4c89a"/>
            <rect x="31" y="15.5" width="10" height="2.2" rx="0.5" fill="#f5f0e0"/>
            <rect x="40" y="15.5" width="2.5" height="2.2" rx="0.4" fill="#999" opacity="0.7"/>
            <circle cx="43.5" cy="16.6" r="0.9" fill="#ff2200"/>
            <circle cx="43.5" cy="16.6" r="1.6" fill="#ff4400" opacity="0.2"/>
        </g>
        <!-- Smoke wisps -->
        <path d="M44 13 Q45 9 43 5 Q45 2 44 0" fill="none" stroke="#bbb" stroke-width="0.7" opacity="0.35"/>
        <path d="M45 12 Q47 8 45 4" fill="none" stroke="#aaa" stroke-width="0.5" opacity="0.25"/>
        <!-- Coffee mug (tucked alongside lower body) -->
        <rect x="4" y="30" width="9" height="10" rx="2" fill="#8B4513" stroke="#6B3503" stroke-width="0.8"/>
        <path d="M13 32.5 Q16 32.5 16 36 Q16 39 13 39" fill="none" stroke="#6B3503" stroke-width="1.5" stroke-linecap="round"/>
        <text x="8.5" y="36.5" text-anchor="middle" font-family="monospace" font-size="3" fill="#d4a054" font-weight="bold">#1</text>
        <text x="8.5" y="39" text-anchor="middle" font-family="monospace" font-size="2" fill="#d4a054">FKN IT</text>
        <ellipse cx="8.5" cy="31.5" rx="3.5" ry="1.2" fill="#2a1505"/>
        <!-- Steam -->
        <path d="M7 28 Q8.5 26 7 24" fill="none" stroke="#ccc" stroke-width="0.7" opacity="0.4"/>
        <path d="M10 27 Q11.5 25 10 23" fill="none" stroke="#ccc" stroke-width="0.7" opacity="0.4"/>
    </svg>""",
    "nexus": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="112" height="112">
        <line x1="24" y1="2" x2="24" y2="8" stroke="#888" stroke-width="2.5" stroke-linecap="round"/>
        <circle cx="24" cy="2" r="2.5" fill="#00ff88"/>
        <rect x="8" y="8" width="32" height="28" rx="4" fill="#555" stroke="#666" stroke-width="1.5"/>
        <rect x="13" y="15" width="8" height="7" rx="1" fill="#222"/>
        <rect x="27" y="15" width="8" height="7" rx="1" fill="#222"/>
        <rect x="15" y="17" width="4" height="3" rx="0.5" fill="#00ff88"/>
        <rect x="29" y="17" width="4" height="3" rx="0.5" fill="#00ff88"/>
        <rect x="16" y="28" width="16" height="4" rx="1" fill="#333"/>
        <line x1="20" y1="28" x2="20" y2="32" stroke="#555" stroke-width="1"/>
        <line x1="24" y1="28" x2="24" y2="32" stroke="#555" stroke-width="1"/>
        <line x1="28" y1="28" x2="28" y2="32" stroke="#555" stroke-width="1"/>
        <circle cx="8" cy="22" r="2.5" fill="#777" stroke="#888" stroke-width="1"/>
        <circle cx="40" cy="22" r="2.5" fill="#777" stroke="#888" stroke-width="1"/>
        <rect x="8" y="38" width="32" height="6" rx="2" fill="#444"/>
        <rect x="20" y="39.5" width="8" height="3" rx="1" fill="#ff2d55"/>
    </svg>""",
    "zelthor": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="112" height="112">
        <!-- Antennae -->
        <line x1="18" y1="10" x2="14" y2="3" stroke="#2a9a3a" stroke-width="2" stroke-linecap="round"/>
        <circle cx="14" cy="2.5" r="2.5" fill="#00ff44"/>
        <circle cx="14.5" cy="2" r="0.8" fill="#88ff88" opacity="0.6"/>
        <line x1="30" y1="10" x2="34" y2="3" stroke="#2a9a3a" stroke-width="2" stroke-linecap="round"/>
        <circle cx="34" cy="2.5" r="2.5" fill="#00ff44"/>
        <circle cx="34.5" cy="2" r="0.8" fill="#88ff88" opacity="0.6"/>
        <!-- Head -->
        <ellipse cx="24" cy="22" rx="16" ry="18" fill="#3aaa4a" stroke="#2a8a3a" stroke-width="1.2"/>
        <ellipse cx="24" cy="23" rx="14" ry="16" fill="#44bb55"/>
        <!-- Forehead ridge -->
        <path d="M14 14 Q18 10 24 9 Q30 10 34 14" fill="none" stroke="#2a9a3a" stroke-width="1.5" stroke-linecap="round"/>
        <!-- Eyes (big almond, dark with green glow) -->
        <ellipse cx="17" cy="20" rx="5.5" ry="6.5" fill="#111" stroke="#1a6a2a" stroke-width="0.8" transform="rotate(-10, 17, 20)"/>
        <ellipse cx="31" cy="20" rx="5.5" ry="6.5" fill="#111" stroke="#1a6a2a" stroke-width="0.8" transform="rotate(10, 31, 20)"/>
        <ellipse cx="17" cy="20" rx="3" ry="3.5" fill="#00cc33"/>
        <ellipse cx="31" cy="20" rx="3" ry="3.5" fill="#00cc33"/>
        <ellipse cx="17.5" cy="19" rx="1" ry="1.2" fill="#88ff88" opacity="0.5"/>
        <ellipse cx="31.5" cy="19" rx="1" ry="1.2" fill="#88ff88" opacity="0.5"/>
        <!-- Nose dots -->
        <circle cx="22.5" cy="27" r="0.8" fill="#2a7a3a"/>
        <circle cx="25.5" cy="27" r="0.8" fill="#2a7a3a"/>
        <!-- Mouth -->
        <path d="M19 31 Q24 34 29 31" fill="none" stroke="#1a6a2a" stroke-width="1.2" stroke-linecap="round"/>
        <!-- Cheek markings -->
        <path d="M8 24 L11 23 L11 25Z" fill="#2a9a3a" opacity="0.4"/>
        <path d="M40 24 L37 23 L37 25Z" fill="#2a9a3a" opacity="0.4"/>
        <!-- Neck -->
        <rect x="20" y="38" width="8" height="4" rx="2" fill="#3aaa4a"/>
        <!-- Collar/suit -->
        <path d="M16 42 L20 39 L28 39 L32 42 L36 44 L12 44Z" fill="#225533" stroke="#1a4428" stroke-width="0.8"/>
        <circle cx="24" cy="42" r="1.5" fill="#00ff44"/>
    </svg>""",
}


# ---------------------------------------------------------------------------
# Eye positions per avatar (SVG coordinates: cx, cy, radius, viewbox_size)
# ---------------------------------------------------------------------------
AVATAR_EYES = {
    "clippy": {
        "vb_w": 50, "vb_h": 68,  # viewBox width x height (tall & slender)
        "left":  (19, 7, 4.5),
        "right": (31, 7, 4.5),
    },
    "nexus": {
        "vb_w": 48, "vb_h": 48,
        "left":  (17, 18.5, 4),
        "right": (31, 18.5, 4),
    },
    "zelthor": {
        "vb_w": 48, "vb_h": 48,
        "left":  (17, 20, 5.5),
        "right": (31, 20, 5.5),
    },
}


# ---------------------------------------------------------------------------
# BloodshotEyeView -- animated bloodshot eye with darting pupil
# ---------------------------------------------------------------------------
class BloodshotEyeView(AppKit.NSView):
    _pupil_dx = 0.0
    _pupil_dy = 0.0
    _vein_data = None

    def initWithFrame_(self, frame):
        self = objc.super(BloodshotEyeView, self).initWithFrame_(frame)
        if self:
            # Pre-generate vein angles so they don't flicker
            self._vein_data = []
            for base in range(0, 360, 40):
                angle = math.radians(base + random.uniform(-12, 12))
                wobble = random.uniform(-0.12, 0.12)
                length = random.uniform(0.7, 0.95)
                width = random.uniform(0.4, 0.9)
                self._vein_data.append((angle, wobble, length, width))
        return self

    def setPupilOffset_dy_(self, dx, dy):
        self._pupil_dx = dx
        self._pupil_dy = dy
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        cx = b.size.width / 2
        cy = b.size.height / 2
        r = min(cx, cy) - 1

        # Sclera (off-white, slightly yellow for that tired look)
        eye_rect = Foundation.NSMakeRect(cx - r, cy - r, r * 2, r * 2)
        sclera = AppKit.NSBezierPath.bezierPathWithOvalInRect_(eye_rect)
        AppKit.NSColor.colorWithRed_green_blue_alpha_(0.98, 0.96, 0.93, 1.0).setFill()
        sclera.fill()

        # Bloodshot veins -- offset origin follows the pupil so veins move with eyes
        vein_cx = cx + self._pupil_dx * 0.5
        vein_cy = cy + self._pupil_dy * 0.5
        for angle, wobble, length, width in (self._vein_data or []):
            vein_color = AppKit.NSColor.colorWithRed_green_blue_alpha_(
                0.85, 0.08, 0.05, random.uniform(0.4, 0.7)
            )
            vein_color.setStroke()
            inner_r = r * 0.32
            outer_r = r * length
            mid_r = (inner_r + outer_r) / 2
            mid_angle = angle + wobble

            vein = AppKit.NSBezierPath.bezierPath()
            vein.moveToPoint_(Foundation.NSMakePoint(
                vein_cx + inner_r * math.cos(angle), vein_cy + inner_r * math.sin(angle)
            ))
            vein.lineToPoint_(Foundation.NSMakePoint(
                vein_cx + mid_r * math.cos(mid_angle), vein_cy + mid_r * math.sin(mid_angle)
            ))
            vein.lineToPoint_(Foundation.NSMakePoint(
                vein_cx + outer_r * math.cos(angle), vein_cy + outer_r * math.sin(angle)
            ))
            vein.setLineWidth_(width)
            vein.stroke()

        # Iris (dark brown/black)
        ir = r * 0.45
        ix = cx + self._pupil_dx
        iy = cy + self._pupil_dy
        iris_path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            Foundation.NSMakeRect(ix - ir, iy - ir, ir * 2, ir * 2)
        )
        AppKit.NSColor.colorWithRed_green_blue_alpha_(0.18, 0.12, 0.08, 1.0).setFill()
        iris_path.fill()

        # Pupil (pure black)
        pr = r * 0.25
        pupil_path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            Foundation.NSMakeRect(ix - pr, iy - pr, pr * 2, pr * 2)
        )
        AppKit.NSColor.blackColor().setFill()
        pupil_path.fill()

        # Highlight glint
        hr = r * 0.1
        glint = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            Foundation.NSMakeRect(ix + pr * 0.3, iy + pr * 0.3, hr * 2, hr * 2)
        )
        AppKit.NSColor.colorWithWhite_alpha_(1.0, 0.7).setFill()
        glint.fill()

        # Eye outline
        AppKit.NSColor.colorWithWhite_alpha_(0.35, 0.9).setStroke()
        sclera.setLineWidth_(0.8)
        sclera.stroke()


# ---------------------------------------------------------------------------
# SpriteView -- renders SVG avatar as a native NSImage (matches web dashboard exactly)
# ---------------------------------------------------------------------------
def sprite_dims(avatar_name):
    """Return (width, height) for the avatar sprite, preserving SVG aspect ratio."""
    cfg = AVATAR_EYES.get(avatar_name, AVATAR_EYES.get("clippy", {}))
    vb_w = cfg.get("vb_w", 48)
    vb_h = cfg.get("vb_h", 48)
    aspect = vb_h / vb_w
    w = SPRITE_W
    h = int(w * aspect)
    return w, h


class SpriteView(AppKit.NSView):
    _image = None
    _avatar_name = "clippy"

    def setAvatar_(self, name):
        self._avatar_name = name
        svg_str = AVATAR_SVGS.get(name, AVATAR_SVGS["clippy"])
        svg_data = Foundation.NSData.dataWithBytes_length_(
            svg_str.encode("utf-8"), len(svg_str.encode("utf-8"))
        )
        self._image = AppKit.NSImage.alloc().initWithData_(svg_data)
        w, h = sprite_dims(name)
        if self._image:
            self._image.setSize_(Foundation.NSMakeSize(w, h))
        self.setFrame_(Foundation.NSMakeRect(
            self.frame().origin.x, self.frame().origin.y, w, h
        ))
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        if self._image:
            self._image.drawInRect_fromRect_operation_fraction_(
                self.bounds(),
                Foundation.NSZeroRect,
                AppKit.NSCompositingOperationSourceOver,
                1.0,
            )


# ---------------------------------------------------------------------------
# Bubble colors per avatar
# ---------------------------------------------------------------------------
BUBBLE_COLORS = {
    "clippy": {
        "fill": (1.0, 1.0, 0.8, 1.0),    # classic Windows yellow #FFFFCC
        "stroke": (0.83, 0.81, 0.38, 1.0), # gold border #D4CF60
    },
    "nexus": {
        "fill": (1.0, 1.0, 1.0, 1.0),     # white
        "stroke": (0.75, 0.75, 0.75, 1.0), # light grey
    },
    "zelthor": {
        "fill": (0.85, 1.0, 0.85, 1.0),   # pale green
        "stroke": (0.4, 0.8, 0.4, 1.0),    # green border
    },
}

# ---------------------------------------------------------------------------
# BubbleView -- rounded rect with a left-pointing tail
# ---------------------------------------------------------------------------
class BubbleView(AppKit.NSView):
    _avatar_name = "clippy"

    def drawRect_(self, rect):
        colors = BUBBLE_COLORS.get(self._avatar_name, BUBBLE_COLORS["nexus"])
        fill_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*colors["fill"])
        stroke_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*colors["stroke"])

        bounds = self.bounds()
        # Main bubble body (inset to leave room for the tail on the left)
        body = Foundation.NSMakeRect(
            TAIL_SIZE, 0,
            bounds.size.width - TAIL_SIZE,
            bounds.size.height,
        )
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            body, 8, 8
        )
        fill_color.setFill()
        path.fill()
        stroke_color.setStroke()
        path.setLineWidth_(1.0)
        path.stroke()

        # Left-pointing tail triangle (vertically centred)
        mid_y = bounds.size.height / 2
        tail = AppKit.NSBezierPath.bezierPath()
        tail.moveToPoint_(Foundation.NSMakePoint(0, mid_y))
        tail.lineToPoint_(Foundation.NSMakePoint(TAIL_SIZE, mid_y + TAIL_SIZE))
        tail.lineToPoint_(Foundation.NSMakePoint(TAIL_SIZE, mid_y - TAIL_SIZE))
        tail.closePath()
        fill_color.setFill()
        tail.fill()


# ---------------------------------------------------------------------------
# OverlayController -- manages the floating window, text streaming, and TTS
# ---------------------------------------------------------------------------
class OverlayController(AppKit.NSObject):
    def init(self):
        self = objc.super(OverlayController, self).init()
        if self is None:
            return None
        self.voice = DEFAULT_VOICE
        self.rate = DEFAULT_RATE
        self.audio_device = ""
        self.avatar_name = "clippy"
        self.window = None
        self.sprite_view = None
        self.bubble_view = None
        self.text_field = None
        self.hide_timer = None
        self.stream_timer = None
        self.think_timer = None
        self.eye_left = None
        self.eye_right = None
        self.words = []
        self.word_index = 0
        self.tts_process = None
        self.is_thinking = False
        self._hide_delay = HIDE_DELAY
        self._build_window()
        return self

    # -- window setup -------------------------------------------------------
    def _build_window(self):
        """Create the borderless transparent floating window."""
        screen = AppKit.NSScreen.mainScreen().frame()

        # Sprite dimensions (preserves SVG aspect ratio)
        sp_w, sp_h = sprite_dims(self.avatar_name)

        # Window size: sprite + gap + bubble
        gap = 8
        win_w = sp_w + gap + BUBBLE_WIDTH + TAIL_SIZE
        win_h = sp_h + 20  # a bit of vertical breathing room
        origin_x = (screen.size.width - win_w) / 2  # bottom-center
        origin_y = 80

        frame = Foundation.NSMakeRect(origin_x, origin_y, win_w, win_h)
        mask = AppKit.NSBorderlessWindowMask
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, mask, AppKit.NSBackingStoreBuffered, False
        )
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setMovableByWindowBackground_(True)
        self.window.setHasShadow_(False)

        content = self.window.contentView()

        # Avatar sprite (bottom-left of content)
        sprite = SpriteView.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, sp_w, sp_h)
        )
        sprite.setAvatar_(self.avatar_name)
        content.addSubview_(sprite)
        self.sprite_view = sprite

        # Bloodshot eye overlays (positioned per avatar)
        self.eye_left = BloodshotEyeView.alloc().initWithFrame_(Foundation.NSZeroRect)
        self.eye_right = BloodshotEyeView.alloc().initWithFrame_(Foundation.NSZeroRect)
        self.sprite_view.addSubview_(self.eye_left)
        self.sprite_view.addSubview_(self.eye_right)
        self._position_eyes()
        self.eye_left.setHidden_(True)
        self.eye_right.setHidden_(True)

        # Bubble view
        bubble_x = sp_w + gap
        bubble_h = sp_h
        self.bubble_view = BubbleView.alloc().initWithFrame_(
            Foundation.NSMakeRect(bubble_x, 0, BUBBLE_WIDTH + TAIL_SIZE, bubble_h)
        )
        self.bubble_view._avatar_name = self.avatar_name
        content.addSubview_(self.bubble_view)

        # Scroll view inside bubble (auto-scrolls long text)
        inner_w = BUBBLE_WIDTH - BUBBLE_PADDING * 2
        inner_h = bubble_h - BUBBLE_PADDING * 2
        sv = AppKit.NSScrollView.alloc().initWithFrame_(
            Foundation.NSMakeRect(TAIL_SIZE + BUBBLE_PADDING, BUBBLE_PADDING, inner_w, inner_h)
        )
        sv.setHasVerticalScroller_(False)
        sv.setHasHorizontalScroller_(False)
        sv.setBorderType_(AppKit.NSNoBorder)
        sv.setDrawsBackground_(False)
        sv.setVerticalScrollElasticity_(1)  # NSScrollElasticityAutomatic
        sv.setScrollerStyle_(1)  # NSScrollerStyleOverlay
        # Force-hide any scroller knob
        sv.setScrollerKnobStyle_(1)  # NSScrollerKnobStyleDark (least visible)
        self.bubble_view.addSubview_(sv)
        self.scroll_view = sv

        # Text field inside scroll view
        tf = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(0, 0, inner_w, inner_h)
        )
        tf.setEditable_(False)
        tf.setSelectable_(False)
        tf.setBordered_(False)
        tf.setDrawsBackground_(False)
        tf.setFont_(AppKit.NSFont.systemFontOfSize_(13))
        tf.setTextColor_(AppKit.NSColor.blackColor())
        tf.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        tf.cell().setWraps_(True)
        tf.cell().setScrollable_(False)
        tf.setStringValue_("")
        sv.setDocumentView_(tf)
        self.text_field = tf

    def _position_eyes(self):
        """Position eye overlays based on current avatar's SVG eye positions."""
        cfg = AVATAR_EYES.get(self.avatar_name, AVATAR_EYES.get("clippy"))
        if not cfg:
            return
        sp_w, sp_h = sprite_dims(self.avatar_name)
        scale_x = sp_w / cfg["vb_w"]
        scale_y = sp_h / cfg["vb_h"]
        scale_r = min(scale_x, scale_y)
        for eye_view, key in [(self.eye_left, "left"), (self.eye_right, "right")]:
            cx, cy, r = cfg[key]
            view_cx = cx * scale_x
            view_cy = sp_h - (cy * scale_y)  # flip Y for NSView
            size = r * scale_r * 2 + 2
            eye_view.setFrame_(Foundation.NSMakeRect(
                view_cx - size / 2, view_cy - size / 2, size, size
            ))

    # -- public interface ---------------------------------------------------
    def switch_avatar(self, name):
        """Switch the displayed avatar sprite."""
        self.avatar_name = name
        self.sprite_view.setAvatar_(name)
        self._position_eyes()
        # Update bubble color for new avatar
        self.bubble_view._avatar_name = name
        self.bubble_view.setNeedsDisplay_(True)

    def show_thinking(self):
        """Show avatar with darting bloodshot eyes, no bubble. Called on 'thinking' event."""
        self._cancel_timers()
        self.is_thinking = True
        # Play wake/thinking sound
        play_sound(SOUND_WAKE)
        # Show avatar window immediately, hide bubble
        self.bubble_view.setHidden_(True)
        self.text_field.setStringValue_("")
        self.window.orderFront_(None)
        # Show bloodshot eyes and start darting animation
        self.eye_left.setHidden_(False)
        self.eye_right.setHidden_(False)
        self.think_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.35, self, "thinkTick:", None, True
        )

    def thinkTick_(self, timer):
        """NSTimer callback: dart the eyes to a random position."""
        cfg = AVATAR_EYES.get(self.avatar_name, AVATAR_EYES.get("clippy"))
        sp_w, sp_h = sprite_dims(self.avatar_name)
        scale = min(sp_w / cfg["vb_w"], sp_h / cfg["vb_h"])
        r = cfg["left"][2] * scale * 0.3  # max offset = 30% of eye radius in view

        # Both eyes look in roughly the same direction
        dx = random.uniform(-r, r)
        dy = random.uniform(-r, r)
        # Slight independent wobble
        self.eye_left.setPupilOffset_dy_(dx + random.uniform(-1, 1), dy + random.uniform(-1, 1))
        self.eye_right.setPupilOffset_dy_(dx + random.uniform(-1, 1), dy + random.uniform(-1, 1))

    def _clean_for_display(self, text):
        """Strip markdown/code for the speech bubble (plain text only)."""
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'`[^`]+`', '', text)
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{2,}', '. ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def show_message(self, text, voice_on=False, speak=False):
        """Display the overlay and stream words visually. TTS is handled by the server
        unless speak=True, in which case the overlay speaks it directly."""
        self._cancel_timers()
        self.is_thinking = False
        # Play listening/response sound
        play_sound(SOUND_LISTEN)
        # Hide bloodshot eyes, show bubble
        self.eye_left.setHidden_(True)
        self.eye_right.setHidden_(True)
        self.bubble_view.setHidden_(False)
        text = self._clean_for_display(text)
        self.words = text.split()
        self.word_index = 0
        self.text_field.setStringValue_("")
        # Start bubble small, will grow as text streams
        self._resize_bubble("")
        self.window.orderFront_(None)

        # Speak directly (used for boot greeting)
        if speak:
            self.speak_text(text)

        # Calculate word interval from TTS speech rate
        # macOS `say` rate is ~words per minute but varies by word length.
        # Scale: rate 200 -> ~0.12s/word, rate 100 -> ~0.24s/word, rate 300 -> ~0.08s/word
        word_interval = max(0.04, 24.0 / max(self.rate, 80))

        # Calculate hide delay: if voice is on, keep overlay visible for full TTS duration
        word_count = len(self.words)
        if voice_on and word_count > 0:
            tts_duration = word_count * 60.0 / max(self.rate, 80)
            stream_duration = word_count * word_interval
            # Stay visible until TTS finishes + generous buffer
            self._hide_delay = max(5.0, tts_duration - stream_duration + 4.0)
        else:
            self._hide_delay = HIDE_DELAY

        # Start word-by-word streaming synced to TTS rate
        self.stream_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            word_interval, self, "streamTick:", None, True
        )

    def streamTick_(self, timer):
        """NSTimer callback: append next word to the text field."""
        if self.word_index < len(self.words):
            current = self.text_field.stringValue()
            sep = " " if current else ""
            new_text = current + sep + self.words[self.word_index]
            self.text_field.setStringValue_(new_text)
            self._resize_bubble(new_text)
            self.word_index += 1
        else:
            timer.invalidate()
            self.stream_timer = None
            # Poll TTS process -- don't hide until speech is actually done
            self._start_tts_monitor()

    def _start_tts_monitor(self):
        """Poll TTS process every 0.3s. Hide overlay only after speech finishes."""
        self._tts_monitor = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "ttsMonitorTick:", None, True
        )

    def ttsMonitorTick_(self, timer):
        """Check if TTS is still running. Hide when done."""
        if self.tts_process and self.tts_process.poll() is None:
            return  # still speaking -- keep waiting
        timer.invalidate()
        self._tts_monitor = None
        # TTS done (or no TTS) -- linger so user can read the bubble
        self.hide_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "hideTick:", None, False
        )

    def _resize_bubble(self, text):
        """Resize the bubble and text field to fit the current text content."""
        sp_w, sp_h = sprite_dims(self.avatar_name)
        gap = 8
        text_w = BUBBLE_WIDTH - BUBBLE_PADDING * 2

        if not text:
            # Minimum bubble: small empty state
            bubble_h = 44
        else:
            # Measure text height using NSString's boundingRectWithSize
            attrs = {AppKit.NSFontAttributeName: self.text_field.font()}
            ns_str = Foundation.NSString.stringWithString_(text)
            max_size = Foundation.NSMakeSize(text_w, 999)
            bounding = ns_str.boundingRectWithSize_options_attributes_(
                max_size,
                AppKit.NSStringDrawingUsesLineFragmentOrigin,
                attrs,
            )
            text_h = bounding.size.height + 4  # small buffer
            bubble_h = max(44, text_h + BUBBLE_PADDING * 2)

        # Cap bubble height -- beyond this, scroll kicks in
        max_bubble_h = 240
        needs_scroll = bubble_h > max_bubble_h
        bubble_h = min(bubble_h, max_bubble_h)

        # Update bubble frame (anchored at bottom, next to sprite)
        bubble_x = sp_w + gap
        self.bubble_view.setFrame_(Foundation.NSMakeRect(
            bubble_x, 0, BUBBLE_WIDTH + TAIL_SIZE, bubble_h
        ))

        # Update scroll view inside bubble
        inner_w = BUBBLE_WIDTH - BUBBLE_PADDING * 2
        inner_h = bubble_h - BUBBLE_PADDING * 2
        self.scroll_view.setFrame_(Foundation.NSMakeRect(
            TAIL_SIZE + BUBBLE_PADDING, BUBBLE_PADDING, inner_w, inner_h,
        ))
        # Hide scrollbar but keep scrolling enabled
        self.scroll_view.setHasVerticalScroller_(False)

        # Update text field to full content height (scrolls inside scroll view)
        if not text:
            tf_h = inner_h
        else:
            tf_h = max(inner_h, bounding.size.height + 4)
        self.text_field.setFrame_(Foundation.NSMakeRect(0, 0, inner_w, tf_h))

        # Auto-scroll to show latest text at bottom
        if needs_scroll:
            # In non-flipped NSView, y=0 is the bottom of the text (newest content)
            clip = self.scroll_view.contentView()
            scroll_y = max(0, tf_h - inner_h)
            clip.scrollToPoint_(Foundation.NSMakePoint(0, scroll_y))
            self.scroll_view.reflectScrolledClipView_(clip)
        self.bubble_view.setNeedsDisplay_(True)

    def hideTick_(self, timer):
        """NSTimer callback: hide the overlay after the delay."""
        self.window.orderOut_(None)
        self.hide_timer = None

    def speak_text(self, text):
        """Run macOS `say` in a subprocess."""
        try:
            if self.tts_process and self.tts_process.poll() is None:
                self.tts_process.terminate()
            cmd = ["say", "-v", self.voice, "-r", str(self.rate)]
            if self.audio_device:
                cmd.extend(["-a", self.audio_device])
            cmd.append(text)
            self.tts_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[overlay] TTS error: {e}")

    def _cancel_timers(self):
        if self.stream_timer:
            self.stream_timer.invalidate()
            self.stream_timer = None
        if self.hide_timer:
            self.hide_timer.invalidate()
            self.hide_timer = None
        if self.think_timer:
            self.think_timer.invalidate()
            self.think_timer = None
        if getattr(self, '_tts_monitor', None):
            self._tts_monitor.invalidate()
            self._tts_monitor = None


# ---------------------------------------------------------------------------
# Wake word listener -- records audio via ffmpeg, transcribes with whisper-cli
# ---------------------------------------------------------------------------
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL",
    os.path.expanduser("~/.local/share/whisper-cpp/ggml-base.en.bin"),
)
WAKE_HALLUCINATIONS = {
    "you", "thank you.", "thanks for watching.", ".", "",
    "thanks.", "bye.", "goodbye.", "thank you for watching.",
    "the end.", "so,", "i'm sorry.", "pause", "blank audio",
    "silence", "coughing", "music", "applause", "laughter",
    "breathing", "sighing", "inaudible", "noise",
}
IDLE_CHUNK = 3       # seconds per chunk in idle mode
CONVO_CHUNK = 5      # seconds per chunk in conversation mode
SILENCE_EXIT = 2     # consecutive silences before exiting conversation


class WakeWordListener:
    """Records mic audio via ffmpeg, transcribes with whisper-cli,
    detects wake words, captures follow-up queries, and sends them
    via a callback. Runs in its own thread."""

    def __init__(self, delegate):
        self.delegate = delegate     # AppDelegate for menubar state
        self.wake_aliases = []       # set by WebSocket init
        self.wake_word = "nexus"
        self.muted = False           # True while TTS is playing
        self.running = True
        self.in_conversation = False
        self._silence_count = 0
        self._send_callback = None   # async fn(text) set by WebSocketClient
        self._tmp_dir = tempfile.mkdtemp(prefix="nexus-wake-")
        self._mic_device = None

    def set_send_callback(self, cb):
        self._send_callback = cb

    def set_wake_config(self, wake_word, aliases):
        self.wake_word = wake_word
        self.wake_aliases = [a.lower() for a in aliases]

    def mute(self, duration=0):
        """Mute mic for duration seconds (0 = mute until unmute() called)."""
        self.muted = True
        if duration > 0:
            threading.Timer(duration, self.unmute).start()

    def unmute(self):
        self.muted = False

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def _run(self):
        # Small delay for WS to connect and get init data
        time.sleep(2)
        print("[wake] Wake word listener started")
        self._find_mic()

        while self.running:
            if self.muted:
                time.sleep(0.3)
                continue

            chunk = CONVO_CHUNK if self.in_conversation else IDLE_CHUNK
            text = self._listen(chunk)

            if text is None:
                if self.in_conversation:
                    self._silence_count += 1
                    if self._silence_count >= SILENCE_EXIT:
                        print("[wake] Conversation ended (silence)")
                        self.in_conversation = False
                        self._silence_count = 0
                        self._set_state("idle")
                continue

            self._silence_count = 0
            text_lower = text.lower().strip()

            if not self.in_conversation:
                # Check for wake word
                if self._contains_wake_word(text_lower):
                    print(f"[wake] Wake word detected: {text_lower}")
                    # Extract query after wake word, if any
                    query = self._strip_wake_word(text_lower)
                    if query and len(query) > 3:
                        # Wake word + query in same chunk
                        self._send_query(query)
                        self.in_conversation = True
                    else:
                        # Just wake word -- listen for the actual query
                        self.in_conversation = True
                        self._set_state("listening")
                        print("[wake] Listening for query...")
                        follow_up = self._listen(CONVO_CHUNK)
                        if follow_up:
                            self._send_query(follow_up)
                        else:
                            # They said the wake word but nothing after
                            self._set_state("idle")
            else:
                # In conversation mode -- everything is a query
                self._send_query(text)

    def _contains_wake_word(self, text_lower):
        for alias in self.wake_aliases:
            if alias in text_lower:
                return True
        return False

    def _strip_wake_word(self, text_lower):
        """Remove the wake word from the beginning of the text."""
        for alias in sorted(self.wake_aliases, key=len, reverse=True):
            if text_lower.startswith(alias):
                rest = text_lower[len(alias):].strip().lstrip(',').strip()
                return rest
        return ""

    def set_event_loop(self, loop):
        """Set the asyncio event loop used by the WebSocket thread."""
        self._loop = loop

    def _send_query(self, text):
        """Send query via the callback (called from listener thread)."""
        if self._send_callback and text:
            self._set_state("listening")
            print(f"[wake] Sending query: {text[:60]}...")
            loop = getattr(self, '_loop', None)
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(self._send_callback(text), loop)
            else:
                print("[wake] No event loop available, can't send query")

    def _set_state(self, state):
        self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "setMenubarStateFromThread:", state, False
        )

    def _find_mic(self):
        """Detect mic device index for ffmpeg avfoundation."""
        try:
            result = subprocess.run(
                ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=10,
            )
            output = result.stderr
            for line in output.splitlines():
                lower = line.lower()
                if any(kw in lower for kw in ["microphone", "built-in", "macbook", "maono"]):
                    match = re.search(r'\[(\d+)\]', line)
                    if match:
                        self._mic_device = match.group(1)
                        print(f"[wake] Found mic device: {self._mic_device}")
                        return
            # Fallback: first audio device
            in_audio = False
            for line in output.splitlines():
                if "audio devices" in line.lower():
                    in_audio = True
                    continue
                if in_audio:
                    match = re.search(r'\[(\d+)\]', line)
                    if match:
                        self._mic_device = match.group(1)
                        print(f"[wake] Fallback mic device: {self._mic_device}")
                        return
        except Exception as e:
            print(f"[wake] Mic detection error: {e}")
        self._mic_device = "0"

    def _listen(self, duration):
        """Record audio and transcribe. Returns text or None."""
        if self.muted:
            return None

        audio_file = os.path.join(self._tmp_dir, f"wake_{os.getpid()}.wav")

        # Record
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "quiet",
                    "-f", "avfoundation",
                    "-i", f":{self._mic_device}",
                    "-t", str(duration),
                    "-ar", "16000", "-ac", "1",
                    audio_file,
                ],
                capture_output=True, timeout=duration + 10,
            )
        except subprocess.TimeoutExpired:
            self._cleanup(audio_file)
            return None

        if not os.path.isfile(audio_file):
            return None

        # Transcribe
        try:
            result = subprocess.run(
                [
                    "whisper-cli",
                    "-m", WHISPER_MODEL,
                    "-f", audio_file,
                    "-l", "en",
                    "-nt",
                ],
                capture_output=True, text=True, timeout=10,
            )
            raw = result.stdout.strip() if result.stdout else None
        except subprocess.TimeoutExpired:
            self._cleanup(audio_file)
            return None

        self._cleanup(audio_file)

        if not raw or len(raw) < 2:
            return None

        # Filter hallucinations
        cleaned = raw.lower().strip().rstrip('.').strip()
        if cleaned in WAKE_HALLUCINATIONS:
            return None
        if re.match(r'^[\[\(\*].*[\]\)\*]$', raw.strip()):
            return None
        if len(cleaned) < 3:
            return None

        return raw.strip()

    def _cleanup(self, *files):
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
# WebSocket client (runs in a background thread)
# ---------------------------------------------------------------------------
class WebSocketClient:
    def __init__(self, delegate):
        self.delegate = delegate          # AppDelegate (NSObject) for ObjC selectors
        self.controller = delegate.controller
        self.running = True
        self.wake_listener = WakeWordListener(delegate)
        self._ws = None                   # active WebSocket connection
        self._loop = None                 # event loop for the WS thread

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        # Wake listener disabled -- Python.app can't get mic permission via TCC.
        # Wake word detection runs in the browser via MediaRecorder API instead.
        # self.wake_listener.start()

    def _run(self):
        """Async event loop for the WebSocket connection with reconnection."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.wake_listener.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        while self.running:
            try:
                await self._connect()
            except Exception as e:
                print(f"[overlay] WebSocket error: {e}")
            if self.running:
                print(f"[overlay] Reconnecting in {RECONNECT_DELAY}s...")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _send_chat(self, text):
        """Send a chat message via WebSocket (called from wake listener thread)."""
        if self._ws:
            await self._ws.send(json.dumps({
                "type": "chat",
                "content": text,
                "voice": True,
            }))

    async def _connect(self):
        async with websockets.connect(WS_URL) as ws:
            self._ws = ws
            print("[overlay] Connected to WebSocket")
            # Announce ourselves so the server skips its own TTS
            await ws.send(json.dumps({"type": "overlay_connect"}))

            # Wire up wake listener's send callback
            self.wake_listener.set_send_callback(self._send_chat)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")

                # Server init -- pick up voice config + avatar + wake word
                if msg_type == "init":
                    settings = msg.get("settings", {})
                    self.controller.voice = settings.get("voice", DEFAULT_VOICE)
                    self.controller.rate = settings.get("rate", DEFAULT_RATE)
                    self.controller.audio_device = settings.get("audio_device", "")
                    avatar = msg.get("avatar", "clippy")
                    self._switch_avatar_on_main(avatar)
                    # Configure wake word listener
                    wake_word = msg.get("wake_word", "nexus")
                    wake_aliases = msg.get("wake_aliases", [wake_word])
                    self.wake_listener.set_wake_config(wake_word, wake_aliases)
                    print(f"[wake] Wake word: {wake_word}, aliases: {wake_aliases}")
                    # Show greeting on launch with TTS so clippy talks on boot
                    greeting = msg.get("greeting", "I'm here!")
                    payload = json.dumps({"text": greeting, "voice": True, "speak": True})
                    self._show_on_main_thread(payload)
                    # Mute wake listener during boot greeting TTS
                    words = len(greeting.split())
                    self.wake_listener.mute(max(2, words * 0.4))
                    print(f"[overlay] Avatar: {avatar}, voice: {self.controller.voice}, rate: {self.controller.rate}")
                    print(f"[overlay] Boot greeting: {greeting}")

                # Avatar switched -- update sprite + voice + wake word
                elif msg_type == "avatar_switched":
                    avatar = msg.get("avatar", "clippy")
                    settings = msg.get("settings", {})
                    self.controller.voice = settings.get("voice", DEFAULT_VOICE)
                    self.controller.rate = settings.get("rate", DEFAULT_RATE)
                    self._switch_avatar_on_main(avatar)
                    # Update wake word config
                    wake_word = msg.get("wake_word", avatar)
                    wake_aliases = msg.get("wake_aliases", [wake_word])
                    self.wake_listener.set_wake_config(wake_word, wake_aliases)
                    self.wake_listener.in_conversation = False
                    print(f"[overlay] Switched to: {avatar}, wake: {wake_word}")

                # Thinking -- pop up avatar with darting eyes
                elif msg_type == "thinking":
                    self._thinking_on_main()
                    self._set_menubar_state("thinking")

                # Assistant message -- show on screen + mute mic during TTS
                elif msg_type == "message" and msg.get("role") == "assistant":
                    text = msg.get("content", "").strip()
                    if text:
                        voice_on = msg.get("voice", False)
                        # Overlay handles its own TTS so it can track when speech ends
                        payload = json.dumps({"text": text, "voice": voice_on, "speak": voice_on})
                        self._show_on_main_thread(payload)
                        if voice_on:
                            self._set_menubar_state("speaking")
                            # Mute wake listener during TTS to prevent self-hearing
                            words = len(text.split())
                            delay = max(2, words * 0.35)
                            self.wake_listener.mute(delay + 1)
                            threading.Timer(delay, lambda: self._set_menubar_state("idle")).start()
                        else:
                            self._set_menubar_state("idle")

                # User message -- show recording state briefly
                elif msg_type == "message" and msg.get("role") == "user":
                    self._set_menubar_state("listening")

    def _show_on_main_thread(self, text):
        """Thread-safe: schedule UI work on the main thread."""
        self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showMessageFromThread:", text, False
        )

    def _thinking_on_main(self):
        """Thread-safe: show thinking animation on main thread."""
        self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "showThinkingFromThread:", None, False
        )

    def _switch_avatar_on_main(self, name):
        """Thread-safe: switch avatar on the main thread."""
        self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "switchAvatarFromThread:", name, False
        )

    def _set_menubar_state(self, state):
        """Thread-safe: update menubar icon state."""
        self.delegate.performSelectorOnMainThread_withObject_waitUntilDone_(
            "setMenubarStateFromThread:", state, False
        )

    def stop(self):
        self.running = False
        self.wake_listener.stop()


# ---------------------------------------------------------------------------
# App delegate wrapping the controller for ObjC selectors
# ---------------------------------------------------------------------------
class AppDelegate(AppKit.NSObject):
    def init(self):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.controller = OverlayController.alloc().init()
        self.ws_client = None
        self.status_item = None
        self._listening_state = "idle"  # idle, listening, speaking, thinking
        return self

    def applicationDidFinishLaunching_(self, notification):
        self.ws_client = WebSocketClient(self)
        self.ws_client.start()

    def applicationWillTerminate_(self, notification):
        # Remove menubar item so it doesn't ghost after kill
        if self.status_item:
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.status_item)
            self.status_item = None
        self.ws_client.stop()

    # ── Menubar status item ──

    def _setup_menubar(self):
        """Create a menubar icon with state indicator."""
        status_bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = status_bar.statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        self._update_menubar_icon("idle")

        # Build menu
        menu = AppKit.NSMenu.alloc().init()

        listening_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Listening...", None, ""
        )
        listening_item.setEnabled_(False)
        listening_item.setTag_(100)
        menu.addItem_(listening_item)

        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Nexus Runner", "terminate:", "q"
        )
        menu.addItem_(quit_item)

        self.status_item.setMenu_(menu)

    def _update_menubar_icon(self, state):
        """Update menubar icon color based on state -- simple colored dot."""
        self._listening_state = state
        colors = {
            "idle": AppKit.NSColor.systemGreenColor(),       # green while waiting
            "listening": AppKit.NSColor.systemRedColor(),     # red while recording
            "speaking": AppKit.NSColor.systemRedColor(),      # red while speaking
            "thinking": AppKit.NSColor.systemBlueColor(),     # blue while thinking
        }
        color = colors.get(state, AppKit.NSColor.systemGrayColor())

        # Draw a filled circle as the menubar icon
        size = Foundation.NSMakeSize(18, 18)
        img = AppKit.NSImage.alloc().initWithSize_(size)
        img.lockFocus()
        color.setFill()
        dot_rect = Foundation.NSMakeRect(4, 4, 10, 10)
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(dot_rect).fill()
        img.unlockFocus()
        img.setTemplate_(False)

        button = self.status_item.button()
        if button:
            button.setImage_(img)
            button.setTitle_("")

        # Update menu status text
        if self.status_item and self.status_item.menu():
            item = self.status_item.menu().itemWithTag_(100)
            if item:
                labels = {
                    "idle": "Idle",
                    "listening": "Listening for wake word...",
                    "speaking": "Speaking...",
                    "thinking": "Thinking...",
                }
                item.setTitle_(labels.get(state, "Idle"))

    def setMenubarStateFromThread_(self, state):
        """Thread-safe menubar state update."""
        self._update_menubar_icon(state)

    # Called from the WS thread via performSelectorOnMainThread
    def showMessageFromThread_(self, payload_str):
        try:
            data = json.loads(payload_str)
            self.controller.show_message(
                data["text"],
                voice_on=data.get("voice", False),
                speak=data.get("speak", False),
            )
        except (json.JSONDecodeError, KeyError):
            # Fallback for plain text (e.g. greeting)
            self.controller.show_message(payload_str)

    def showThinkingFromThread_(self, _):
        self.controller.show_thinking()

    def switchAvatarFromThread_(self, name):
        self.controller.switch_avatar(name)


# Expose ObjC selectors used by performSelectorOnMainThread
AppDelegate.showMessageFromThread_ = AppDelegate.showMessageFromThread_
AppDelegate.showThinkingFromThread_ = AppDelegate.showThinkingFromThread_
AppDelegate.switchAvatarFromThread_ = AppDelegate.switchAvatarFromThread_
AppDelegate.setMenubarStateFromThread_ = AppDelegate.setMenubarStateFromThread_


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _acquire_lock():
    """Acquire exclusive file lock. Exit immediately if another overlay is running."""
    global _lock_fh
    _lock_fh = open("/tmp/nexus-overlay.lock", "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except BlockingIOError:
        print("[overlay] Another overlay is already running. Exiting.")
        sys.exit(0)


def main():
    _acquire_lock()
    app = AppKit.NSApplication.sharedApplication()
    # No dock icon -- behave as an accessory
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    # Clean shutdown on SIGINT/SIGTERM -- remove menubar item before exit
    def _shutdown(*_):
        if delegate.status_item:
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(delegate.status_item)
            delegate.status_item = None
        AppHelper.stopEventLoop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("[overlay] Avatar overlay running. Ctrl-C to quit.")
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
