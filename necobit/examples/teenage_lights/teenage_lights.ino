/*
 * TEENAGE LIGHTS
 * M5Stack Basic Core + Necobit MIDI Module 2 (-1)
 *
 * Receives MIDI from Teenage Engineering KO II and drives:
 *   - 11 onscreen visualizations (spectrum, waterfall, note rain, etc.)
 *   - 36 MIDI-reactive LED panel presets on WS2812B (32xN, auto-configured)
 *
 * Controls:
 *   Button A            - Previous LED preset
 *   Button B (single)   - Next screen visualization
 *   Button B (double)   - Toggle auto-transition mode (beat-reactive chaining)
 *   Button C            - Next LED preset
 *   Hold B + A          - Brightness down
 *   Hold B + C          - Brightness up
 *
 * Wiring:
 *   MIDI IN  - DIN 5-pin via Necobit stacking bus (GPIO 16 RX / 17 TX)
 *   LED Data - Grove Port B, GPIO 26 (yellow wire)
 *
 * Set NUM_LEDS to total pixel count (256 = one 32x8 panel, 512 = two, etc).
 * Panel height auto-derives from NUM_LEDS / PANEL_W.
 */

#include <M5Unified.h>
#include <FastLED.h>

// ====================== CONFIGURATION ======================
#define NUM_LEDS        256
#define MAX_LEDS        1000
#define LED_DATA_PIN    26      // Grove Port B yellow wire (G26)
#define LED_COLOR_ORDER GRB
#define LED_BRIGHTNESS  90

#define MIDI_RX_PIN     16      // Necobit module via M5-Bus
#define MIDI_TX_PIN     17      // Necobit module via M5-Bus
#define MIDI_BAUD       31250

#define NUM_BANDS       16
#define GRAD_SECTIONS   8

#define HDR_H           24
#define VIS_Y           HDR_H
#define VIS_H           (240 - HDR_H)
#define TARGET_FPS      30

// ====================== GLOBALS ============================

CRGB leds[MAX_LEDS];
int numLeds = NUM_LEDS;

// MIDI state
uint8_t noteVel[128];
uint8_t lastNote, lastVel;
unsigned long lastNoteMs;
uint8_t midiStat, midiBuf[2], midiIdx, midiNeed;
unsigned long midiByteCount = 0;
unsigned long midiNoteCount = 0;

// Spectrum bands
float bandLvl[NUM_BANDS];
float bandDisp[NUM_BANDS];
float bandPk[NUM_BANDS];

// Transient energy (fast decay for punchy LED response)
float energy = 0;

// Waterfall
uint8_t wfData[320][NUM_BANDS];
int wfX = 0;

// Note rain
#define MAX_DROPS 48
struct Drop {
    float y;
    float spd;
    int   x;
    uint8_t hue;
    uint8_t bri;
    bool  on;
};
Drop drops[MAX_DROPS];

// Pulse rings
#define MAX_RINGS 12
struct Ring {
    float   r;
    float   maxR;
    uint8_t hue;
    bool    on;
};
Ring rings[MAX_RINGS];

// Panel geometry — width fixed, height derived from total LED count
#define PANEL_W       32
#define MAX_PANEL_H  (MAX_LEDS / PANEL_W)
int panelH = NUM_LEDS / PANEL_W;

// serpentine XY mapping: columns of panelH running vertically
// even columns top-to-bottom, odd columns bottom-to-top
inline int xyToIndex(int x, int y) {
    if (x < 0 || x >= PANEL_W || y < 0 || y >= panelH) return -1;
    int idx = (x & 1) ? (x * panelH + (panelH - 1 - y)) : (x * panelH + y);
    return (idx < numLeds) ? idx : -1;
}

inline void panelSet(int x, int y, CRGB c) {
    int idx = xyToIndex(x, y);
    if (idx >= 0) leds[idx] = c;
}

inline void panelAdd(int x, int y, CRGB c) {
    int idx = xyToIndex(x, y);
    if (idx >= 0) leds[idx] |= c;
}

inline void panelFade(uint8_t amount) {
    fadeToBlackBy(leds, numLeds, amount);
}

// UI
int visMode   = 0;
int ledPreset = 0;
bool transitionMode = false;
#define NUM_VIS     11
#define NUM_PRESETS 36
const char *visName[]  = {
    "SPECTRUM", "WATERFALL", "NOTE RAIN", "PULSE",
    "PLASMA", "TUNNEL", "DEEP SPACE", "ACID",
    "CHLADNI", "GEOMETRY", "OSCILLOSCOPE"
};
const char *ledName[]  = {
    "RAINBOW", "VU METER", "COMET", "SPARKLE",
    "FIRE", "STROBE", "OCEAN", "METEOR",
    "SCREEN SYNC", "NEON PULSE", "CYBER RAIN", "SYNTHWAVE",
    "LAVA LAMP", "AURORA", "BREATHE", "GLITCH", "LIGHTNING", "SLICER",
    "NOTE MAP", "NOTE SPLASH", "NOTE STACK", "NOTE PIANO",
    "COMET TRAIL", "RAIN DROP", "STARFIELD", "WAVE",
    "FIREWORKS", "BOUNCE", "SNAKE", "WATERFALL2D", "RADAR", "TETRIS",
    "CYBERPUNK", "CHLADNI CYB", "CHLADNI MRP", "NOTE BARS"
};
bool hdrDirty  = true;
bool visClear  = true;
unsigned long lastFrameMs = 0;

// Starfield — "flying through space" background for all screen modes
#define NUM_STARS 40
struct Star {
    float x, y;      // position relative to center (-1..1 normalized)
    float speed;      // base radial speed
    uint8_t bri;
};
Star stars[NUM_STARS];
float starSpeed = 1.0f;

// Color LUTs (built in setup for fast drawing)
uint16_t heatLUT[256];
uint16_t wfLUT[256];

// ====================== COLOR HELPERS ======================

uint16_t makeHeat565(uint8_t v) {
    uint8_t r, g, b;
    if      (v <  64) { r = 0;             g = 0;                    b = v * 4; }
    else if (v < 128) { r = 0;             g = (v - 64) * 4;        b = 255 - (v - 64) * 4; }
    else if (v < 192) { r = (v - 128) * 4; g = 255;                 b = 0; }
    else              { r = 255;            g = 255 - (v - 192) * 4; b = 0; }
    return M5.Display.color565(r, g, b);
}

uint16_t hsv565(uint8_t h, uint8_t s, uint8_t v) {
    CRGB c;
    c.setHSV(h, s, v);
    return M5.Display.color565(c.r, c.g, c.b);
}

void buildLUTs() {
    for (int i = 0; i < 256; i++) {
        heatLUT[i] = makeHeat565(i);
        uint8_t hue = 160 - (uint8_t)(i * 160 / 255);
        wfLUT[i] = (i < 4) ? (uint16_t)TFT_BLACK : hsv565(hue, 255, (uint8_t)i);
    }
}

const char* noteNames[] = {"C","C#","D","D#","E","F","F#","G","G#","A","A#","B"};

String getNoteName(uint8_t note) {
    int octave = (note / 12) - 1;
    return String(noteNames[note % 12]) + String(octave);
}

void initStars() {
    for (int i = 0; i < NUM_STARS; i++) {
        float angle = random(360) * 0.01745f;
        float dist = random(5, 100) / 100.0f;
        stars[i].x = cosf(angle) * dist;
        stars[i].y = sinf(angle) * dist;
        stars[i].speed = 0.005f + random(15) / 1000.0f;
        stars[i].bri = 40 + random(80);
    }
}

void respawnStar(int i) {
    float angle = random(3600) / 573.0f; // 0..2*PI
    float dist = 0.01f + random(5) / 100.0f;
    stars[i].x = cosf(angle) * dist;
    stars[i].y = sinf(angle) * dist;
    stars[i].speed = 0.004f + random(16) / 1000.0f;
    stars[i].bri = 30 + random(60);
}

void drawStarfield() {
    int cx = 160;
    int cy = VIS_Y + VIS_H / 2;
    float midiBoost = starSpeed;

    for (int i = 0; i < NUM_STARS; i++) {
        float dist = sqrtf(stars[i].x * stars[i].x + stars[i].y * stars[i].y);
        if (dist < 0.001f) dist = 0.001f;

        // erase old position
        int oldSx = cx + (int)(stars[i].x * 160);
        int oldSy = cy + (int)(stars[i].y * (VIS_H / 2));
        if (oldSx >= 0 && oldSx < 319 && oldSy > VIS_Y && oldSy < VIS_Y + VIS_H - 1)
            M5.Display.fillRect(oldSx, oldSy, 2, 2, TFT_BLACK);

        // move outward — accelerates with distance (perspective)
        float accel = dist * 2.0f + 0.3f;
        float dx = (stars[i].x / dist) * stars[i].speed * accel * midiBoost;
        float dy = (stars[i].y / dist) * stars[i].speed * accel * midiBoost;
        stars[i].x += dx;
        stars[i].y += dy;

        // check bounds
        int sx = cx + (int)(stars[i].x * 160);
        int sy = cy + (int)(stars[i].y * (VIS_H / 2));
        if (sx < 0 || sx >= 319 || sy <= VIS_Y || sy >= VIS_Y + VIS_H - 1) {
            respawnStar(i);
            continue;
        }

        // brightness increases with distance (closer = brighter in perspective)
        uint8_t bri = stars[i].bri + (uint8_t)(dist * 180);
        if (bri < stars[i].bri) bri = 255; // overflow protection
        uint8_t size = (dist > 0.6f) ? 2 : 1;

        uint16_t col = hsv565(180 + (uint8_t)(dist * 60), 80, bri);
        M5.Display.fillRect(sx, sy, size, size, col);
    }
}

// ====================== MIDI ===============================

void recalcBands() {
    for (int b = 0; b < NUM_BANDS; b++) {
        float mx = 0;
        for (int n = b * 8; n < (b + 1) * 8 && n < 128; n++)
            if (noteVel[n] > mx) mx = noteVel[n];
        bandLvl[b] = mx / 127.0f;
    }
}

void triggerNoteOn(uint8_t note, uint8_t vel) {
    float fvel = vel / 127.0f;
    if (fvel > energy) energy = fvel;

    for (int i = 0; i < MAX_DROPS; i++) {
        if (!drops[i].on) {
            drops[i].on  = true;
            drops[i].y   = 0;
            drops[i].spd = 2.0f + fvel * 5.0f;
            drops[i].x   = (note - 36) * 5 + 2;
            if (drops[i].x < 2) drops[i].x = 2;
            if (drops[i].x > 316) drops[i].x = 316;
            drops[i].hue = (note % 12) * 21;
            drops[i].bri = (vel < 128) ? vel * 2 : 255;
            break;
        }
    }

    for (int i = 0; i < MAX_RINGS; i++) {
        if (!rings[i].on) {
            rings[i].on   = true;
            rings[i].r    = 0;
            rings[i].maxR = 20 + fvel * 90;
            rings[i].hue  = note * 2;
            break;
        }
    }

}

void handleMidiMsg(uint8_t status, uint8_t d1, uint8_t d2) {
    uint8_t type = status & 0xF0;
    if (type == 0x90 && d2 > 0) {
        noteVel[d1] = d2;
        lastNote    = d1;
        lastVel     = d2;
        lastNoteMs  = millis();
        midiNoteCount++;
        recalcBands();
        triggerNoteOn(d1, d2);
    } else if (type == 0x80 || (type == 0x90 && d2 == 0)) {
        noteVel[d1] = 0;
        recalcBands();
    }
}

void readMIDI() {
    while (Serial2.available()) {
        uint8_t b = Serial2.read();
        midiByteCount++;
        if (b >= 0xF8) continue;
        if (b & 0x80) {
            if (b >= 0xF0) { midiStat = 0; midiIdx = 0; continue; }
            midiStat = b;
            midiIdx  = 0;
            uint8_t t = b & 0xF0;
            midiNeed = (t == 0xC0 || t == 0xD0) ? 1 : 2;
            continue;
        }
        if (!midiStat) continue;
        midiBuf[midiIdx++] = b;
        if (midiIdx >= midiNeed) {
            handleMidiMsg(midiStat, midiBuf[0], midiNeed > 1 ? midiBuf[1] : 0);
            midiIdx = 0;
        }
    }
}

// ====================== ENERGY HELPERS =====================

float avgEnergy() {
    float e = 0;
    for (int b = 0; b < NUM_BANDS; b++) e += bandDisp[b];
    return e / NUM_BANDS;
}

float peakEnergy() {
    float mx = 0;
    for (int b = 0; b < NUM_BANDS; b++)
        if (bandDisp[b] > mx) mx = bandDisp[b];
    return mx;
}

// ====================== SCREEN VISUALS =====================

static uint8_t hdrFrame = 0;

void drawHeader() {
    M5.Display.startWrite();
    M5.Display.fillRect(0, 0, 320, HDR_H, TFT_BLACK);

    hdrFrame++;

    // left: vis mode name (small)
    M5.Display.setTextSize(1);
    M5.Display.setTextDatum(TL_DATUM);
    uint16_t visCol = hsv565(hdrFrame * 2, 200, 255);
    M5.Display.setTextColor(visCol, TFT_BLACK);
    M5.Display.drawString(visName[visMode], 2, 2);

    // right: LED preset name (small), with AUTO indicator
    uint16_t ledCol = hsv565(hdrFrame * 2 + 128, 200, 255);
    M5.Display.setTextColor(ledCol, TFT_BLACK);
    M5.Display.setTextDatum(TR_DATUM);
    if (transitionMode) {
        uint16_t autoCol = hsv565(hdrFrame * 4, 255, 255);
        M5.Display.setTextColor(autoCol, TFT_BLACK);
        M5.Display.drawString(String("AUTO>") + ledName[ledPreset], 318, 2);
    } else {
        M5.Display.drawString(ledName[ledPreset], 318, 2);
    }

    // center: note name (size 1, fits between left/right labels)
    bool recentHit = (millis() - lastNoteMs < 300);
    M5.Display.setTextSize(1);
    M5.Display.setTextDatum(TC_DATUM);
    if (recentHit && lastVel > 0) {
        String noteTxt = getNoteName(lastNote) + " v" + String(lastVel);
        uint8_t flashBri = (millis() - lastNoteMs < 100) ? 255 : 160;
        M5.Display.setTextColor(hsv565(lastNote * 2, 200, flashBri), TFT_BLACK);
        M5.Display.drawString(noteTxt, 160, 2);
    } else {
        M5.Display.setTextColor(0x4208, TFT_BLACK);
        M5.Display.drawString("n:" + String(midiNoteCount), 160, 2);
    }

    // second row: [A]< preset >[C]
    M5.Display.setTextSize(1);
    M5.Display.setTextDatum(TL_DATUM);
    M5.Display.setTextColor(0x3186, TFT_BLACK);
    M5.Display.drawString("[A]<", 2, 12);
    M5.Display.setTextDatum(TR_DATUM);
    M5.Display.drawString(">[C]", 318, 12);

    M5.Display.endWrite();
    hdrDirty = false;
}

// 0: SPECTRUM — mirrored gradient bars growing from center line,
//    glowing caps, floating peak dots, rich color per band

