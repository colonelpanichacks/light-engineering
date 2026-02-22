# Necobit MIDI Module 2 (-1) + M5Stack Basic Core

## Hardware Setup

### M5Stack Basic Core

- **Processor**: ESP32-D0WDQ6-V3, 240MHz dual core, 600 DMIPS
- **SRAM**: 520KB
- **Flash**: 16MB
- **Display**: 2" IPS LCD, 320x240px (ILI9342C)
- **Input**: 3 physical buttons (A, B, C)
- **Speaker**: Built-in, driven by GPIO25 DAC
- **Expansion**: Grove ports, stacking module bus

### Necobit MIDI Module 2 (-1)

- **Product**: M5Stack stacking module (sits underneath the Core)
- **Version**: (-1) — no built-in amplifier or speaker
- **MIDI IN**: DIN 5-pin input
- **MIDI OUT/THRU**: DIN 5-pin output
- **Solder Jumpers**: JP1 + JP3 shorted (factory default for Basic Core)
- **GitHub**: https://github.com/necobit/M5Stack-MIDI-Module
- **Docs**: https://necobit.com/denshi/m5-midi-module2/

## Pin Map

| Function         | GPIO | Notes                              |
|------------------|------|------------------------------------|
| MIDI RX          | 16   | Incoming MIDI data (Serial2 RX)    |
| MIDI TX          | 17   | Outgoing MIDI data (Serial2 TX)    |
| LED Data         | 26   | WS2812B panel, Grove Port B        |
| Internal Speaker | 25   | DAC — silenced at boot             |

## MIDI Serial Config

- **Baud rate**: 31,250 (standard MIDI)
- **Format**: 8N1

```cpp
Serial2.begin(31250, SERIAL_8N1, 16, 17);
```

## Important Notes

- (-1) version has NO onboard speaker or amp
- Port A (G21/G22) has I2C pull-ups — bad for NeoPixels, use Port B (G26) instead
- Port B G26 shares Necobit audio trace to 3.5mm jack, harmless on (-1) version
- `NUM_LEDS` controls total pixel count; panel height auto-derives from `NUM_LEDS / PANEL_W`
- Column-major serpentine wiring: 8-LED vertical strips chained left to right

## Development

- **IDE**: Arduino IDE / arduino-cli
- **Board package**: m5stack:esp32 (M5Stack Basic Core)
- **Libraries**: M5Unified, M5GFX, FastLED
