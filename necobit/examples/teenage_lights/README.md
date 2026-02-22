# Light Engineering

MIDI-reactive visualizer for **M5Stack Basic Core** + **Necobit MIDI Module 2 (-1)** + **WS2812B LED panels**.

Receives MIDI from a **Teenage Engineering KO II** (or any DIN MIDI source) and drives both an onscreen light show and a 32xN addressable LED panel in real time.

---

## Features

### 11 Screen Visualizations

| Mode | Description |
|------|-------------|
| SPECTRUM | Per-note lane bars with chromatic colors and triangle peaks |
| WATERFALL | Scrolling piano roll — time on X, notes on Y |
| NOTE RAIN | Colored drops fall per note, speed from velocity |
| PULSE | Expanding concentric rings on each hit |
| PLASMA | Layered sine-wave interference patterns |
| TUNNEL | Concentric squares in purple-pink-blue receding into depth |
| DEEP SPACE | Stars, planets with rings, UFOs with tractor beams, nebula glow |
| ACID | Six-layer psychedelic distortion |
| CHLADNI | Vibrating plate nodal line patterns |
| GEOMETRY | Rotating triangles and geometric wireframes |
| OSCILLOSCOPE | Real-time waveform trace |

All modes share a dynamic starfield background that accelerates with MIDI energy.

### 37 LED Panel Effects

| # | Name | Style |
|---|------|-------|
| 1 | RAINBOW | Scrolling hue plasma |
| 2 | VU METER | Per-column level towers |
| 3 | COMET | Scanning bar sweeps across panel |
| 4 | SPARKLE | Kaleidoscope burst patterns |
| 5 | FIRE | 2D rising flames |
| 6 | STROBE | Full-panel pulse flash |
| 7 | OCEAN | Mirrored horizontal wave |
| 8 | METEOR | Falling green matrix rain |
| 9 | SCREEN SYNC | Rotating spiral |
| 10 | NEON PULSE | Heartbeat center flash |
| 11 | CYBER RAIN | Note-triggered falling drops |
| 12 | SYNTHWAVE | Geometric shape bursts |
| 13 | LAVA LAMP | Conway's Game of Life — MIDI-seeded |
| 14 | AURORA | Chladni nodal line patterns |
| 15 | BREATHE | Diamond expanding from center |
| 16 | GLITCH | Crosshatch interference grid |
| 17 | LIGHTNING | Left-right mirror symmetry |
| 18 | SLICER | Horizontal spectrum bands |
| 19 | NOTE MAP | Chromatic note grid — octave vs pitch |
| 20 | NOTE SPLASH | Ripple rings from note hits |
| 21 | NOTE STACK | Hex grid cellular automata |
| 22 | NOTE PIANO | Chromatic note grid (alt layout) |
| 23 | COMET TRAIL | Bright comet orbits the panel edges |
| 24 | RAIN DROP | Columns fill from top on hits, drain down |
| 25 | STARFIELD | Particles explode outward from center |
| 26 | WAVE | Scrolling sine wave, amplitude from MIDI |
| 27 | FIREWORKS | 12-particle bursts with gravity |
| 28 | BOUNCE | Bouncing balls spawned by notes |
| 29 | SNAKE | Growing snake, turns on note hits |
| 30 | WATERFALL2D | Top-row spectrogram cascading down |
| 31 | RADAR | Rotating sweep with MIDI blips |
| 32 | TETRIS | Falling blocks stack on note hits |
| 33 | CYBERPUNK | Pink/purple/blue plasma — no other colors |
| 34 | CHLADNI CYB | Chladni patterns in cyberpunk palette |
| 35 | CHLADNI MRP | Morphing Chladni — harmonics shift per note |
| 36 | NOTE BARS | Per-note vertical bars with peak caps |
| 37 | SINE WAVE | Green sine wave, amplitude from MIDI |

All effects are strictly MIDI-reactive and go dark when idle.

### Auto-Transition Mode

Double-click **Button B** to toggle beat-reactive effect chaining. The system detects beats in real time and switches presets on the hit — picking from energetic, mid, or calm pools based on current dynamics. Manual control (A/C) instantly overrides.

---

## Controls

| Input | Action |
|-------|--------|
| **Button A** | Previous LED preset |
| **Button B** (single click) | Next screen visualization |
| **Button B** (double click) | Toggle auto-transition mode |
| **Button C** | Next LED preset |
| **Hold B + A** | Brightness down |
| **Hold B + C** | Brightness up |

---

## Hardware

### What You Need

- [M5Stack Basic Core](https://m5stack.com)
- [Necobit MIDI Module 2 (-1)](https://necobit.com/denshi/m5-midi-module2/)
- WS2812B LED panel (32x8 recommended, chain up to 3 for 32x24)
- 5V power supply for the LEDs
- DIN MIDI cable from your instrument

### Wiring

| Connection | Pin |
|-----------|-----|
| MIDI RX | GPIO 16 (via Necobit stacking bus) |
| MIDI TX | GPIO 17 (via Necobit stacking bus) |
| LED Data | GPIO 26 (Grove Port B, yellow wire) |

The panel uses **column-major serpentine wiring** — 8-LED vertical strips chained left to right. Even columns run top-to-bottom, odd columns bottom-to-top.

### Configuration

Set `NUM_LEDS` at the top of the sketch to match your total pixel count:

| Panels | NUM_LEDS | Dimensions |
|--------|----------|------------|
| 1 | 256 | 32 x 8 |
| 2 | 512 | 32 x 16 |
| 3 | 768 | 32 x 24 |

Panel height auto-derives from `NUM_LEDS / 32`.

---

## Build

### Dependencies

- [M5Unified](https://github.com/m5stack/M5Unified)
- [FastLED](https://github.com/FastLED/FastLED)

Install via Arduino Library Manager or:

```bash
arduino-cli lib install M5Unified FastLED
```

### Compile & Flash

```bash
arduino-cli compile --fqbn m5stack:esp32:m5stack_core teenage_lights.ino
arduino-cli upload -p /dev/YOUR_PORT --fqbn m5stack:esp32:m5stack_core teenage_lights.ino
```

---

## Shoutouts

- **[Necobit](https://necobit.com)** — clean DIN MIDI on the M5Stack bus. The Module 2 just works.
- **[M5Stack](https://m5stack.com)** — endlessly hackable little Core with a screen, buttons, and Grove ports.
- **[Teenage Engineering](https://teenage.engineering)** — the KO II that started this whole thing.

---

## License

MIT