void drawSpectrum() {
    // Per-note lanes: 64 notes (C2–B6 = notes 36–99), 5px each = 320px
    static float noteDisp[64];
    static float notePk[64];
    const int FIRST_NOTE = 36;
    const int LANE_COUNT = 64;
    const int laneW = 5;
    int midY = VIS_Y + VIS_H / 2;

    M5.Display.startWrite();

    starSpeed = 0.6f + avgEnergy() * 4.0f + energy * 6.0f;
    drawStarfield();

    for (int i = 0; i < LANE_COUNT; i++) {
        int note = FIRST_NOTE + i;
        float target = (note < 128) ? noteVel[note] / 127.0f : 0;
        if (target > noteDisp[i]) noteDisp[i] += (target - noteDisp[i]) * 0.6f;
        else noteDisp[i] *= 0.88f;
        if (noteDisp[i] < 0.005f) noteDisp[i] = 0;
        if (noteDisp[i] > notePk[i]) notePk[i] = noteDisp[i];
        else notePk[i] -= 0.012f;
        if (notePk[i] < 0) notePk[i] = 0;

        int x = i * laneW;
        int halfH = (int)(noteDisp[i] * VIS_H / 2);
        uint8_t hue = (note % 12) * 21 + hdrFrame;
        int cx = x + laneW / 2;

        // clear lane
        M5.Display.fillRect(x, VIS_Y, laneW, midY - halfH - VIS_Y, TFT_BLACK);
        M5.Display.fillRect(x, midY + halfH, laneW, VIS_Y + VIS_H - midY - halfH, TFT_BLACK);

        if (halfH > 1) {
            // gradient fill mirrored from center
            for (int s = 0; s < 4; s++) {
                int y0 = halfH * s / 4;
                int y1 = halfH * (s + 1) / 4;
                int secH = y1 - y0;
                if (secH <= 0) continue;
                uint8_t pct = (uint8_t)(y0 * 255 / max(1, VIS_H / 2));
                uint8_t bri = 80 + pct * 175 / 255;
                uint16_t col = hsv565(hue + pct / 4, 240, bri);
                M5.Display.fillRect(x, midY - y1, laneW - 1, secH, col);
                M5.Display.fillRect(x, midY + y0, laneW - 1, secH, col);
            }

            // triangle tips pointing outward
            if (halfH > 6) {
                int triH = min(halfH / 3, 8);
                uint16_t tipCol = hsv565(hue + 30, 180, 255);
                M5.Display.fillTriangle(cx, midY - halfH - triH,
                    x, midY - halfH, x + laneW - 2, midY - halfH, tipCol);
                M5.Display.fillTriangle(cx, midY + halfH + triH,
                    x, midY + halfH, x + laneW - 2, midY + halfH, tipCol);
                M5.Display.fillRect(cx, midY - halfH - triH, 1, 2, TFT_WHITE);
                M5.Display.fillRect(cx, midY + halfH + triH - 1, 1, 2, TFT_WHITE);
            }
        }

        // center tick
        uint16_t tickCol = hsv565(hue, 180, 30 + (uint8_t)(noteDisp[i] * 120));
        M5.Display.fillRect(x, midY, laneW - 1, 1, tickCol);

        // floating peak marker
        int pkH = (int)(notePk[i] * VIS_H / 2);
        if (notePk[i] > 0.03f && pkH > halfH + 3) {
            uint16_t pkCol = hsv565(hue, 100, 255);
            M5.Display.fillRect(x, midY - pkH, laneW - 1, 1, pkCol);
            M5.Display.fillRect(x, midY + pkH, laneW - 1, 1, pkCol);
        }
    }

    M5.Display.endWrite();
}

// 1: WATERFALL — scrolling spectrogram, deep palette, hot pixels

void drawWaterfall() {
    // Scrolling piano roll — Y axis = 64 notes (C2–B6), X = time
    const int FIRST_NOTE = 36;
    const int LANE_COUNT = 64;
    int laneH = VIS_H / LANE_COUNT; // ~3px per note
    if (laneH < 1) laneH = 1;

    M5.Display.startWrite();

    // write current column: each note row lit by its velocity
    for (int i = 0; i < LANE_COUNT; i++) {
        int note = FIRST_NOTE + i;
        uint8_t vel = (note < 128) ? noteVel[note] : 0;
        int y = VIS_Y + i * laneH;
        if (vel > 0) {
            uint8_t hue = (note % 12) * 21 + hdrFrame;
            uint8_t bri = 40 + vel * 215 / 127;
            uint8_t sat = (vel > 100) ? 180 : 240;
            M5.Display.fillRect(wfX, y, 2, laneH, hsv565(hue, sat, bri));
            if (vel > 100)
                M5.Display.fillRect(wfX, y, 2, 1, TFT_WHITE);
        } else {
            M5.Display.fillRect(wfX, y, 2, laneH, TFT_BLACK);
        }
    }

    // bright flash on new note hits
    if (energy > 0.15f) {
        int noteIdx = lastNote - FIRST_NOTE;
        if (noteIdx >= 0 && noteIdx < LANE_COUNT) {
            int y = VIS_Y + noteIdx * laneH;
            uint16_t fc = hsv565(lastNote * 2, 140, 255);
            M5.Display.fillRect(wfX, y - 1, 2, laneH + 2, fc);
            M5.Display.fillRect(wfX, y, 2, 1, TFT_WHITE);
        }
    }

    // advance write head
    int nx = (wfX + 2) % 320;
    M5.Display.fillRect(nx, VIS_Y, 2, VIS_H, hsv565(hdrFrame * 3, 255, 30));
    int nx2 = (nx + 2) % 320;
    M5.Display.fillRect(nx2, VIS_Y, 2, VIS_H, TFT_BLACK);
    wfX = nx;

    M5.Display.endWrite();
}

// 2: NOTE RAIN — drops in fixed note lanes (64 lanes, 5px each)

void drawNoteRain() {
    const int laneW = 5;

    M5.Display.startWrite();

    starSpeed = 0.5f + avgEnergy() * 4.0f + energy * 6.0f;
    drawStarfield();

    for (int i = 0; i < MAX_DROPS; i++) {
        if (!drops[i].on) continue;

        int lx = drops[i].x - laneW / 2;
        if (lx < 0) lx = 0;
        int ey = VIS_Y + (int)drops[i].y;
        int eraseTop = ey - 36;
        if (eraseTop < VIS_Y) eraseTop = VIS_Y;
        M5.Display.fillRect(lx, eraseTop, laneW, 44, TFT_BLACK);

        drops[i].y += drops[i].spd;

        if (drops[i].y >= VIS_H - 4) {
            drops[i].on = false;
            int sy = VIS_Y + VIS_H - 2;
            // splash particles within the lane and neighbors
            for (int sp = 0; sp < 8; sp++) {
                int sx = drops[i].x - 8 + random(17);
                int sdy = random(12);
                if (sx >= 0 && sx < 320)
                    M5.Display.fillRect(sx, sy - sdy, 2, 2,
                        hsv565(drops[i].hue + random(30), 220, 140 + random(116)));
            }
            // bottom splash triangle
            uint16_t splCol = hsv565(drops[i].hue + 10, 200, 140);
            M5.Display.fillTriangle(
                drops[i].x, sy - 7,
                drops[i].x - 4, sy,
                drops[i].x + 4, sy, splCol);
            continue;
        }

        int dy = VIS_Y + (int)drops[i].y;
        uint16_t headCol = hsv565(drops[i].hue, 170, drops[i].bri);

        // triangle head pointing down, fits in lane
        M5.Display.fillTriangle(
            drops[i].x, dy + 3,
            drops[i].x - 2, dy - 2,
            drops[i].x + 2, dy - 2, headCol);
        M5.Display.fillRect(drops[i].x, dy, 1, 2, TFT_WHITE);

        // tail trail within the lane
        for (int t = 1; t < 10; t++) {
            int ty = dy - t * 3 - 3;
            if (ty < VIS_Y) break;
            uint8_t fade = (drops[i].bri > t * 22) ? drops[i].bri - t * 22 : 0;
            if (fade < 4) break;
            int tw = (t < 3) ? 3 : 1;
            M5.Display.fillRect(drops[i].x - tw / 2, ty, tw, 2,
                hsv565(drops[i].hue + t * 5, 255, fade));
        }
    }
    M5.Display.endWrite();
}

// 3: PULSE — massive shockwaves over starfield, screen-filling
//    blasts, stacking horizontal + diagonal slams, all fast rects

void drawPulse() {
    M5.Display.startWrite();
    M5.Display.fillRect(0, VIS_Y, 320, VIS_H, TFT_BLACK);

    // starfield background — faster with energy
    starSpeed = 1.0f + avgEnergy() * 6.0f + energy * 10.0f;
    drawStarfield();

    int cx = 160;
    int cy = VIS_Y + VIS_H / 2;
    float e = avgEnergy();
    float pk = peakEnergy();
    int halfW = 158;
    int halfH = VIS_H / 2 - 2;

    // expanding diamond/rect shockwaves (back to front)
    for (int i = MAX_RINGS - 1; i >= 0; i--) {
        if (!rings[i].on) continue;
        rings[i].r += 2.0f + pk * 3.0f;
        if (rings[i].r >= rings[i].maxR) { rings[i].on = false; continue; }

        float progress = rings[i].r / rings[i].maxR;
        float invP = 1.0f - progress;
        uint8_t bri = (uint8_t)(invP * invP * 255);
        if (bri < 4) continue;

        int hw = (int)(progress * halfW);
        int hh = (int)(progress * halfH);

        // big filled interior on new waves
        if (progress < 0.25f) {
            uint8_t fb = (uint8_t)(invP * 80);
            M5.Display.fillRect(cx - hw, cy - hh, hw * 2, hh * 2,
                hsv565(rings[i].hue + 20, 200, fb));
        }

        // thick shockwave band — 4 rects deep
        for (int w = 0; w < 4; w++) {
            int rw = hw + w * 2;
            int rh = hh + w * 2;
            int x0 = max(0, cx - rw);
            int y0 = max(VIS_Y, cy - rh);
            int x1 = min(319, cx + rw);
            int y1 = min(VIS_Y + VIS_H - 1, cy + rh);
            if (x1 <= x0 || y1 <= y0) continue;

            uint8_t bFade = bri - w * (bri / 5);
            uint8_t hue = rings[i].hue + w * 12 + (uint8_t)(progress * 70);
            uint16_t c = hsv565(hue, 250 - w * 15, bFade);

            int thick = (w == 0) ? 3 : 2;
            M5.Display.fillRect(x0, y0, x1 - x0, thick, c);
            M5.Display.fillRect(x0, y1 - thick + 1, x1 - x0, thick, c);
            M5.Display.fillRect(x0, y0, thick, y1 - y0, c);
            M5.Display.fillRect(x1 - thick + 1, y0, thick, y1 - y0, c);
        }

        // horizontal slam bars — wide colored bars at wave height
        if (progress < 0.5f && bri > 80) {
            int barH = 2 + (int)(invP * 6);
            uint16_t bc = hsv565(rings[i].hue + 40, 220, bri * 2 / 3);
            M5.Display.fillRect(0, cy - hh - barH, 320, barH, bc);
            M5.Display.fillRect(0, cy + hh, 320, barH, bc);
        }
    }

    // diagonal energy slashes on hits
    if (energy > 0.2f) {
        int len = (int)(energy * halfH * 1.5f);
        uint8_t hue = lastNote * 2 + 60;
        uint8_t bri = (uint8_t)(energy * 200);
        for (int d = 0; d < len; d++) {
            int x1 = cx - len + d * 2;
            int y1 = cy - len + d;
            int x2 = cx + len - d * 2;
            int y2 = cy + len - d;
            if (x1 >= 0 && x1 < 318 && y1 > VIS_Y && y1 < VIS_Y + VIS_H)
                M5.Display.fillRect(x1, y1, 3, 2, hsv565(hue, 200, bri));
            if (x2 >= 0 && x2 < 318 && y2 > VIS_Y && y2 < VIS_Y + VIS_H)
                M5.Display.fillRect(x2, y2, 3, 2, hsv565(hue + 30, 200, bri));
        }
    }

    // rotating diamond/triangle geometry overlay
    if (e > 0.02f || energy > 0.05f) {
        static uint8_t geoAngle = 0;
        geoAngle += 2 + (uint8_t)(energy * 8);
        int diam = (int)((e + energy) * halfH * 0.8f);
        if (diam > 6) {
            uint8_t hue = geoAngle + lastNote * 3;
            uint8_t bri = 60 + (uint8_t)((e + energy) * 140);
            uint16_t gc = hsv565(hue, 230, bri);
            // four triangles forming a diamond/star
            M5.Display.fillTriangle(cx, cy - diam, cx - diam / 2, cy, cx + diam / 2, cy, gc);
            M5.Display.fillTriangle(cx, cy + diam, cx - diam / 2, cy, cx + diam / 2, cy, gc);
            uint16_t gc2 = hsv565(hue + 64, 230, bri * 3 / 4);
            M5.Display.fillTriangle(cx - diam, cy, cx, cy - diam / 2, cx, cy + diam / 2, gc2);
            M5.Display.fillTriangle(cx + diam, cy, cx, cy - diam / 2, cx, cy + diam / 2, gc2);
            // inner outline diamond
            int id = diam / 2;
            uint16_t ic = hsv565(hue + 128, 180, bri);
            M5.Display.drawLine(cx, cy - id, cx + id, cy, ic);
            M5.Display.drawLine(cx + id, cy, cx, cy + id, ic);
            M5.Display.drawLine(cx, cy + id, cx - id, cy, ic);
            M5.Display.drawLine(cx - id, cy, cx, cy - id, ic);
        }
    }

    // massive cross blast from center
    float total = e + energy;
    if (total > 0.02f) {
        int barW = (int)(total * 155);
        int barH = 4 + (int)(energy * 14);
        uint8_t hue = lastNote * 2;
        uint8_t bri = (uint8_t)(total * 240);
        M5.Display.fillRect(cx - barW, cy - barH / 2, barW * 2, barH,
            hsv565(hue, 230, bri));
        int vH = (int)(total * halfH);
        int vW = 3 + (int)(energy * 8);
        M5.Display.fillRect(cx - vW / 2, cy - vH, vW, vH * 2,
            hsv565(hue + 40, 230, bri * 4 / 5));
    }

    // center glow on hard hits
    if (energy > 0.4f) {
        int fw = 6 + (int)(energy * 30);
        int fh = 4 + (int)(energy * 20);
        uint16_t gc = hsv565(lastNote * 2, 120, (uint8_t)(energy * 255));
        M5.Display.fillRect(max(0, cx - fw), max(VIS_Y, cy - fh),
            min(320, fw * 2), min(VIS_H, fh * 2), gc);
        // colored halo
        int hw2 = fw + 6; int hh2 = fh + 6;
        uint16_t halo = hsv565(lastNote * 2, 120, (uint8_t)(energy * 200));
        M5.Display.fillRect(max(0, cx - hw2), max(VIS_Y, cy - hh2),
            min(320, hw2 * 2), 4, halo);
        M5.Display.fillRect(max(0, cx - hw2), min(VIS_Y + VIS_H - 4, cy + hh2 - 4),
            min(320, hw2 * 2), 4, halo);
    } else if (energy > 0.06f) {
        int cw = 5 + (int)(energy * 30);
        int ch = 4 + (int)(energy * 20);
        M5.Display.fillRect(cx - cw, cy - ch, cw * 2, ch * 2,
            hsv565(lastNote * 2, 180, (uint8_t)(energy * 240)));
    }

    M5.Display.endWrite();
}

// 4: PLASMA — classic demoscene plasma, MIDI warps the field,
//    sin8-based for speed, full psychedelic color cycling

void drawPlasma() {
    M5.Display.startWrite();

    static uint16_t plasmaT = 0;
    float e = avgEnergy();
    float total = e + energy;
    plasmaT += 2 + (uint16_t)(total * 12);

    // starfield underneath
    starSpeed = 0.8f + total * 8.0f;
    if (total < 0.02f) {
        // dim fade when silent instead of full clear
        for (int y = VIS_Y; y < VIS_Y + VIS_H; y += 4)
            M5.Display.fillRect(0, y, 320, 4, TFT_BLACK);
        drawStarfield();
        M5.Display.endWrite();
        return;
    }

    // draw plasma in coarse 4x4 blocks for speed
    uint8_t hueShift = (uint8_t)(plasmaT / 3);
    uint8_t energyBri = (uint8_t)(total * 200);

    for (int by = 0; by < VIS_H; by += 4) {
        int sy = VIS_Y + by;
        uint8_t yWave1 = sin8(by * 4 + plasmaT / 2);
        uint8_t yWave2 = sin8(by * 6 - plasmaT);

        for (int bx = 0; bx < 320; bx += 4) {
            uint8_t xWave1 = sin8(bx * 3 + plasmaT);
            uint8_t xWave2 = sin8(bx * 5 - plasmaT / 2 + 64);

            uint8_t plasma = qadd8(
                qadd8(xWave1 / 4, yWave1 / 4),
                qadd8(xWave2 / 4, yWave2 / 4)
            );

            // diagonal interference
            uint8_t diag = sin8((bx + by) * 2 + plasmaT * 2);
            plasma = qadd8(plasma, diag / 4);

            // MIDI modulation — bands warp the field
            int band = (bx * NUM_BANDS) / 320;
            if (band >= NUM_BANDS) band = NUM_BANDS - 1;
            uint8_t bandBoost = (uint8_t)(bandDisp[band] * 80);
            plasma = qadd8(plasma, bandBoost);

            uint8_t hue = plasma + hueShift + lastNote;
            uint8_t bri = scale8(plasma, energyBri);
            bri = qadd8(bri, (uint8_t)(energy * 60));
            bri = qadd8(bri, 30);

            M5.Display.fillRect(bx, sy, 4, 4, hsv565(hue, 230, bri));
        }
    }

    M5.Display.endWrite();
}

