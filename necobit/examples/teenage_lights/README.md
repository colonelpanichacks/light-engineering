# Teenage Lights

MIDI-reactive visualizer for **M5Stack Basic Core** + **Necobit MIDI Module 2 (-1)** + **WS2812B LED panels**.

Receives MIDI from a **Teenage Engineering KO II** (or any DIN MIDI source) and drives both an onscreen light show and a 32xN addressable LED panel in real time.

## Features

### 11 Screen Visualizations
SPECTRUM, WATERFALL, NOTE RAIN, PULSE, PLASMA, TUNNEL, DEEP SPACE, ACID, CHLADNI, GEOMETRY, OSCILLOSCOPE — all with a dynamic starfield background.

### 36 LED Panel Effects
RAINBOW, VU METER, COMET, SPARKLE, FIRE, STROBE, OCEAN, METEOR, SCREEN SYNC, NEON PULSE, CYBER RAIN, SYNTHWAVE, LAVA LAMP, AURORA, BREATHE, GLITCH, LIGHTNING, SLICER, NOTE MAP, NOTE SPLASH, NOTE STACK, NOTE PIANO, COMET TRAIL, RAIN DROP, STARFIELD, WAVE, FIREWORKS, BOUNCE, SNAKE, WATERFALL2D, RADAR, TETRIS, CYBERPUNK, CHLADNI CYB, CHLADNI MRP, NOTE BARS

All effects are MIDI-reactive and go dark when idle.

### Auto-Transition Mode
Double-click Button B to enable beat-reactive effect chaining — the LED preset automatically switches based on real-time beat detection, energy dynamics, and note density.

## Controls

| Input | Action |
|-------|--------|
| Button A | Previous LED preset |
| Button B (single click) | Next screen visualization |
| Button B (double click) | Toggle auto-transition mode |
| Button C | Next LED preset |
| Hold B + A | Brightness down |
| Hold B + C | Brightness up |

## Wiring

- **MIDI**: DIN 5-pin via Necobit stacking bus (GPIO 16 RX / 17 TX)
- **LED Panel**: Grove Port B, data on GPIO 26

Set `NUM_LEDS` to your total pixel count (256 = one 32x8 panel, 512 = two, 768 = three). Panel height auto-configures. External 5V PSU recommended.

## Dependencies

- [M5Unified](https://github.com/m5stack/M5Unified)
- [FastLED](https://github.com/FastLED/FastLED)

Install via Arduino Library Manager or `arduino-cli lib install`.

## Build

```bash
arduino-cli compile --fqbn m5stack:esp32:m5stack_core teenage_lights.ino
arduino-cli upload -p /dev/YOUR_PORT --fqbn m5stack:esp32:m5stack_core teenage_lights.ino
```

## Shoutouts

- **[Necobit](https://necobit.com)** for the MIDI Module 2 — clean DIN MIDI on the M5Stack bus
- **[M5Stack](https://m5stack.com)** for the endlessly hackable Basic Core
- **[Teenage Engineering](https://teenage.engineering)** for the KO II that started this whole thing

## License

MIT