// 5: TUNNEL — concentric rectangles zooming inward, warp speed
//    on MIDI hits, color rings shift per note

void drawTunnel() {
    M5.Display.startWrite();
    M5.Display.fillRect(0, VIS_Y, 320, VIS_H, TFT_BLACK);

    starSpeed = 1.0f + avgEnergy() * 8.0f + energy * 12.0f;
    drawStarfield();

    float e = avgEnergy();
    float total = e + energy;

    static uint16_t tunnelPhase = 0;
    tunnelPhase += 3 + (uint16_t)(total * 15);

    int cx = 160;
    int cy = VIS_Y + VIS_H / 2;
    int maxRings = 16;
    int halfW = 158;
    int halfHt = VIS_H / 2 - 2;

    // purple(192) -> pink(224) -> blue(160) palette
    // hue cycles within this range based on ring depth + phase
    for (int r = maxRings - 1; r >= 0; r--) {
        float t = (float)r / maxRings;
        float animT = t + (float)(tunnelPhase % 256) / 256.0f / maxRings;
        if (animT > 1.0f) animT -= 1.0f;

        float scale = animT * animT;
        int rw = (int)(scale * halfW);
        int rh = (int)(scale * halfHt);
        if (rw < 2 || rh < 2) continue;

        // purple-pink-blue range: hue 160-224 (mapped from blue through purple to pink)
        uint8_t hueBase = 160 + ((tunnelPhase / 6 + r * 5 + lastNote) % 65);
        float briScale = scale * 1.4f;
        if (briScale > 1.0f) briScale = 1.0f;
        uint8_t bri = (uint8_t)(briScale * 240);
        bri = scale8(bri, (uint8_t)(total * 255));
        bri = qadd8(bri, (uint8_t)(energy * (1.0f - t) * 140));
        if (bri < 8) continue;

        uint16_t c = hsv565(hueBase, 220, bri);

        int x0 = max(0, cx - rw);
        int y0 = max(VIS_Y, cy - rh);
        int x1 = min(319, cx + rw);
        int y1 = min(VIS_Y + VIS_H - 1, cy + rh);

        // thicker rings — outer ones chunkier
        int thick = (scale > 0.6f) ? 4 : (scale > 0.3f) ? 3 : 2;

        M5.Display.fillRect(x0, y0, x1 - x0, thick, c);
        M5.Display.fillRect(x0, y1 - thick + 1, x1 - x0, thick, c);
        M5.Display.fillRect(x0, y0, thick, y1 - y0, c);
        M5.Display.fillRect(x1 - thick + 1, y0, thick, y1 - y0, c);

        // bright inner glow line for depth
        if (bri > 60 && scale > 0.15f) {
            uint16_t gc = hsv565(hueBase + 20, 180, min(255, bri + 40));
            M5.Display.fillRect(x0 + thick, y0 + thick, x1 - x0 - thick * 2, 1, gc);
            M5.Display.fillRect(x0 + thick, y1 - thick, x1 - x0 - thick * 2, 1, gc);
            M5.Display.fillRect(x0 + thick, y0 + thick, 1, y1 - y0 - thick * 2, gc);
            M5.Display.fillRect(x1 - thick, y0 + thick, 1, y1 - y0 - thick * 2, gc);
        }

        // bright corner squares on outer rings
        if (bri > 80 && scale > 0.35f) {
            int cs = 2 + (int)(scale * 4);
            uint16_t cc = hsv565(hueBase + 32, 160, min(255, bri + 50));
            M5.Display.fillRect(x0, y0, cs, cs, cc);
            M5.Display.fillRect(x1 - cs + 1, y0, cs, cs, cc);
            M5.Display.fillRect(x0, y1 - cs + 1, cs, cs, cc);
            M5.Display.fillRect(x1 - cs + 1, y1 - cs + 1, cs, cs, cc);
        }
    }

    // edge flash bars on hits
    if (energy > 0.15f) {
        int barSz = 4 + (int)(energy * 12);
        uint16_t tc = hsv565(200, 200, (uint8_t)(energy * 220));
        M5.Display.fillRect(cx - barSz * 2, VIS_Y, barSz * 4, 3, tc);
        M5.Display.fillRect(cx - barSz * 2, VIS_Y + VIS_H - 3, barSz * 4, 3, tc);
        M5.Display.fillRect(0, cy - barSz, 3, barSz * 2, tc);
        M5.Display.fillRect(317, cy - barSz, 3, barSz * 2, tc);
    }

    // vanishing point glow
    uint8_t vpBri = 60 + (uint8_t)(total * 180);
    M5.Display.fillRect(cx - 2, cy - 2, 5, 5, hsv565(200, 180, vpBri));

    M5.Display.endWrite();
}

// 6: KALEIDOSCOPE — mirrored 4-quadrant symmetric patterns,
//    bands generate shapes that reflect across X/Y axes

// 6: DEEP SPACE — stars, planets, and UFOs spawned by MIDI notes

#define MAX_PLANETS 8
#define MAX_UFOS    6
struct Planet {
    int x, y, r;
    uint8_t hue, ringHue;
    bool hasRing;
    uint8_t life;
};
struct Ufo {
    float x, y, dx;
    uint8_t hue, life;
    bool on;
};
static Planet planets[MAX_PLANETS];
static Ufo ufos[MAX_UFOS];
static bool spaceInit = false;

void drawKaleidoscope() {
    M5.Display.startWrite();
    M5.Display.fillRect(0, VIS_Y, 320, VIS_H, TFT_BLACK);

    float e = avgEnergy();
    float total = e + energy;

    if (!spaceInit) {
        memset(planets, 0, sizeof(planets));
        memset(ufos, 0, sizeof(ufos));
        spaceInit = true;
    }

    // Background: dense starfield — always visible, speed with energy
    starSpeed = 0.4f + total * 5.0f;
    drawStarfield();

    // Extra twinkling stars (more than the base starfield)
    static uint16_t spaceTick = 0;
    spaceTick++;
    for (int s = 0; s < 30; s++) {
        int sx = (s * 107 + spaceTick * 3) % 320;
        int sy = VIS_Y + (s * 73 + spaceTick * 2) % VIS_H;
        uint8_t twinkle = sin8(spaceTick * 4 + s * 37);
        if (twinkle > 140) {
            uint8_t bri = twinkle - 60;
            uint16_t sc = hsv565(s * 20, (s % 3 == 0) ? 60 : 0, bri);
            M5.Display.fillRect(sx, sy, 1 + (twinkle > 220 ? 1 : 0), 1, sc);
        }
    }

    // Spawn planets on note hits
    static uint32_t prevNC = 0;
    if (midiNoteCount != prevNC) {
        prevNC = midiNoteCount;

        // spawn a planet
        for (int i = 0; i < MAX_PLANETS; i++) {
            if (planets[i].life == 0) {
                planets[i].x = 20 + (lastNote * 37 + spaceTick) % 280;
                planets[i].y = VIS_Y + 15 + (lastNote * 19 + lastVel) % (VIS_H - 30);
                planets[i].r = 4 + lastVel / 20;
                planets[i].hue = (lastNote % 12) * 21;
                planets[i].ringHue = planets[i].hue + 60;
                planets[i].hasRing = (lastNote % 3 == 0);
                planets[i].life = 120 + lastVel;
                break;
            }
        }

        // spawn a UFO on every 3rd note
        if (lastNote % 3 == 1) {
            for (int i = 0; i < MAX_UFOS; i++) {
                if (!ufos[i].on) {
                    ufos[i].on = true;
                    ufos[i].x = (lastVel > 64) ? -10 : 330;
                    ufos[i].y = VIS_Y + 10 + (lastNote * 13) % (VIS_H - 20);
                    ufos[i].dx = (lastVel > 64) ? (1.5f + lastVel / 60.0f) : -(1.5f + lastVel / 60.0f);
                    ufos[i].hue = lastNote * 5;
                    ufos[i].life = 200;
                    break;
                }
            }
        }
    }

    // Draw planets
    for (int i = 0; i < MAX_PLANETS; i++) {
        if (planets[i].life == 0) continue;
        planets[i].life--;
        int px = planets[i].x;
        int py = planets[i].y;
        int pr = planets[i].r;
        uint8_t fade = (planets[i].life > 60) ? 255 : planets[i].life * 4;
        uint8_t hue = planets[i].hue;

        // planet body — filled circle approximation with rects
        for (int dy = -pr; dy <= pr; dy++) {
            int halfW = pr - abs(dy) * pr / max(1, pr);
            if (halfW < 1) halfW = 1;
            int ry = py + dy;
            if (ry < VIS_Y || ry >= VIS_Y + VIS_H) continue;
            // shading: lighter on top, darker on bottom
            uint8_t shade = (uint8_t)(200 + dy * 40 / max(1, pr));
            uint16_t pc = hsv565(hue, 200, scale8(shade, fade));
            M5.Display.fillRect(px - halfW, ry, halfW * 2 + 1, 1, pc);
        }

        // highlight spot (top-left)
        if (pr > 3 && fade > 100) {
            M5.Display.fillRect(px - pr / 3, py - pr / 3, 2, 1,
                hsv565(hue, 80, fade));
        }

        // ring (like Saturn)
        if (planets[i].hasRing && pr > 3) {
            int rw = pr + 4 + pr / 2;
            uint16_t rc = hsv565(planets[i].ringHue, 150, scale8(180, fade));
            M5.Display.fillRect(px - rw, py, rw * 2, 1, rc);
            M5.Display.fillRect(px - rw + 1, py + 1, rw * 2 - 2, 1,
                hsv565(planets[i].ringHue + 10, 120, scale8(120, fade)));
        }
    }

    // Draw UFOs
    for (int i = 0; i < MAX_UFOS; i++) {
        if (!ufos[i].on) continue;
        ufos[i].x += ufos[i].dx;
        ufos[i].life--;
        if (ufos[i].life == 0 || ufos[i].x < -20 || ufos[i].x > 340) {
            ufos[i].on = false;
            continue;
        }
        int ux = (int)ufos[i].x;
        int uy = (int)ufos[i].y;
        // wobble up and down
        uy += (int)((int8_t)(sin8(spaceTick * 6 + i * 80) - 128) * 4 / 128);
        if (uy < VIS_Y + 2) uy = VIS_Y + 2;
        if (uy > VIS_Y + VIS_H - 4) uy = VIS_Y + VIS_H - 4;

        uint8_t fade = (ufos[i].life > 40) ? 255 : ufos[i].life * 6;
        uint8_t hue = ufos[i].hue + (uint8_t)(spaceTick * 3);

        // dome (top half-circle approx)
        M5.Display.fillRect(ux - 2, uy - 3, 5, 2, hsv565(hue + 40, 140, scale8(220, fade)));
        // body — wide saucer
        M5.Display.fillRect(ux - 6, uy - 1, 13, 2, hsv565(hue, 180, fade));
        // undercarriage
        M5.Display.fillRect(ux - 3, uy + 1, 7, 1, hsv565(hue + 20, 200, scale8(160, fade)));
        // blinking lights on saucer edges
        if ((spaceTick + i * 30) % 8 < 4) {
            M5.Display.fillRect(ux - 6, uy - 1, 1, 1, hsv565(0, 255, fade));
            M5.Display.fillRect(ux + 6, uy - 1, 1, 1, hsv565(96, 255, fade));
        }
        // tractor beam on some
        if (i % 2 == 0 && fade > 120) {
            for (int b = 0; b < 8; b++) {
                int bx = ux - 2 + b / 2;
                int by = uy + 2 + b;
                if (by < VIS_Y + VIS_H)
                    M5.Display.fillRect(bx, by, 1 + b / 3, 1,
                        hsv565(hue + 80, 100, 30 + (uint8_t)(sin8(spaceTick * 8 + b * 30) / 4)));
            }
        }
    }

    // Nebula glow on sustained energy — colored fog near center
    if (e > 0.1f) {
        int ncx = 160, ncy = VIS_Y + VIS_H / 2;
        int nw = 30 + (int)(e * 80);
        int nh = 20 + (int)(e * 50);
        uint8_t nBri = (uint8_t)(e * 50);
        M5.Display.fillRect(ncx - nw, ncy - nh, nw * 2, nh * 2,
            hsv565(spaceTick / 3, 200, nBri));
    }

    M5.Display.endWrite();
}

// 7: ACID — full-screen color melt, overlapping waves in
//    psychedelic neon, horizontal bands that shift and throb

void drawAcid() {
    M5.Display.startWrite();

    float e = avgEnergy();
    float total = e + energy;

    static uint16_t acidPhase = 0;
    acidPhase += 3 + (uint16_t)(total * 18);

    if (total < 0.02f) {
        for (int y = VIS_Y; y < VIS_Y + VIS_H; y += 6)
            M5.Display.fillRect(0, y, 320, 6, TFT_BLACK);
        starSpeed = 0.3f;
        drawStarfield();
        M5.Display.endWrite();
        return;
    }

    uint8_t baseHue = (uint8_t)(acidPhase / 3) + lastNote * 3;
    uint8_t energyScale = (uint8_t)(total * 245);

    // Layer 1: warped plasma field — 4x4 blocks with extreme color shifts
    for (int row = 0; row < VIS_H; row += 4) {
        int sy = VIS_Y + row;
        uint8_t rw1 = sin8(row * 6 + acidPhase);
        uint8_t rw2 = sin8(row * 4 - acidPhase * 2 + 128);
        uint8_t rw3 = sin8(row * 9 + acidPhase / 2 + 64);
        uint8_t rw4 = sin8(row * 2 + acidPhase * 3);

        for (int col = 0; col < 320; col += 4) {
            uint8_t cw1 = sin8(col * 5 + acidPhase + rw1);
            uint8_t cw2 = sin8(col * 3 - acidPhase / 2 + rw2);
            uint8_t cw3 = sin8(col * 7 + acidPhase * 2 - rw3);

            uint8_t val = qadd8(qadd8(rw1 / 4, cw1 / 4), qadd8(rw2 / 5, cw2 / 5));
            val = qadd8(val, qadd8(rw3 / 6, cw3 / 6));
            val = qadd8(val, rw4 / 5);

            int bandForRow = (row * NUM_BANDS) / VIS_H;
            if (bandForRow >= NUM_BANDS) bandForRow = NUM_BANDS - 1;
            val = qadd8(val, (uint8_t)(bandDisp[bandForRow] * 140));

            // extreme color cycling — 3 hue zones that fight each other
            uint8_t hue = baseHue + val;
            if ((row / 8) % 3 == 0) hue += 85;
            else if ((row / 8) % 3 == 1) hue += 170;

            // column-based hue warp
            hue += sin8(col * 2 + acidPhase) / 4;

            uint8_t bri = scale8(val, energyScale);

            // hot flicker zones
            if (energy > 0.2f) {
                uint8_t flicker = sin8(col * 4 + row * 3 + acidPhase * 4);
                if (flicker > 180) bri = qadd8(bri, (uint8_t)(energy * 100));
            }

            M5.Display.fillRect(col, sy, 4, 4, hsv565(hue, 235, bri));
        }
    }

    // Layer 2: pulsing neon vertical bars on hits
    if (energy > 0.12f) {
        int numBars = 3 + (int)(energy * 8);
        for (int b = 0; b < numBars; b++) {
            int bx = (lastNote * 37 + b * 53 + acidPhase / 3) % 320;
            int bw = 2 + random(6);
            uint8_t bHue = baseHue + b * 33;
            uint8_t bBri = (uint8_t)(energy * 230);
            M5.Display.fillRect(bx, VIS_Y, bw, VIS_H, hsv565(bHue, 200, bBri));
        }
    }

    // Layer 3: horizontal scan lines that rip across
    if (energy > 0.1f) {
        int numScans = 2 + (int)(energy * 5);
        for (int s = 0; s < numScans; s++) {
            int sy = VIS_Y + ((lastNote * 19 + s * 41 + acidPhase / 2) % VIS_H);
            int sh = 2 + random(4);
            uint8_t sHue = baseHue + s * 60 + 128;
            M5.Display.fillRect(0, sy, 320, sh, hsv565(sHue, 220, (uint8_t)(energy * 200)));
        }
    }

    // Layer 4: spinning concentric hexagons
    if (total > 0.1f) {
        static uint8_t hexPhase = 0;
        hexPhase += 2 + (uint8_t)(energy * 8);
        int hcx = 160, hcy = VIS_Y + VIS_H / 2;
        int hexR = 25 + (int)(total * 70);
        for (int h = 0; h < 4; h++) {
            int r = hexR - h * 18;
            if (r < 6) break;
            uint8_t hBri = (uint8_t)(total * 140) - h * 20;
            uint16_t hcol = hsv565(baseHue + hexPhase + h * 50, 210, hBri);
            for (int seg = 0; seg < 6; seg++) {
                uint8_t a1 = (uint8_t)(seg * 43 + hexPhase + h * 10);
                uint8_t a2 = (uint8_t)((seg + 1) * 43 + hexPhase + h * 10);
                int x1 = hcx + (int)((int8_t)(cos8(a1) - 128) * r / 128);
                int y1 = hcy + (int)((int8_t)(sin8(a1) - 128) * r / 128);
                int x2 = hcx + (int)((int8_t)(cos8(a2) - 128) * r / 128);
                int y2 = hcy + (int)((int8_t)(sin8(a2) - 128) * r / 128);
                M5.Display.drawLine(x1, y1, x2, y2, hcol);
            }
        }
    }

    // Layer 5: radial burst lines on hard hits
    if (energy > 0.35f) {
        int hcx = 160, hcy = VIS_Y + VIS_H / 2;
        int numRays = 8 + (int)(energy * 12);
        int rayLen = 30 + (int)(energy * 90);
        for (int r = 0; r < numRays; r++) {
            uint8_t angle = (uint8_t)(r * 256 / numRays + acidPhase / 2);
            int ex = hcx + (int)((int8_t)(cos8(angle) - 128) * rayLen / 128);
            int ey = hcy + (int)((int8_t)(sin8(angle) - 128) * rayLen / 128);
            uint16_t rc = hsv565(baseHue + r * 20, 240, (uint8_t)(energy * 255));
            M5.Display.drawLine(hcx, hcy, ex, ey, rc);
        }
    }

    // Layer 6: color inversion flash zones on big hits
    if (energy > 0.5f) {
        int cx = 160, cy2 = VIS_Y + VIS_H / 2;
        int fw = 15 + (int)(energy * 50);
        int fh = 10 + (int)(energy * 30);
        uint16_t flashCol = hsv565(baseHue + 128, 255, (uint8_t)(energy * 240));
        M5.Display.fillRect(cx - fw, cy2 - fh, fw * 2, fh * 2, flashCol);
    }

    M5.Display.endWrite();
}

// 8: CHLADNI — standing wave interference patterns on a vibrating plate,
//    nodal lines shift with MIDI note, energy drives vibration amplitude

void drawChladni() {
    M5.Display.startWrite();

    float e = avgEnergy();
    float total = e + energy;

    static uint16_t chladPhase = 0;
    chladPhase += 1 + (uint16_t)(total * 8);

    if (total < 0.02f) {
        for (int y = VIS_Y; y < VIS_Y + VIS_H; y += 4)
            M5.Display.fillRect(0, y, 320, 4, TFT_BLACK);
        starSpeed = 0.3f;
        drawStarfield();
        M5.Display.endWrite();
        return;
    }

    // Chladni pattern: |cos(n*pi*x)*cos(m*pi*y) - cos(m*pi*x)*cos(n*pi*y)|
    // n, m driven by last MIDI note for evolving patterns
    float n = 1.0f + (lastNote % 8);
    float m = 1.0f + ((lastNote / 8) % 6);
    float phase = chladPhase * 0.01f;
    uint8_t baseHue = (uint8_t)(chladPhase / 3) + lastNote * 2;

    for (int py = 0; py < VIS_H; py += 3) {
        int sy = VIS_Y + py;
        float fy = (float)py / VIS_H;
        uint8_t yS1 = sin8((uint8_t)(fy * n * 128 + phase * 40));
        uint8_t yS2 = sin8((uint8_t)(fy * m * 128 - phase * 25));
        uint8_t yC1 = cos8((uint8_t)(fy * n * 128 + phase * 40));
        uint8_t yC2 = cos8((uint8_t)(fy * m * 128 - phase * 25));

        for (int px = 0; px < 320; px += 3) {
            float fx = (float)px / 320.0f;
            uint8_t xS1 = sin8((uint8_t)(fx * n * 128 + phase * 30));
            uint8_t xS2 = sin8((uint8_t)(fx * m * 128 - phase * 20));
            uint8_t xC1 = cos8((uint8_t)(fx * n * 128 + phase * 30));
            uint8_t xC2 = cos8((uint8_t)(fx * m * 128 - phase * 20));

            // approximation of chladni function using sin8/cos8
            int term1 = ((int)xC1 * yC2) / 256;
            int term2 = ((int)xC2 * yC1) / 256;
            int diff = abs(term1 - term2);

            // band modulation
            int band = (px * NUM_BANDS) / 320;
            if (band >= NUM_BANDS) band = NUM_BANDS - 1;
            uint8_t bandMod = (uint8_t)(bandDisp[band] * 120);

            // near nodal lines (diff near 0) glow bright — wide detection
            uint8_t nodalBri;
            if (diff < 50) {
                nodalBri = 255 - diff * 4;
            } else {
                nodalBri = max(0, 60 - (int)(diff - 50) / 2);
            }
            uint8_t eScale = 120 + (uint8_t)(total * 135);
            nodalBri = scale8(nodalBri, eScale);
            nodalBri = qadd8(nodalBri, bandMod);
            nodalBri = qadd8(nodalBri, 40);

            if (nodalBri < 3) continue;
            uint8_t hue = baseHue + diff / 3;
            M5.Display.fillRect(px, sy, 3, 3, hsv565(hue, 210, nodalBri));
        }
    }

    M5.Display.endWrite();
}

// 9: GEOMETRY — sacred geometry, rotating triangles, hexagons,
//    flower of life circles, all MIDI reactive

void drawGeometry() {
    M5.Display.startWrite();
    M5.Display.fillRect(0, VIS_Y, 320, VIS_H, TFT_BLACK);

    float e = avgEnergy();
    float total = e + energy;
    starSpeed = 0.6f + total * 8.0f;
    drawStarfield();

    if (total < 0.01f) { M5.Display.endWrite(); return; }

    static uint16_t geoPhase = 0;
    geoPhase += 2 + (uint16_t)(total * 10);

    int cx = 160;
    int cy = VIS_Y + VIS_H / 2;
    uint8_t baseHue = (uint8_t)(geoPhase / 4) + lastNote * 3;

    // LAYER 1: rotating triangle rings (3 nested, each spinning different speed)
    for (int ring = 0; ring < 3; ring++) {
        int radius = 20 + ring * 28 + (int)(total * 20);
        uint8_t angle = (uint8_t)(geoPhase / (2 + ring)) + ring * 85;
        uint8_t hue = baseHue + ring * 50;
        uint8_t bri = 140 + (uint8_t)(total * 115) - ring * 10;
        uint16_t col = hsv565(hue, 230, bri);

        // 3 vertices of equilateral triangle approximated using sin8/cos8
        int x0 = cx + (int)((int8_t)(cos8(angle) - 128) * radius / 128);
        int y0 = cy + (int)((int8_t)(sin8(angle) - 128) * radius / 128);
        int x1 = cx + (int)((int8_t)(cos8(angle + 85) - 128) * radius / 128);
        int y1 = cy + (int)((int8_t)(sin8(angle + 85) - 128) * radius / 128);
        int x2 = cx + (int)((int8_t)(cos8(angle + 170) - 128) * radius / 128);
        int y2 = cy + (int)((int8_t)(sin8(angle + 170) - 128) * radius / 128);

        if (total > 0.2f && ring == 0) {
            M5.Display.fillTriangle(x0, y0, x1, y1, x2, y2, hsv565(hue, 200, bri / 2));
        }
        M5.Display.drawTriangle(x0, y0, x1, y1, x2, y2, col);

        // counter-rotating inner triangle
        uint8_t invAngle = 255 - angle;
        int ir = radius * 2 / 3;
        int ix0 = cx + (int)((int8_t)(cos8(invAngle) - 128) * ir / 128);
        int iy0 = cy + (int)((int8_t)(sin8(invAngle) - 128) * ir / 128);
        int ix1 = cx + (int)((int8_t)(cos8(invAngle + 85) - 128) * ir / 128);
        int iy1 = cy + (int)((int8_t)(sin8(invAngle + 85) - 128) * ir / 128);
        int ix2 = cx + (int)((int8_t)(cos8(invAngle + 170) - 128) * ir / 128);
        int iy2 = cy + (int)((int8_t)(sin8(invAngle + 170) - 128) * ir / 128);
        M5.Display.drawTriangle(ix0, iy0, ix1, iy1, ix2, iy2, hsv565(hue + 128, 200, bri));
    }

    // LAYER 2: hexagonal grid pattern — grows with energy
    int hexR = 8 + (int)(total * 30);
    for (int hx = -2; hx <= 2; hx++) {
        for (int hy = -2; hy <= 2; hy++) {
            int offX = hx * hexR * 2 + (hy % 2) * hexR;
            int offY = hy * (int)(hexR * 1.7f);
            int hcx = cx + offX;
            int hcy = cy + offY;
            if (hcx < -hexR || hcx > 320 + hexR || hcy < VIS_Y - hexR || hcy > VIS_Y + VIS_H + hexR) continue;

            uint8_t hue = baseHue + abs(hx) * 30 + abs(hy) * 20;
            uint8_t bri = 40 + (uint8_t)(total * 120);
            int band = (abs(hx) + abs(hy)) % NUM_BANDS;
            bri = qadd8(bri, (uint8_t)(bandDisp[band] * 80));
            uint16_t hcol = hsv565(hue, 210, bri);

            // draw hex as 6 triangle edges
            for (int seg = 0; seg < 6; seg++) {
                uint8_t a1 = seg * 43; // 256/6
                uint8_t a2 = (seg + 1) * 43;
                int px1 = hcx + (int)((int8_t)(cos8(a1) - 128) * hexR / 128);
                int py1 = hcy + (int)((int8_t)(sin8(a1) - 128) * hexR / 128);
                int px2 = hcx + (int)((int8_t)(cos8(a2) - 128) * hexR / 128);
                int py2 = hcy + (int)((int8_t)(sin8(a2) - 128) * hexR / 128);
                M5.Display.drawLine(px1, py1, px2, py2, hcol);
            }
        }
    }

    // LAYER 3: pulsing concentric triangles from center on hits
    if (energy > 0.1f) {
        int numT = 2 + (int)(energy * 4);
        for (int t = 0; t < numT; t++) {
            int r = 8 + t * 16 + (int)(energy * t * 8);
            uint8_t hue = baseHue + t * 30 + 80;
            uint8_t bri = (uint8_t)(energy * 255) - t * 20;
            if (bri < 10) break;
            uint16_t tc = hsv565(hue, 240, bri);
            M5.Display.fillTriangle(cx, cy - r, cx - r, cy + r * 2 / 3, cx + r, cy + r * 2 / 3, tc);
        }
    }

    // LAYER 4: band-reactive diamond ring
    for (int b = 0; b < NUM_BANDS; b++) {
        float lvl = bandDisp[b];
        if (lvl < 0.05f) continue;
        uint8_t a = (uint8_t)(b * 256 / NUM_BANDS + geoPhase / 3);
        int dist = 30 + (int)(lvl * 60);
        int dx = cx + (int)((int8_t)(cos8(a) - 128) * dist / 128);
        int dy = cy + (int)((int8_t)(sin8(a) - 128) * dist / 128);
        int sz = 3 + (int)(lvl * 8);
        uint16_t dc = hsv565(baseHue + b * 14, 230, (uint8_t)(lvl * 255));
        // tiny diamond
        M5.Display.fillTriangle(dx, dy - sz, dx - sz, dy, dx + sz, dy, dc);
        M5.Display.fillTriangle(dx, dy + sz, dx - sz, dy, dx + sz, dy, dc);
    }

    // center glow
    if (total > 0.05f) {
        int cSz = 3 + (int)(total * 8);
        M5.Display.fillRect(cx - cSz, cy - cSz, cSz * 2, cSz * 2,
            hsv565(baseHue, 200, (uint8_t)(total * 240)));
    }

    M5.Display.endWrite();
}

// 10: OSCILLOSCOPE — waveform display, MIDI notes draw sine waves
//     with harmonics, XY lissajous patterns, all reactive

void drawOscilloscope() {
    M5.Display.startWrite();
    M5.Display.fillRect(0, VIS_Y, 320, VIS_H, TFT_BLACK);

    float e = avgEnergy();
    float total = e + energy;
    starSpeed = 0.4f + total * 5.0f;
    drawStarfield();

    if (total < 0.01f) { M5.Display.endWrite(); return; }

    static uint16_t oscPhase = 0;
    oscPhase += 3 + (uint16_t)(total * 12);

    int cx = 160;
    int cy = VIS_Y + VIS_H / 2;
    uint8_t baseHue = (uint8_t)(oscPhase / 4);

    // LAYER 1: main waveform — composite of active band frequencies
    int prevY1 = cy, prevY2 = cy;
    for (int x = 0; x < 320; x++) {
        float wave = 0;
        float wave2 = 0;
        for (int b = 0; b < NUM_BANDS; b++) {
            float lvl = bandDisp[b];
            if (lvl < 0.02f) continue;
            float freq = (b + 1) * 2.5f;
            uint8_t s = sin8((uint8_t)(x * freq / 2 + oscPhase + b * 20));
            wave += (int8_t)(s - 128) * lvl / 128.0f;
            uint8_t c = cos8((uint8_t)(x * freq / 2 + oscPhase + b * 30));
            wave2 += (int8_t)(c - 128) * lvl / 128.0f;
        }
        int amp = VIS_H / 2 - 8;
        int y1 = cy + (int)(wave * amp * 0.4f);
        int y2 = cy + (int)(wave2 * amp * 0.3f);
        y1 = constrain(y1, VIS_Y + 2, VIS_Y + VIS_H - 3);
        y2 = constrain(y2, VIS_Y + 2, VIS_Y + VIS_H - 3);

        uint8_t hue1 = baseHue + x / 4;
        uint8_t bri1 = 120 + (uint8_t)(total * 130);
        M5.Display.drawLine(max(0, x - 1), prevY1, x, y1, hsv565(hue1, 220, bri1));
        // second waveform (phase-shifted, different color)
        M5.Display.drawLine(max(0, x - 1), prevY2, x, y2, hsv565(hue1 + 85, 200, bri1 * 2 / 3));
        prevY1 = y1;
        prevY2 = y2;

        // glow beneath main wave
        if (abs(y1 - cy) > 8) {
            int glowH = abs(y1 - cy) / 4;
            int gy = (y1 < cy) ? y1 : y1 - glowH;
            M5.Display.fillRect(x, gy, 1, glowH, hsv565(hue1, 200, 30));
        }
    }

    // LAYER 2: Lissajous figure in center
    if (total > 0.1f) {
        float freqA = 2.0f + (lastNote % 5);
        float freqB = 3.0f + (lastNote % 4);
        int lissR = 20 + (int)(total * 50);
        int prevLx = cx, prevLy = cy;
        for (int t = 0; t < 200; t++) {
            uint8_t tByte = (uint8_t)(t * 256 / 200);
            int lx = cx + (int)((int8_t)(sin8((uint8_t)(tByte * freqA + oscPhase)) - 128) * lissR / 128);
            int ly = cy + (int)((int8_t)(sin8((uint8_t)(tByte * freqB + oscPhase / 2)) - 128) * lissR / 128);
            lx = constrain(lx, 2, 317);
            ly = constrain(ly, VIS_Y + 2, VIS_Y + VIS_H - 3);
            if (t > 0)
                M5.Display.drawLine(prevLx, prevLy, lx, ly,
                    hsv565(baseHue + 40 + t / 3, 180, 60 + (uint8_t)(total * 150)));
            prevLx = lx;
            prevLy = ly;
        }
    }

    // LAYER 3: trigger markers — triangles at zero crossings on hits
    if (energy > 0.15f) {
        uint16_t mc = hsv565(lastNote * 2, 200, (uint8_t)(energy * 220));
        int triSz = 4 + (int)(energy * 6);
        // left trigger arrow
        M5.Display.fillTriangle(0, cy - triSz, triSz * 2, cy, 0, cy + triSz, mc);
        // right trigger arrow
        M5.Display.fillTriangle(319, cy - triSz, 319 - triSz * 2, cy, 319, cy + triSz, mc);
    }

    // center line
    uint8_t cBri = 20 + (uint8_t)(total * 40);
    M5.Display.drawFastHLine(0, cy, 320, hsv565(baseHue, 100, cBri));

    // graticule marks
    for (int gx = 0; gx < 320; gx += 40) {
        M5.Display.fillRect(gx, cy - 1, 1, 3, hsv565(0, 0, cBri));
    }
    for (int gy = VIS_Y; gy < VIS_Y + VIS_H; gy += 20) {
        M5.Display.fillRect(159, gy, 3, 1, hsv565(0, 0, cBri));
    }

    M5.Display.endWrite();
}

void drawPanelStatus() {
    M5.Display.startWrite();

    String line1 = String(PANEL_W) + "x" + String(panelH) + " " + String(numLeds) + "px";
    uint8_t curBri = FastLED.getBrightness();
    String line2 = "BRI:" + String((int)(curBri * 100 / 255)) + "%";

    int boxW = max(line1.length(), line2.length()) * 6 + 8;
    int boxH = 20;
    int bx = 2;
    int by = 240 - boxH - 2;

    M5.Display.fillRect(bx, by, boxW, boxH, 0x0841);

    M5.Display.setTextSize(1);
    M5.Display.setTextDatum(TL_DATUM);
    uint16_t col = hsv565(hdrFrame * 3, 220, 220);
    M5.Display.setTextColor(col, 0x0841);
    M5.Display.drawString(line1, bx + 3, by + 2);
    M5.Display.setTextColor(0xC618, 0x0841);
    M5.Display.drawString(line2, bx + 3, by + 11);

    M5.Display.endWrite();
}

void updateDisplay() {
    drawHeader();
    if (visClear) {
        M5.Display.fillRect(0, VIS_Y, 320, VIS_H, TFT_BLACK);
        visClear = false;
    }
    switch (visMode) {
        case 0:  drawSpectrum();     break;
        case 1:  drawWaterfall();    break;
        case 2:  drawNoteRain();     break;
        case 3:  drawPulse();        break;
        case 4:  drawPlasma();       break;
        case 5:  drawTunnel();       break;
        case 6:  drawKaleidoscope(); break;
        case 7:  drawAcid();         break;
        case 8:  drawChladni();      break;
        case 9:  drawGeometry();     break;
        case 10: drawOscilloscope(); break;
    }
    drawPanelStatus();
}

// =================== LED PANEL EFFECTS =====================

// Panel: 2D plasma
void panelPlasma() {
    static uint16_t pp = 0;
    float total = avgEnergy() + energy;
    pp += 2 + (uint16_t)(total * 10);
    if (total < 0.02f) { panelFade(20); return; }
    uint8_t bScale = (uint8_t)(total * 230);
    for (int y = 0; y < panelH; y++) {
        uint8_t yw1 = sin8(y * 16 + pp / 2);
        uint8_t yw2 = sin8(y * 24 - pp);
        for (int x = 0; x < PANEL_W; x++) {
            uint8_t xw1 = sin8(x * 20 + pp);
            uint8_t xw2 = sin8(x * 12 - pp / 2 + 64);
            uint8_t v = qadd8(qadd8(xw1/4, yw1/4), qadd8(xw2/4, yw2/4));
            uint8_t diag = sin8((x + y) * 14 + pp * 2);
            v = qadd8(v, diag / 4);
            int band = x * NUM_BANDS / PANEL_W;
            v = qadd8(v, (uint8_t)(bandDisp[min(band, NUM_BANDS-1)] * 60));
            uint8_t hue = v + (uint8_t)(pp / 3) + lastNote;
            uint8_t bri = scale8(v, bScale);
            panelSet(x, y, CHSV(hue, 230, bri));
        }
    }
}

// Panel: 2D fire rising from bottom
void panelFire() {
    static uint8_t pheat[PANEL_W][MAX_PANEL_H];
    float total = avgEnergy() + energy;
    if (total < 0.02f) { panelFade(15); return; }
    // cool down
    for (int x = 0; x < PANEL_W; x++)
        for (int y = 0; y < panelH; y++)
            pheat[x][y] = qsub8(pheat[x][y], random(6, 20));
    // ignite bottom row from MIDI
    if (energy > 0.05f) {
        for (int x = 0; x < PANEL_W; x++) {
            int band = x * NUM_BANDS / PANEL_W;
            uint8_t spark = (uint8_t)(bandDisp[min(band, NUM_BANDS-1)] * 200 + energy * 200);
            pheat[x][panelH - 1] = qadd8(pheat[x][panelH - 1], spark);
            if (random(3) == 0 && panelH > 1)
                pheat[x][panelH - 2] = qadd8(pheat[x][panelH - 2], spark / 2);
        }
    }
    // rise upward
    for (int x = 0; x < PANEL_W; x++)
        for (int y = 0; y < panelH - 1; y++) {
            int below = (x > 0 && x < PANEL_W - 1)
                ? ((int)pheat[x-1][y+1] + pheat[x][y+1] + pheat[x+1][y+1]) / 3
                : pheat[x][y+1];
            pheat[x][y] = max(0, below - (int)random(3));
        }
    // render heat to color
    for (int y = 0; y < panelH; y++)
        for (int x = 0; x < PANEL_W; x++) {
            uint8_t h = pheat[x][y];
            uint8_t hue = (h < 128) ? 0 : map(h, 128, 255, 0, 40);
            uint8_t sat = (h > 200) ? 180 : 255;
            panelSet(x, y, CHSV(hue, sat, h));
        }
}

// Panel: 2D spectrum bars (columns = bands)
void panelSpectrum() {
    panelFade(30);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    int colsPerBand = max(1, PANEL_W / NUM_BANDS);
    static uint8_t pFrame = 0;
    pFrame++;
    for (int b = 0; b < NUM_BANDS && b < PANEL_W; b++) {
        float lvl = bandDisp[b];
        int height = (int)(lvl * panelH);
        uint8_t hue = b * 16 + pFrame;
        for (int col = 0; col < colsPerBand; col++) {
            int x = b * colsPerBand + col;
            if (x >= PANEL_W) break;
            for (int row = 0; row < height && row < panelH; row++) {
                int y = panelH - 1 - row;
                uint8_t bri = 60 + (uint8_t)(row * 195 / panelH);
                panelSet(x, y, CHSV(hue + row * 3, 240, bri));
            }
            // white cap
            if (height > 0 && height <= panelH)
                panelSet(x, panelH - height, CRGB::White);
        }
    }
}

// Panel: 2D note rain (drops fall down columns)
void panelNoteRain() {
    // shift everything down one row
    for (int y = panelH - 1; y > 0; y--)
        for (int x = 0; x < PANEL_W; x++) {
            int dst = xyToIndex(x, y);
            int src = xyToIndex(x, y - 1);
            if (dst >= 0 && src >= 0) leds[dst] = leds[src];
        }
    // clear top row
    for (int x = 0; x < PANEL_W; x++) panelSet(x, 0, CRGB::Black);
    // spawn new drops on top row from active notes
    static uint32_t prevNC = 0;
    if (midiNoteCount != prevNC) {
        int col = lastNote % PANEL_W;
        uint8_t hue = (lastNote % 12) * 21;
        uint8_t bri = map(lastVel, 1, 127, 100, 255);
        panelSet(col, 0, CHSV(hue, 230, bri));
        // wider for hard hits
        if (lastVel > 80) {
            if (col > 0) panelSet(col - 1, 0, CHSV(hue + 10, 200, bri / 2));
            if (col < PANEL_W - 1) panelSet(col + 1, 0, CHSV(hue + 10, 200, bri / 2));
        }
        prevNC = midiNoteCount;
    }
    panelFade(8);
}

// Panel: 2D ripple rings expanding from note hit positions
void panelRipple() {
    panelFade(25);
    #define MAX_PRIPPLE 8
    struct PRipple { int cx, cy; float r, maxR; uint8_t hue; bool on; };
    static PRipple pr[MAX_PRIPPLE];
    static bool prInit = false;
    if (!prInit) { memset(pr, 0, sizeof(pr)); prInit = true; }

    static uint32_t prevNC2 = 0;
    if (midiNoteCount != prevNC2) {
        for (int i = 0; i < MAX_PRIPPLE; i++) {
            if (!pr[i].on) {
                pr[i].cx = lastNote % PANEL_W;
                pr[i].cy = (lastNote / PANEL_W) % panelH;
                pr[i].r = 0;
                pr[i].maxR = 3.0f + lastVel / 14.0f;
                pr[i].hue = (lastNote % 12) * 21;
                pr[i].on = true;
                break;
            }
        }
        prevNC2 = midiNoteCount;
    }

    for (int i = 0; i < MAX_PRIPPLE; i++) {
        if (!pr[i].on) continue;
        pr[i].r += 0.4f + energy * 1.5f;
        if (pr[i].r >= pr[i].maxR) { pr[i].on = false; continue; }
        float progress = pr[i].r / pr[i].maxR;
        uint8_t bri = (uint8_t)((1.0f - progress) * 255);
        int ir = (int)pr[i].r;
        for (int dy = -ir; dy <= ir; dy++) {
            for (int dx = -ir; dx <= ir; dx++) {
                int d2 = dx * dx + dy * dy;
                int ir2 = ir * ir;
                int irInner = (ir > 1) ? (ir - 1) * (ir - 1) : 0;
                if (d2 <= ir2 && d2 >= irInner) {
                    panelAdd(pr[i].cx + dx, pr[i].cy + dy,
                        CHSV(pr[i].hue, 230, bri));
                }
            }
        }
        // filled center on fresh ripples
        if (progress < 0.25f)
            panelAdd(pr[i].cx, pr[i].cy, CHSV(pr[i].hue, 180, bri));
    }
}

// Panel: 2D note map — chromatic piano grid
void panelNoteMap() {
    panelFade(30);
    for (int n = 0; n < 128; n++) {
        if (noteVel[n] == 0) continue;
        uint8_t hue = (n % 12) * 21;
        uint8_t bri = map(noteVel[n], 1, 127, 60, 255);
        // map note to 2D: x = note within octave, y = octave
        int x = (n % 12) * PANEL_W / 12;
        int y = (n / 12);
        if (y >= panelH) y = panelH - 1;
        panelSet(x, y, CHSV(hue, 230, bri));
        // glow around it
        panelAdd(x + 1, y, CHSV(hue, 200, bri / 3));
        panelAdd(x - 1, y, CHSV(hue, 200, bri / 3));
        panelAdd(x, y + 1, CHSV(hue, 200, bri / 3));
        panelAdd(x, y - 1, CHSV(hue, 200, bri / 3));
    }
    // white flash on latest hit
    if (energy > 0.1f) {
        int x = (lastNote % 12) * PANEL_W / 12;
        int y = lastNote / 12;
        if (y >= panelH) y = panelH - 1;
        panelSet(x, y, CRGB::White);
    }
}

// Panel: 2D pulse — expanding diamond/circle from center
void panelPulse() {
    panelFade(30);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    int cx = PANEL_W / 2, cy = panelH / 2;

    static float pRing = 0;
    static uint8_t pHue = 0;
    if (energy > 0.1f) { pRing = 0; pHue = (lastNote % 12) * 21; }
    pRing += 0.3f + total * 1.5f;
    if (pRing > PANEL_W) pRing = PANEL_W;

    int r = (int)pRing;
    float fade = 1.0f - pRing / PANEL_W;
    uint8_t bri = (uint8_t)(fade * total * 255);

    for (int y = 0; y < panelH; y++) {
        for (int x = 0; x < PANEL_W; x++) {
            int dist = abs(x - cx) + abs(y - cy); // manhattan diamond
            if (dist >= r - 1 && dist <= r + 1) {
                panelSet(x, y, CHSV(pHue + dist * 6, 230, bri));
            } else if (dist < r - 1 && energy > 0.2f) {
                uint8_t inner = (uint8_t)(energy * 40 * fade);
                panelAdd(x, y, CHSV(pHue + 40, 200, inner));
            }
        }
    }
    // center flash
    if (energy > 0.3f)
        panelSet(cx, cy, CRGB::White);
}

// Panel: 2D Chladni pattern
void panelChladni() {
    float total = avgEnergy() + energy;
    if (total < 0.02f) { panelFade(15); return; }
    static uint16_t cp = 0;
    cp += 1 + (uint16_t)(total * 6);
    float n = 1.0f + (lastNote % 6);
    float m = 1.0f + ((lastNote / 6) % 5);
    uint8_t baseHue = (uint8_t)(cp / 3) + lastNote * 2;
    uint8_t bScale = (uint8_t)(total * 250);

    for (int y = 0; y < panelH; y++) {
        float fy = (float)y / panelH;
        uint8_t yc1 = cos8((uint8_t)(fy * n * 128 + cp * 0.4f));
        uint8_t yc2 = cos8((uint8_t)(fy * m * 128 - cp * 0.25f));
        for (int x = 0; x < PANEL_W; x++) {
            float fx = (float)x / PANEL_W;
            uint8_t xc1 = cos8((uint8_t)(fx * n * 128 + cp * 0.3f));
            uint8_t xc2 = cos8((uint8_t)(fx * m * 128 - cp * 0.2f));
            int t1 = ((int)xc1 * yc2) / 256;
            int t2 = ((int)xc2 * yc1) / 256;
            int diff = abs(t1 - t2);
            uint8_t bri;
            if (diff < 30) bri = 255 - diff * 8;
            else bri = max(0, 30 - (diff - 30) / 3);
            bri = scale8(bri, bScale);
            if (bri < 3) { panelSet(x, y, CRGB::Black); continue; }
            panelSet(x, y, CHSV(baseHue + diff / 2, 210, bri));
        }
    }
}

// Panel: 2D geometry — rotating triangles on grid
void panelGeometry() {
    panelFade(35);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t gp = 0;
    gp += 2 + (uint16_t)(total * 8);
    int cx = PANEL_W / 2, cy = panelH / 2;
    uint8_t baseHue = (uint8_t)(gp / 4) + lastNote * 3;

    // rotating line arms from center
    for (int arm = 0; arm < 6; arm++) {
        uint8_t a = (uint8_t)(gp / 3 + arm * 43);
        int dx = (int8_t)(cos8(a) - 128);
        int dy = (int8_t)(sin8(a) - 128);
        int len = 2 + (int)(total * 6);
        for (int s = 0; s < len; s++) {
            int px = cx + dx * s / 128;
            int py = cy + dy * s / 128;
            panelAdd(px, py, CHSV(baseHue + arm * 42, 230,
                (uint8_t)(total * 200) - s * 15));
        }
    }

    // band-reactive dots in orbit
    for (int b = 0; b < NUM_BANDS; b++) {
        float lvl = bandDisp[b];
        if (lvl < 0.05f) continue;
        uint8_t a = (uint8_t)(b * 256 / NUM_BANDS + gp / 3);
        int dist = 2 + (int)(lvl * 5);
        int px = cx + (int8_t)(cos8(a) - 128) * dist / 128;
        int py = cy + (int8_t)(sin8(a) - 128) * dist / 128;
        panelAdd(px, py, CHSV(baseHue + b * 14, 230, (uint8_t)(lvl * 255)));
    }

    // center glow
    panelAdd(cx, cy, CHSV(baseHue, 150, (uint8_t)(total * 200)));
}

// Panel: mirror mode — left half renders, right half mirrors it
void panelMirror() {
    panelFade(25);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t mp = 0;
    mp += 2 + (uint16_t)(total * 8);
    int halfW = PANEL_W / 2;
    uint8_t baseHue = (uint8_t)(mp / 3) + lastNote * 2;

    // draw on left half from band data
    for (int b = 0; b < NUM_BANDS; b++) {
        float lvl = bandDisp[b];
        if (lvl < 0.03f) continue;
        int x = b * halfW / NUM_BANDS;
        int h = (int)(lvl * panelH);
        uint8_t hue = baseHue + b * 14;
        uint8_t bri = (uint8_t)(lvl * 255);
        for (int row = 0; row < h && row < panelH; row++) {
            int y = panelH - 1 - row;
            uint8_t rb = bri - row * 15;
            if (rb < 10) break;
            panelSet(x, y, CHSV(hue + row * 4, 230, rb));
        }
    }
    // energy sparkles on left
    if (energy > 0.1f) {
        int n = 2 + (int)(energy * 6);
        for (int i = 0; i < n; i++) {
            int sx = random(halfW);
            int sy = random(panelH);
            panelAdd(sx, sy, CHSV(baseHue + random(60), 200, (uint8_t)(energy * 200)));
        }
    }
    // mirror left → right
    for (int y = 0; y < panelH; y++) {
        for (int x = 0; x < halfW; x++) {
            int srcIdx = xyToIndex(x, y);
            int mirX = PANEL_W - 1 - x;
            if (srcIdx >= 0) panelSet(mirX, y, leds[srcIdx]);
        }
    }
}

// Panel: mirror wave — sine waves on left, mirrored right, MIDI modulated
void panelMirrorWave() {
    panelFade(20);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t mwp = 0;
    mwp += 2 + (uint16_t)(total * 10);
    int halfW = PANEL_W / 2;
    uint8_t baseHue = (uint8_t)(mwp / 4) + lastNote * 3;

    // layered sine waves on left half
    for (int x = 0; x < halfW; x++) {
        for (int layer = 0; layer < 3; layer++) {
            float freq = 1.5f + layer * 0.8f + (lastNote % 4) * 0.3f;
            uint8_t wave = sin8((uint8_t)(x * freq * 8 + mwp / (2 + layer)));
            int y = (int)((wave / 255.0f) * (panelH - 1));
            uint8_t hue = baseHue + layer * 60 + x * 3;
            uint8_t bri = 120 + (uint8_t)(total * 130);
            panelAdd(x, y, CHSV(hue, 230, bri));
            if (y > 0) panelAdd(x, y - 1, CHSV(hue, 200, bri / 3));
            if (y < panelH - 1) panelAdd(x, y + 1, CHSV(hue, 200, bri / 3));
        }
    }
    // note hit: bright horizontal bar on left
    if (energy > 0.15f) {
        int barY = lastNote % panelH;
        for (int x = 0; x < halfW; x++)
            panelAdd(x, barY, CHSV(baseHue + 80, 180, (uint8_t)(energy * 220)));
    }
    // mirror left → right
    for (int y = 0; y < panelH; y++)
        for (int x = 0; x < halfW; x++) {
            int srcIdx = xyToIndex(x, y);
            if (srcIdx >= 0) panelSet(PANEL_W - 1 - x, y, leds[srcIdx]);
        }
}

// Panel: rotating diamond — spinning diamond shape that pulses with MIDI
void panelDiamond() {
    panelFade(30);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t dp = 0;
    dp += 2 + (uint16_t)(total * 10);
    int cx = PANEL_W / 2, cy = panelH / 2;
    uint8_t baseHue = (uint8_t)(dp / 3) + lastNote * 2;

    // multiple rotating diamond layers
    for (int layer = 0; layer < 3; layer++) {
        int sz = 2 + layer * 2 + (int)(total * 3);
        uint8_t phase = (uint8_t)(dp / (2 + layer)) + layer * 85;
        int offX = (int8_t)(cos8(phase) - 128) * sz / 256;
        int offY = (int8_t)(sin8(phase) - 128) * sz / 256;
        uint8_t hue = baseHue + layer * 50;
        uint8_t bri = 150 + (uint8_t)(total * 100) - layer * 20;

        for (int y = 0; y < panelH; y++) {
            for (int x = 0; x < PANEL_W; x++) {
                int dx = abs(x - cx - offX);
                int dy = abs(y - cy - offY);
                int manhattan = dx + dy * (PANEL_W / panelH);
                if (manhattan >= sz - 1 && manhattan <= sz + 1) {
                    panelAdd(x, y, CHSV(hue + manhattan * 4, 230, bri));
                }
            }
        }
    }
    // center flash
    if (energy > 0.2f) {
        panelSet(cx, cy, CRGB::White);
        panelAdd(cx - 1, cy, CHSV(baseHue, 150, (uint8_t)(energy * 200)));
        panelAdd(cx + 1, cy, CHSV(baseHue, 150, (uint8_t)(energy * 200)));
    }
}

// Panel: hex grid — hexagonal cells that light up per band
void panelHexGrid() {
    panelFade(25);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t hp = 0;
    hp += 1 + (uint16_t)(total * 6);
    uint8_t baseHue = (uint8_t)(hp / 3) + lastNote;

    int cellW = 4;
    int cellH = 4;
    int cols = PANEL_W / cellW;
    int rows = panelH / cellH;

    for (int cr = 0; cr < rows; cr++) {
        for (int cc = 0; cc < cols; cc++) {
            int band = (cc * NUM_BANDS) / cols;
            if (band >= NUM_BANDS) band = NUM_BANDS - 1;
            float lvl = bandDisp[band];
            int ox = cc * cellW + (cr % 2) * (cellW / 2);
            int oy = cr * cellH;
            uint8_t hue = baseHue + cc * 12 + cr * 20;
            uint8_t bri = (uint8_t)(lvl * 220);
            bri = qadd8(bri, (uint8_t)(energy * 30));
            if (bri < 8) continue;
            // fill hex-ish cell (diamond within the cell)
            for (int dy = 0; dy < cellH; dy++) {
                int w = (dy < cellH / 2) ? (dy + 1) : (cellH - dy);
                int startX = ox + cellW / 2 - w;
                for (int dx = 0; dx < w * 2; dx++) {
                    int px = startX + dx;
                    int py = oy + dy;
                    if (px >= 0 && px < PANEL_W)
                        panelAdd(px, py, CHSV(hue, 230, bri));
                }
            }
        }
    }
}

// Panel: spiral — color spiral that winds outward from center
void panelSpiral() {
    panelFade(20);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t sp = 0;
    sp += 2 + (uint16_t)(total * 12);
    int cx = PANEL_W / 2, cy = panelH / 2;
    uint8_t baseHue = (uint8_t)(sp / 3) + lastNote * 2;

    float maxDist = sqrt(cx * cx + cy * cy);
    for (int y = 0; y < panelH; y++) {
        for (int x = 0; x < PANEL_W; x++) {
            int dx = x - cx;
            int dy = (y - cy) * PANEL_W / max(1, panelH);
            float dist = sqrt(dx * dx + dy * dy);
            // angle approximation using atan2-like approach with sin8
            uint8_t angle = (uint8_t)(atan2f(dy, dx) * 128.0f / 3.14159f) + 128;
            uint8_t spiral = angle + (uint8_t)(dist * 12) - (uint8_t)(sp / 2);
            uint8_t armVal = sin8(spiral);

            int bandIdx = (int)(dist * NUM_BANDS / maxDist);
            if (bandIdx >= NUM_BANDS) bandIdx = NUM_BANDS - 1;
            float bandLvl = bandDisp[bandIdx];

            uint8_t bri = scale8(armVal, (uint8_t)(total * 200));
            bri = qadd8(bri, (uint8_t)(bandLvl * 80));
            if (bri < 6) continue;
            uint8_t hue = baseHue + angle + (uint8_t)(dist * 4);
            panelSet(x, y, CHSV(hue, 230, bri));
        }
    }
    // center glow on hits
    if (energy > 0.2f) panelSet(cx, cy, CRGB::White);
}

// Panel: matrix rain — vertical streams of color falling, one per column
void panelMatrix() {
    // shift down
    for (int y = panelH - 1; y > 0; y--)
        for (int x = 0; x < PANEL_W; x++) {
            int dst = xyToIndex(x, y);
            int src = xyToIndex(x, y - 1);
            if (dst >= 0 && src >= 0) {
                leds[dst] = leds[src];
                leds[dst].fadeToBlackBy(25);
            }
        }
    // clear top row
    for (int x = 0; x < PANEL_W; x++) panelSet(x, 0, CRGB::Black);

    float total = avgEnergy() + energy;
    static uint8_t streamActive[32]; // per-column stream state
    static uint8_t streamHue[32];

    // spawn new streams on note hits
    static uint32_t prevNC3 = 0;
    if (midiNoteCount != prevNC3) {
        int col = lastNote % PANEL_W;
        streamActive[col] = 6 + lastVel / 20;
        streamHue[col] = (lastNote % 12) * 21;
        prevNC3 = midiNoteCount;
    }

    // random streams from energy
    if (energy > 0.1f && random(4) == 0) {
        int col = random(PANEL_W);
        streamActive[col] = 3 + random(5);
        streamHue[col] = random(256);
    }

    for (int x = 0; x < PANEL_W; x++) {
        if (streamActive[x] > 0) {
            panelSet(x, 0, CHSV(streamActive[x] > 3 ? streamHue[x] : streamHue[x] + 40,
                220, 180 + streamActive[x] * 10));
            // bright head
            if (streamActive[x] > 4) panelSet(x, 0, CRGB(200, 255, 200));
            streamActive[x]--;
        }
    }
}

// Panel: kaleidoscope — 4-way mirrored from top-left quadrant
void panelKaleidoscope() {
    panelFade(22);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t kp = 0;
    kp += 2 + (uint16_t)(total * 8);
    int halfW = PANEL_W / 2;
    int halfH = panelH / 2;
    uint8_t baseHue = (uint8_t)(kp / 3) + lastNote * 3;

    // render top-left quadrant from bands + energy
    for (int y = 0; y < halfH; y++) {
        for (int x = 0; x < halfW; x++) {
            int band = (x + y) * NUM_BANDS / (halfW + halfH);
            if (band >= NUM_BANDS) band = NUM_BANDS - 1;
            float lvl = bandDisp[band];

            uint8_t wave = sin8(x * 16 + y * 20 + kp);
            uint8_t wave2 = sin8(x * 10 - y * 14 + kp / 2);
            uint8_t combined = qadd8(wave / 3, wave2 / 3);
            combined = qadd8(combined, (uint8_t)(lvl * 120));
            uint8_t bri = scale8(combined, (uint8_t)(total * 240));
            if (bri < 5) continue;
            CHSV col = CHSV(baseHue + combined / 2 + x * 3 + y * 4, 230, bri);
            panelSet(x, y, col);
        }
    }
    // sparkles on hits in top-left
    if (energy > 0.15f) {
        int n = 1 + (int)(energy * 4);
        for (int i = 0; i < n; i++)
            panelSet(random(halfW), random(halfH),
                CHSV(baseHue + random(80), 200, (uint8_t)(energy * 255)));
    }
    // mirror 4-way: TL→TR, TL→BL, TL→BR
    for (int y = 0; y < halfH; y++) {
        for (int x = 0; x < halfW; x++) {
            int srcIdx = xyToIndex(x, y);
            if (srcIdx < 0) continue;
            CRGB c = leds[srcIdx];
            panelSet(PANEL_W - 1 - x, y, c);                 // TR
            panelSet(x, panelH - 1 - y, c);                 // BL
            panelSet(PANEL_W - 1 - x, panelH - 1 - y, c);  // BR
        }
    }
}

// Panel: scanner — Larson scanner / cylon eye, 2D version sweeping across
void panelScanner() {
    panelFade(35);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static float scanPos = 0;
    static int8_t scanDir = 1;
    static uint8_t scanHue = 0;

    float speed = 0.3f + total * 2.0f;
    scanPos += speed * scanDir;
    if (scanPos >= PANEL_W - 1) { scanPos = PANEL_W - 1; scanDir = -1; }
    if (scanPos <= 0) { scanPos = 0; scanDir = 1; }

    if (energy > 0.1f) scanHue = (lastNote % 12) * 21;

    int px = (int)scanPos;
    uint8_t bri = 120 + (uint8_t)(total * 130);
    // bright column at scan position
    for (int y = 0; y < panelH; y++) {
        int band = y * NUM_BANDS / panelH;
        uint8_t yBri = bri;
        yBri = qadd8(yBri, (uint8_t)(bandDisp[min(band, NUM_BANDS - 1)] * 60));
        panelSet(px, y, CHSV(scanHue + y * 8, 230, yBri));
        // glow wings
        if (px > 0) panelAdd(px - 1, y, CHSV(scanHue + 20, 200, yBri / 2));
        if (px < PANEL_W - 1) panelAdd(px + 1, y, CHSV(scanHue + 20, 200, yBri / 2));
        if (px > 1) panelAdd(px - 2, y, CHSV(scanHue + 40, 180, yBri / 4));
        if (px < PANEL_W - 2) panelAdd(px + 2, y, CHSV(scanHue + 40, 180, yBri / 4));
    }
    // white flash on hard hits
    if (energy > 0.4f) {
        for (int y = 0; y < panelH; y++)
            panelSet(px, y, CRGB::White);
    }
}

// Panel: heartbeat — pulsing shape from center that expands/contracts with MIDI
void panelHeartbeat() {
    panelFade(30);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static float beatPhase = 0;
    static uint8_t beatHue = 0;
    beatPhase += 0.05f + total * 0.3f;
    if (energy > 0.15f) beatHue = (lastNote % 12) * 21;

    int cx = PANEL_W / 2, cy = panelH / 2;
    float pulse = (sin8((uint8_t)(beatPhase * 40)) / 255.0f);
    float sz = 1.0f + pulse * 2.0f + total * 3.0f;

    for (int y = 0; y < panelH; y++) {
        for (int x = 0; x < PANEL_W; x++) {
            float dx = (float)(x - cx);
            float dy = (float)(y - cy) * (float)PANEL_W / (float)panelH;
            float dist = sqrt(dx * dx + dy * dy);
            if (dist <= sz + 1.0f) {
                float t = dist / (sz + 1.0f);
                uint8_t bri = (uint8_t)((1.0f - t) * (120 + total * 135));
                panelAdd(x, y, CHSV(beatHue + (uint8_t)(dist * 10), 220, bri));
            }
            // ring at edge
            if (dist >= sz - 0.5f && dist <= sz + 0.5f) {
                panelAdd(x, y, CHSV(beatHue + 60, 200, (uint8_t)(total * 200)));
            }
        }
    }
    if (energy > 0.3f) panelSet(cx, cy, CRGB::White);
}

// Panel: VU towers — each column is a VU meter, mirrored from center
void panelVUTowers() {
    panelFade(30);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint8_t vuFrame = 0;
    vuFrame++;
    int halfW = PANEL_W / 2;

    // left half: band VU meters
    for (int x = 0; x < halfW; x++) {
        int band = x * NUM_BANDS / halfW;
        if (band >= NUM_BANDS) band = NUM_BANDS - 1;
        float lvl = bandDisp[band];
        int h = (int)(lvl * panelH);
        uint8_t hue = vuFrame + band * 16;
        for (int row = 0; row < h && row < panelH; row++) {
            int y = panelH - 1 - row;
            uint8_t bri = 60 + row * 25;
            if (bri > 255) bri = 255;
            panelSet(x, y, CHSV(hue + row * 6, 240, bri));
        }
        // white peak
        if (h > 0 && h <= panelH)
            panelSet(x, panelH - h, CRGB::White);
    }
    // mirror left → right
    for (int y = 0; y < panelH; y++)
        for (int x = 0; x < halfW; x++) {
            int srcIdx = xyToIndex(x, y);
            if (srcIdx >= 0) panelSet(PANEL_W - 1 - x, y, leds[srcIdx]);
        }
}

// Panel: crosshatch — diagonal lines that intensify with MIDI
void panelCrosshatch() {
    panelFade(25);
    float total = avgEnergy() + energy;
    if (total < 0.01f) return;
    static uint16_t chp = 0;
    chp += 2 + (uint16_t)(total * 10);
    uint8_t baseHue = (uint8_t)(chp / 3) + lastNote;

    int spacing = max(2, 6 - (int)(total * 3));
    uint8_t bri = 60 + (uint8_t)(total * 190);

    // diagonal lines (\)
    for (int d = -panelH; d < PANEL_W + panelH; d += spacing) {
        int phase = (d + (int)(chp / 4)) % (spacing * 2);
        if (phase < 0) phase += spacing * 2;
        if (phase >= spacing) continue;
        for (int s = 0; s < panelH; s++) {
            int x = d + s;
            int y = s;
            if (x >= 0 && x < PANEL_W)
                panelAdd(x, y, CHSV(baseHue + d * 3, 230, bri));
        }
    }
    // diagonal lines (/)
    for (int d = -panelH; d < PANEL_W + panelH; d += spacing) {
        int phase = (d + (int)(chp / 5)) % (spacing * 2);
        if (phase < 0) phase += spacing * 2;
        if (phase >= spacing) continue;
        for (int s = 0; s < panelH; s++) {
            int x = d + (panelH - 1 - s);
            int y = s;
            if (x >= 0 && x < PANEL_W)
                panelAdd(x, y, CHSV(baseHue + 80 + d * 3, 230, bri));
        }
    }
    // intersection brightening on hits
    if (energy > 0.2f) {
        for (int y = 0; y < panelH; y++)
            for (int x = 0; x < PANEL_W; x++) {
                int idx = xyToIndex(x, y);
                if (idx >= 0 && leds[idx].getLuma() > 100)
                    leds[idx] |= CHSV(0, 0, (uint8_t)(energy * 120));
            }
    }
}

// Panel: Conway-ish — cellular automata-inspired, cells born from MIDI
void panelLife() {
    static uint8_t grid[PANEL_W][MAX_PANEL_H];
    static uint8_t gridHue[PANEL_W][MAX_PANEL_H];
    float total = avgEnergy() + energy;

    // inject cells from notes
    static uint32_t prevNC4 = 0;
    if (midiNoteCount != prevNC4) {
        int cx = lastNote % PANEL_W;
        int cy = (lastNote / PANEL_W) % panelH;
        grid[cx][cy] = 255;
        gridHue[cx][cy] = (lastNote % 12) * 21;
        // neighbors
        for (int d = -1; d <= 1; d++) {
            int nx = (cx + d + PANEL_W) % PANEL_W;
            int ny = (cy + d + panelH) % panelH;
            grid[nx][cy] = qadd8(grid[nx][cy], lastVel);
            gridHue[nx][cy] = gridHue[cx][cy] + 30;
            grid[cx][ny] = qadd8(grid[cx][ny], lastVel);
            gridHue[cx][ny] = gridHue[cx][cy] + 30;
        }
        prevNC4 = midiNoteCount;
    }

    // spread and decay
    static uint8_t lifeFrame = 0;
    lifeFrame++;
    if ((lifeFrame % 3) == 0) {
        uint8_t tmp[PANEL_W][MAX_PANEL_H];
        memcpy(tmp, grid, sizeof(grid));
        for (int y = 0; y < panelH; y++) {
            for (int x = 0; x < PANEL_W; x++) {
                int sum = 0;
                for (int dy = -1; dy <= 1; dy++)
                    for (int dx = -1; dx <= 1; dx++) {
                        if (dx == 0 && dy == 0) continue;
                        int nx = (x + dx + PANEL_W) % PANEL_W;
                        int ny = (y + dy + panelH) % panelH;
                        sum += tmp[nx][ny];
                    }
                int avg = sum / 8;
                if (avg > 20 && grid[x][y] < 200) grid[x][y] = qadd8(grid[x][y], avg / 4);
                grid[x][y] = qsub8(grid[x][y], 6);
            }
        }
    }

    for (int y = 0; y < panelH; y++)
        for (int x = 0; x < PANEL_W; x++) {
            if (grid[x][y] > 3)
                panelSet(x, y, CHSV(gridHue[x][y], 220, grid[x][y]));
            else
                panelSet(x, y, CRGB::Black);
        }
}

// Panel: COMET TRAIL — bright comet orbits the panel edges
void panelCometTrail() {
    panelFade(30);
    float total = avgEnergy() + energy;
    if (total < 0.02f) return;
    static uint16_t cPos = 0;
    cPos += 2 + (uint16_t)(total * 8);
    int perimeter = (PANEL_W + panelH) * 2 - 4;
    int head = cPos % perimeter;
    for (int t = 0; t < 8; t++) {
        int p = (head - t + perimeter) % perimeter;
        int x, y;
        if (p < PANEL_W) { x = p; y = 0; }
        else if (p < PANEL_W + panelH - 1) { x = PANEL_W - 1; y = p - PANEL_W + 1; }
        else if (p < PANEL_W * 2 + panelH - 2) { x = PANEL_W - 1 - (p - PANEL_W - panelH + 2); y = panelH - 1; }
        else { x = 0; y = panelH - 1 - (p - PANEL_W * 2 - panelH + 3); }
        uint8_t bri = 255 - t * 30;
        panelSet(x, y, CHSV(lastNote * 2 + t * 8, 255, scale8(bri, (uint8_t)(total * 255))));
    }
}

// Panel: RAIN DROP — columns fill from top on note hits, drain down
void panelRainDrop() {
    static uint8_t colFill[PANEL_W];
    float total = avgEnergy() + energy;
    panelFade(15);
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        int col = lastNote % PANEL_W;
        colFill[col] = min(255, (int)colFill[col] + lastVel * 2);
        if (col > 0) colFill[col - 1] = qadd8(colFill[col - 1], lastVel);
        if (col < PANEL_W - 1) colFill[col + 1] = qadd8(colFill[col + 1], lastVel);
        prevN = midiNoteCount;
    }
    for (int x = 0; x < PANEL_W; x++) {
        if (colFill[x] > 0) {
            int h = 1 + colFill[x] * panelH / 256;
            if (h > panelH) h = panelH;
            uint8_t hue = (lastNote % 12) * 21 + x * 4;
            for (int y = 0; y < h; y++)
                panelAdd(x, y, CHSV(hue, 240, colFill[x] - y * 20));
            colFill[x] = qsub8(colFill[x], 4);
        }
    }
}

// Panel: STARFIELD — points fly outward from center
void panelStarfield() {
    panelFade(40);
    float total = avgEnergy() + energy;
    if (total < 0.02f) return;
    static float sx[16], sy[16], sdx[16], sdy[16];
    static uint8_t shue[16];
    static bool sinit = false;
    if (!sinit) { memset(sx, 0, sizeof(sx)); sinit = true; }
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        for (int i = 0; i < 16; i++) {
            if (sx[i] == 0 && sy[i] == 0) {
                sx[i] = PANEL_W / 2.0f;
                sy[i] = panelH / 2.0f;
                float angle = (lastNote * 20 + i * 16) % 256;
                sdx[i] = (int8_t)(cos8((uint8_t)angle) - 128) / 128.0f * (0.5f + lastVel / 100.0f);
                sdy[i] = (int8_t)(sin8((uint8_t)angle) - 128) / 128.0f * (0.5f + lastVel / 100.0f);
                shue[i] = (lastNote % 12) * 21;
                break;
            }
        }
        prevN = midiNoteCount;
    }
    for (int i = 0; i < 16; i++) {
        if (sx[i] == 0 && sy[i] == 0) continue;
        sx[i] += sdx[i];
        sy[i] += sdy[i];
        sdx[i] *= 1.05f;
        sdy[i] *= 1.05f;
        if (sx[i] < -1 || sx[i] >= PANEL_W + 1 || sy[i] < -1 || sy[i] >= panelH + 1) {
            sx[i] = sy[i] = 0;
            continue;
        }
        panelSet((int)sx[i], (int)sy[i], CHSV(shue[i], 255, 220));
    }
}

// Panel: WAVE — sine wave scrolls horizontally, amplitude from MIDI
void panelWave() {
    panelFade(35);
    float total = avgEnergy() + energy;
    if (total < 0.02f) return;
    static uint16_t wavePhase = 0;
    wavePhase += 3 + (uint16_t)(total * 10);
    float amp = total * (panelH / 2.0f - 0.5f);
    float midY = panelH / 2.0f;
    for (int x = 0; x < PANEL_W; x++) {
        uint8_t s = sin8((uint8_t)(x * 20 + wavePhase));
        float fy = midY + (int8_t)(s - 128) / 128.0f * amp;
        int y = (int)fy;
        if (y >= 0 && y < panelH) {
            int band = x * NUM_BANDS / PANEL_W;
            uint8_t hue = (uint8_t)(wavePhase / 4) + x * 6;
            uint8_t bri = 120 + (uint8_t)(bandDisp[min(band, NUM_BANDS - 1)] * 135);
            panelSet(x, y, CHSV(hue, 240, bri));
            if (y > 0) panelAdd(x, y - 1, CHSV(hue, 240, bri / 3));
            if (y < panelH - 1) panelAdd(x, y + 1, CHSV(hue, 240, bri / 3));
        }
    }
}

// Panel: FIREWORKS — bursts explode from note hits
#define MAX_FW 6
struct Firework { float px[12], py[12], vx[12], vy[12]; uint8_t hue; uint8_t life; bool on; };
static Firework fw[MAX_FW];
void panelFireworks() {
    panelFade(25);
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        for (int i = 0; i < MAX_FW; i++) {
            if (!fw[i].on) {
                fw[i].on = true;
                fw[i].hue = (lastNote % 12) * 21;
                fw[i].life = 40 + lastVel / 3;
                float cx = 2 + lastNote % (PANEL_W - 4);
                float cy = 1 + (lastVel / 32) % max(1, panelH - 2);
                for (int p = 0; p < 12; p++) {
                    fw[i].px[p] = cx;
                    fw[i].py[p] = cy;
                    uint8_t a = p * 21;
                    fw[i].vx[p] = (int8_t)(cos8(a) - 128) / 128.0f * 0.8f;
                    fw[i].vy[p] = (int8_t)(sin8(a) - 128) / 128.0f * 0.5f;
                }
                break;
            }
        }
        prevN = midiNoteCount;
    }
    for (int i = 0; i < MAX_FW; i++) {
        if (!fw[i].on) continue;
        fw[i].life--;
        if (fw[i].life == 0) { fw[i].on = false; continue; }
        uint8_t bri = fw[i].life * 6;
        for (int p = 0; p < 12; p++) {
            fw[i].px[p] += fw[i].vx[p];
            fw[i].py[p] += fw[i].vy[p];
            fw[i].vy[p] += 0.02f;
            int x = (int)fw[i].px[p], y = (int)fw[i].py[p];
            if (x >= 0 && x < PANEL_W && y >= 0 && y < panelH)
                panelAdd(x, y, CHSV(fw[i].hue + p * 8, 255, bri));
        }
    }
}

// Panel: BOUNCE — balls bounce around, spawned by notes
void panelBounce() {
    panelFade(30);
    static float bx[8], by[8], bdx[8], bdy[8];
    static uint8_t bhue[8];
    static bool bon[8] = {};
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        for (int i = 0; i < 8; i++) {
            if (!bon[i]) {
                bon[i] = true;
                bx[i] = PANEL_W / 2.0f;
                by[i] = panelH / 2.0f;
                bdx[i] = (lastNote % 2 ? 0.5f : -0.5f) + lastVel / 200.0f;
                bdy[i] = (lastNote % 3 ? 0.3f : -0.3f);
                bhue[i] = (lastNote % 12) * 21;
                break;
            }
        }
        prevN = midiNoteCount;
    }
    for (int i = 0; i < 8; i++) {
        if (!bon[i]) continue;
        bx[i] += bdx[i];
        by[i] += bdy[i];
        if (bx[i] <= 0 || bx[i] >= PANEL_W - 1) bdx[i] = -bdx[i];
        if (by[i] <= 0 || by[i] >= panelH - 1) bdy[i] = -bdy[i];
        bx[i] = constrain(bx[i], 0, PANEL_W - 1);
        by[i] = constrain(by[i], 0, panelH - 1);
        panelSet((int)bx[i], (int)by[i], CHSV(bhue[i], 255, 220));
        bhue[i]++;
        if (avgEnergy() < 0.01f && energy < 0.01f) bon[i] = false;
    }
}

// Panel: SNAKE — growing snake that turns on note hits
void panelSnake() {
    panelFade(15);
    static int snakeX[64], snakeY[64];
    static int sLen = 1, sDir = 0;
    static uint8_t sHue = 0;
    static bool sInit = false;
    if (!sInit) { snakeX[0] = PANEL_W / 2; snakeY[0] = panelH / 2; sInit = true; }
    float total = avgEnergy() + energy;
    if (total < 0.02f) return;
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        sDir = (sDir + 1 + (lastNote % 3)) % 4;
        if (sLen < 60) sLen++;
        sHue = (lastNote % 12) * 21;
        prevN = midiNoteCount;
    }
    int dx[] = {1, 0, -1, 0};
    int dy[] = {0, 1, 0, -1};
    for (int i = sLen - 1; i > 0; i--) { snakeX[i] = snakeX[i - 1]; snakeY[i] = snakeY[i - 1]; }
    snakeX[0] += dx[sDir];
    snakeY[0] += dy[sDir];
    if (snakeX[0] < 0) snakeX[0] = PANEL_W - 1;
    if (snakeX[0] >= PANEL_W) snakeX[0] = 0;
    if (snakeY[0] < 0) snakeY[0] = panelH - 1;
    if (snakeY[0] >= panelH) snakeY[0] = 0;
    for (int i = 0; i < sLen; i++) {
        uint8_t bri = 255 - i * 4;
        if (bri < 20) break;
        panelSet(snakeX[i], snakeY[i], CHSV(sHue + i * 3, 240, bri));
    }
}

// Panel: WATERFALL2D — columns cascade downward from top, band-reactive
void panelWaterfall2D() {
    for (int y = panelH - 1; y > 0; y--)
        for (int x = 0; x < PANEL_W; x++) {
            int from = xyToIndex(x, y - 1);
            int to = xyToIndex(x, y);
            if (from >= 0 && to >= 0) leds[to] = leds[from];
        }
    float total = avgEnergy() + energy;
    for (int x = 0; x < PANEL_W; x++) {
        int band = x * NUM_BANDS / PANEL_W;
        if (band >= NUM_BANDS) band = NUM_BANDS - 1;
        float lvl = bandDisp[band] * 0.7f + energy * 0.3f;
        if (lvl > 0.03f) {
            uint8_t hue = (lastNote % 12) * 21 + x * 3;
            uint8_t bri = (uint8_t)(lvl * 255);
            panelSet(x, 0, CHSV(hue, 240, bri));
        } else {
            panelSet(x, 0, CRGB::Black);
        }
    }
    fadeToBlackBy(leds, numLeds, 10);
}

// Panel: RADAR — rotating sweep line with MIDI blips
void panelRadar() {
    panelFade(12);
    float total = avgEnergy() + energy;
    if (total < 0.02f) { panelFade(8); return; }
    static uint8_t radarAngle = 0;
    radarAngle += 2 + (uint8_t)(total * 6);
    int cx = PANEL_W / 2, cy = panelH / 2;
    for (int d = 0; d < max(PANEL_W, panelH); d++) {
        int x = cx + (int)((int8_t)(cos8(radarAngle) - 128) * d / 128);
        int y = cy + (int)((int8_t)(sin8(radarAngle) - 128) * d / 128);
        if (x >= 0 && x < PANEL_W && y >= 0 && y < panelH)
            panelAdd(x, y, CHSV(96, 255, 160));
    }
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        int bx = lastNote % PANEL_W;
        int by = (lastVel / 16) % panelH;
        panelSet(bx, by, CHSV((lastNote % 12) * 21, 255, 255));
        if (bx > 0) panelAdd(bx - 1, by, CHSV((lastNote % 12) * 21, 255, 120));
        if (bx < PANEL_W - 1) panelAdd(bx + 1, by, CHSV((lastNote % 12) * 21, 255, 120));
        prevN = midiNoteCount;
    }
}

// Panel: TETRIS — blocks fall and stack from MIDI
void panelTetris() {
    static uint8_t stack[PANEL_W];
    static float dropX, dropY, dropSpd;
    static uint8_t dropHue;
    static bool dropping = false;
    panelFade(8);
    float total = avgEnergy() + energy;
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN && !dropping) {
        dropping = true;
        dropX = lastNote % PANEL_W;
        dropY = 0;
        dropSpd = 0.2f + lastVel / 300.0f;
        dropHue = (lastNote % 12) * 21;
        prevN = midiNoteCount;
    }
    if (dropping) {
        dropY += dropSpd;
        int targetY = panelH - 1 - stack[(int)dropX];
        if ((int)dropY >= targetY) {
            dropping = false;
            int sx = (int)dropX;
            if (stack[sx] < panelH) stack[sx]++;
            panelSet(sx, targetY, CHSV(dropHue, 255, 255));
        } else {
            panelSet((int)dropX, (int)dropY, CHSV(dropHue, 255, 240));
        }
    }
    for (int x = 0; x < PANEL_W; x++) {
        for (int s = 0; s < (int)stack[x] && s < panelH; s++) {
            int y = panelH - 1 - s;
            panelAdd(x, y, CHSV(x * 8 + s * 20, 220, 80 + s * 15));
        }
    }
    if (total < 0.01f) {
        for (int x = 0; x < PANEL_W; x++)
            if (stack[x] > 0) stack[x]--;
    }
}

// Panel: CHLADNI CYBER — Chladni nodal patterns in pink/purple/blue palette
void panelChladniCyber() {
    float total = avgEnergy() + energy;
    if (total < 0.02f) { panelFade(15); return; }
    static uint16_t cp = 0;
    cp += 1 + (uint16_t)(total * 5);
    float n = 2.0f + (lastNote % 5);
    float m = 1.0f + ((lastNote / 5) % 4);
    uint8_t bScale = (uint8_t)(total * 250);

    for (int y = 0; y < panelH; y++) {
        float fy = (float)y / panelH;
        uint8_t yc1 = cos8((uint8_t)(fy * n * 128 + cp * 0.35f));
        uint8_t yc2 = cos8((uint8_t)(fy * m * 128 - cp * 0.2f));
        for (int x = 0; x < PANEL_W; x++) {
            float fx = (float)x / PANEL_W;
            uint8_t xc1 = cos8((uint8_t)(fx * n * 128 + cp * 0.25f));
            uint8_t xc2 = cos8((uint8_t)(fx * m * 128 - cp * 0.15f));
            int t1 = ((int)xc1 * yc2) / 256;
            int t2 = ((int)xc2 * yc1) / 256;
            int diff = abs(t1 - t2);
            uint8_t bri;
            if (diff < 35) bri = 255 - diff * 7;
            else bri = max(0, 40 - (diff - 35) / 2);
            bri = scale8(bri, bScale);
            if (bri < 3) { panelSet(x, y, CRGB::Black); continue; }
            uint8_t hue = 190 + scale8((uint8_t)(diff + cp / 4), 55);
            panelSet(x, y, CHSV(hue, 220, bri));
        }
    }
}

// Panel: CHLADNI MORPH — Chladni patterns that morph between harmonic modes
void panelChladniMorph() {
    float total = avgEnergy() + energy;
    if (total < 0.02f) { panelFade(12); return; }
    static uint16_t cp = 0;
    static float modeN = 2.0f, modeM = 3.0f;
    static float targN = 2.0f, targM = 3.0f;
    cp += 1 + (uint16_t)(total * 6);
    static uint32_t prevN = 0;
    if (midiNoteCount != prevN) {
        targN = 1.0f + (lastNote % 7);
        targM = 1.0f + ((lastVel / 18) % 6);
        prevN = midiNoteCount;
    }
    modeN += (targN - modeN) * 0.02f;
    modeM += (targM - modeM) * 0.02f;
    uint8_t bScale = (uint8_t)(total * 250);
    uint8_t baseHue = (uint8_t)(cp / 5) + lastNote * 3;

    for (int y = 0; y < panelH; y++) {
        float fy = (float)y / panelH;
        uint8_t yc1 = sin8((uint8_t)(fy * modeN * 128 + cp * 0.3f));
        uint8_t yc2 = cos8((uint8_t)(fy * modeM * 128 - cp * 0.2f));
        for (int x = 0; x < PANEL_W; x++) {
            float fx = (float)x / PANEL_W;
            uint8_t xc1 = sin8((uint8_t)(fx * modeN * 128 + cp * 0.25f));
            uint8_t xc2 = cos8((uint8_t)(fx * modeM * 128 - cp * 0.15f));
            int p1 = ((int)xc1 * yc1 + (int)xc2 * yc2) / 512;
            int p2 = ((int)xc1 * yc2 - (int)xc2 * yc1) / 512;
            int diff = abs(p1 - p2);
            uint8_t bri;
            if (diff < 25) bri = 255 - diff * 10;
            else bri = max(0, 50 - (diff - 25));
            bri = scale8(bri, bScale);
            if (bri < 3) { panelSet(x, y, CRGB::Black); continue; }
            panelSet(x, y, CHSV(baseHue + diff, 230, bri));
        }
    }
}

// Panel: NOTE BARS — each active MIDI note gets its own vertical bar column
void panelNoteBars() {
    panelFade(25);
    for (int n = 0; n < 128; n++) {
        if (noteVel[n] == 0) continue;
        uint8_t hue = (n % 12) * 21;
        uint8_t bri = map(noteVel[n], 1, 127, 80, 255);
        int x = (n - 36);
        if (x < 0 || x >= PANEL_W) continue;
        int barH = 1 + noteVel[n] * (panelH - 1) / 127;
        for (int y = panelH - 1; y >= panelH - barH && y >= 0; y--) {
            uint8_t rowBri = bri - (panelH - 1 - y) * 15;
            if (rowBri < 10) rowBri = 10;
            panelSet(x, y, CHSV(hue, 240, rowBri));
        }
        int topY = panelH - barH;
        if (topY >= 0 && topY < panelH)
            panelSet(x, topY, CHSV(hue, 150, 255));
    }
}

// Panel: CYBERPUNK — pink/purple/blue only gradient plasma
void panelCyberpunk() {
    static uint16_t cp = 0;
    float total = avgEnergy() + energy;
    cp += 2 + (uint16_t)(total * 10);
    if (total < 0.02f) { panelFade(20); return; }
    for (int y = 0; y < panelH; y++) {
        uint8_t yw = sin8(y * 18 + cp / 2);
        for (int x = 0; x < PANEL_W; x++) {
            uint8_t xw = sin8(x * 22 + cp);
            uint8_t d  = sin8((x + y) * 14 + cp * 2);
            uint8_t v  = qadd8(qadd8(xw / 3, yw / 3), d / 3);
            int band = x * NUM_BANDS / PANEL_W;
            v = qadd8(v, (uint8_t)(bandDisp[min(band, NUM_BANDS - 1)] * 80));
            // Map v to hue range 190–240 (blue 160 → purple 192 → pink 224)
            uint8_t hue = 190 + scale8(v, 50);
            uint8_t bri = scale8(v, (uint8_t)(total * 255));
            panelSet(x, y, CHSV(hue, 230, bri));
        }
    }
}

// Panel dispatch: routes ledPreset index to the corresponding effect
void renderPanel() {
    switch (ledPreset) {
        case 0:  panelPlasma();       break;
        case 1:  panelVUTowers();     break;
        case 2:  panelScanner();      break;
        case 3:  panelKaleidoscope(); break;
        case 4:  panelFire();         break;
        case 5:  panelPulse();        break;
        case 6:  panelMirrorWave();   break;
        case 7:  panelMatrix();       break;
        case 8:  panelSpiral();       break;
        case 9:  panelHeartbeat();    break;
        case 10: panelNoteRain();     break;
        case 11: panelGeometry();     break;
        case 12: panelLife();         break;
        case 13: panelChladni();      break;
        case 14: panelDiamond();      break;
        case 15: panelCrosshatch();   break;
        case 16: panelMirror();       break;
        case 17: panelSpectrum();     break;
        case 18: panelNoteMap();      break;
        case 19: panelRipple();       break;
        case 20: panelHexGrid();      break;
        case 21: panelNoteMap();      break;
        case 22: panelCometTrail();   break;
        case 23: panelRainDrop();     break;
        case 24: panelStarfield();    break;
        case 25: panelWave();         break;
        case 26: panelFireworks();    break;
        case 27: panelBounce();       break;
        case 28: panelSnake();        break;
        case 29: panelWaterfall2D();  break;
        case 30: panelRadar();        break;
        case 31: panelTetris();       break;
        case 32: panelCyberpunk();    break;
        case 33: panelChladniCyber(); break;
        case 34: panelChladniMorph(); break;
        case 35: panelNoteBars();     break;
    }
}

void updateLEDs() {
    renderPanel();
    FastLED.show();
}

// ====================== BUTTONS ============================

static unsigned long lastBriChange = 0;
static bool btnBConsumed = false;

static unsigned long lastTransSwitch = 0;
static uint16_t transBeatCount = 0;
static uint8_t transBeatsToSwitch = 4;
static float transPrevE = 0;
static float transSmoothedE = 0;
static float transPeakE = 0;
static unsigned long transLastBeat = 0;
static bool transBeatFlag = false;
static float transAvgInterval = 500;

static const uint8_t transHigh[]  = {0,2,4,5,8,10,22,24,25,26,27,31,35};
static const uint8_t transMid[]   = {3,6,7,9,11,12,16,17,18,20,28,30,33,34};
static const uint8_t transLow[]   = {1,13,14,15,23,29,32};
#define TR_HIGH_N sizeof(transHigh)
#define TR_MID_N  sizeof(transMid)
#define TR_LOW_N  sizeof(transLow)

void transDoSwitch(float level) {
    int prev = ledPreset;
    const uint8_t *pool; uint8_t poolN;
    if (level > 0.45f) { pool = transHigh; poolN = TR_HIGH_N; }
    else if (level > 0.15f) { pool = transMid; poolN = TR_MID_N; }
    else { pool = transLow; poolN = TR_LOW_N; }
    int tries = 0;
    do { ledPreset = pool[random(poolN)]; tries++; } while (ledPreset == prev && tries < 4);
    lastTransSwitch = millis();
    transBeatCount = 0;
    transBeatsToSwitch = 2 + random(6);
    hdrDirty = true;
}

void updateTransitionMode() {
    if (!transitionMode) return;
    unsigned long now = millis();

    float cur = energy + avgEnergy();
    transSmoothedE = transSmoothedE * 0.92f + cur * 0.08f;
    if (cur > transPeakE) transPeakE = cur;
    transPeakE *= 0.998f;
    float threshold = max(0.08f, transSmoothedE * 1.6f);

    transBeatFlag = false;
    if (cur > threshold && transPrevE <= threshold && (now - transLastBeat > 120)) {
        transBeatFlag = true;
        unsigned long interval = now - transLastBeat;
        if (interval < 2000 && interval > 80)
            transAvgInterval = transAvgInterval * 0.7f + interval * 0.3f;
        transLastBeat = now;
        transBeatCount++;
    }
    transPrevE = cur;

    unsigned long elapsed = now - lastTransSwitch;

    if (transBeatFlag && transBeatCount >= transBeatsToSwitch && elapsed > 400) {
        transDoSwitch(transSmoothedE);
        return;
    }

    if (cur > transPeakE * 0.9f && transPeakE > 0.5f && elapsed > 600) {
        transDoSwitch(cur);
        return;
    }

    if (transPrevE > 0.3f && cur < 0.05f && elapsed > 300) {
        transDoSwitch(cur);
        return;
    }

    float maxWait = max(2000.0f, transAvgInterval * transBeatsToSwitch * 2);
    if (elapsed > (unsigned long)maxWait) {
        transDoSwitch(transSmoothedE);
    }
}

static unsigned long btnBLastRelease = 0;
static uint8_t btnBClickCount = 0;
static bool btnBWaitingDouble = false;

void checkButtons() {
    M5.update();
    bool bHeld = M5.BtnB.isPressed();
    unsigned long now = millis();

    // While B is held: A = brightness down, C = brightness up
    if (bHeld) {
        if (M5.BtnA.isPressed() && (now - lastBriChange > 150)) {
            uint8_t cur = FastLED.getBrightness();
            FastLED.setBrightness(cur > 10 ? cur - 10 : 1);
            lastBriChange = now;
            hdrDirty = true;
            btnBConsumed = true;
        }
        if (M5.BtnC.isPressed() && (now - lastBriChange > 150)) {
            uint8_t cur = FastLED.getBrightness();
            FastLED.setBrightness(cur < 245 ? cur + 10 : 255);
            lastBriChange = now;
            hdrDirty = true;
            btnBConsumed = true;
        }
    }

    // Button A: prev LED preset (only when B not held)
    if (!bHeld && M5.BtnA.wasPressed()) {
        if (transitionMode) {
            transitionMode = false;
            hdrDirty = true;
        }
        ledPreset = (ledPreset + NUM_PRESETS - 1) % NUM_PRESETS;
        hdrDirty = true;
    }

    // Button B release: detect single vs double click
    if (M5.BtnB.wasReleased()) {
        if (btnBConsumed) {
            btnBConsumed = false;
            btnBWaitingDouble = false;
            btnBClickCount = 0;
        } else {
            btnBClickCount++;
            btnBLastRelease = now;
            btnBWaitingDouble = true;
        }
    }

    if (btnBWaitingDouble && (now - btnBLastRelease > 300)) {
        if (btnBClickCount >= 2) {
            transitionMode = !transitionMode;
            if (transitionMode) {
                lastTransSwitch = now;
                transBeatCount = 0;
                transSmoothedE = avgEnergy();
                transPeakE = 0;
                transBeatsToSwitch = 4;
            }
            hdrDirty = true;
        } else if (btnBClickCount == 1) {
            visMode = (visMode + 1) % NUM_VIS;
            hdrDirty = true;
            visClear = true;
            memset(bandDisp, 0, sizeof(bandDisp));
            memset(bandPk,   0, sizeof(bandPk));
        }
        btnBWaitingDouble = false;
        btnBClickCount = 0;
    }

    // Button C: next LED preset (only when B not held)
    if (!bHeld && M5.BtnC.wasPressed()) {
        if (transitionMode) {
            transitionMode = false;
            hdrDirty = true;
        }
        ledPreset = (ledPreset + 1) % NUM_PRESETS;
        hdrDirty = true;
    }
}

// ====================== SETUP & LOOP =======================

void setup() {
    auto cfg = M5.config();
    cfg.internal_spk = false;
    M5.begin(cfg);
    M5.Display.fillScreen(TFT_BLACK);

    dacWrite(25, 0);

    Serial2.end();
    Serial2.begin(MIDI_BAUD, SERIAL_8N1, MIDI_RX_PIN, MIDI_TX_PIN);

    FastLED.addLeds<WS2812B, LED_DATA_PIN, LED_COLOR_ORDER>(leds, numLeds);
    FastLED.setBrightness(LED_BRIGHTNESS);
    FastLED.clear();
    FastLED.show();

    delay(100);

    memset(noteVel,  0, sizeof(noteVel));
    memset(bandLvl,  0, sizeof(bandLvl));
    memset(bandDisp, 0, sizeof(bandDisp));
    memset(bandPk,   0, sizeof(bandPk));
    memset(wfData,   0, sizeof(wfData));
    memset(drops,    0, sizeof(drops));
    memset(rings,    0, sizeof(rings));

    initStars();
    buildLUTs();
    drawHeader();
}

void loop() {
    readMIDI();
    checkButtons();

    unsigned long now = millis();
    if (now - lastFrameMs >= 1000 / TARGET_FPS) {
        lastFrameMs = now;
        energy *= 0.88f;
        if (energy < 0.005f) energy = 0;
        updateTransitionMode();
        updateDisplay();
        updateLEDs();
    }
}
