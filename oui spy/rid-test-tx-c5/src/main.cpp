/*
 * =============================================================================
 * Remote ID Test Transmitter - XIAO ESP32-C5
 * FOR TESTING ONLY - NOT FOR DISTRIBUTION
 *
 * Broadcasts ODID-compliant NAN Action Frames on 5GHz UNII-3 channels
 * Generates random drone ID and simulated circular flight path at boot
 *
 * Used to verify C5 detection firmware catches 5GHz Remote ID
 * =============================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_mac.h>
#include <nvs_flash.h>
#include <math.h>
#include "opendroneid.h"
#include "odid_wifi.h"

// =============================================================================
// Configuration
// =============================================================================
#define TX_POWER_DBM      20        // Max TX power
#define BROADCAST_HZ      4         // Broadcasts per second
#define BROADCAST_MS      (1000 / BROADCAST_HZ)

// Buzzer (XIAO ESP32-C5, D2 = GPIO25)
#define BUZZER_PIN        25

// =============================================================================
// Pac-Man Power Pellet Siren (ghost chase mode!)
// Two-tone wailing siren that loops while transmitting
// =============================================================================
static unsigned long sirenLastStep = 0;
static int sirenStep = 0;
static bool sirenRising = true;

// The classic Pac-Man siren is a repeating sweep between two frequencies
// Low ~200Hz up to ~600Hz and back down, creating that iconic wail
// Simple once-per-second beep
#define BEEP_FREQ         800
#define BEEP_DURATION_MS  80
#define BEEP_INTERVAL_MS  1000

static void updateSiren() {
    unsigned long now = millis();
    if (now - sirenLastStep < BEEP_INTERVAL_MS) return;
    sirenLastStep = now;
    tone(BUZZER_PIN, BEEP_FREQ, BEEP_DURATION_MS);
}

// 5GHz UNII-3 channels to rotate through
static const uint8_t channels_5g[] = { 149, 153, 157, 161, 165 };
#define NUM_5G_CH (sizeof(channels_5g) / sizeof(channels_5g[0]))
static uint8_t chanIdx = 0;

// Simulated flight parameters
#define CIRCLE_RADIUS_DEG  0.002    // ~220m radius circle
#define ORBIT_PERIOD_SEC   60.0     // Full circle in 60 seconds
#define BASE_ALTITUDE_M    100.0    // 100m AGL
#define SPEED_MPS          15.0     // ~33mph

// =============================================================================
// Random drone identity (generated at boot)
// =============================================================================
static char droneSerialId[ODID_ID_SIZE + 1];
static char operatorId[ODID_ID_SIZE + 1];
static double homeLat, homeLon;
static uint8_t fakeMac[6];

static void generateRandomIdentity() {
    // Random serial number (FAA format-ish)
    const char charset[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    snprintf(droneSerialId, sizeof(droneSerialId), "TEST");
    for (int i = 4; i < 16; i++) {
        droneSerialId[i] = charset[random(0, sizeof(charset) - 1)];
    }
    droneSerialId[16] = '\0';

    // Random operator ID
    snprintf(operatorId, sizeof(operatorId), "OP");
    for (int i = 2; i < 10; i++) {
        operatorId[i] = charset[random(0, sizeof(charset) - 1)];
    }
    operatorId[10] = '\0';

    // Random home position (continental US)
    homeLat = 30.0 + (random(0, 15000) / 1000.0);   // 30-45N
    homeLon = -120.0 + (random(0, 30000) / 1000.0);  // -120 to -90W

    // Random MAC
    esp_fill_random(fakeMac, 6);
    fakeMac[0] = (fakeMac[0] & 0xFC) | 0x02;  // Locally administered, unicast

    Serial.printf("[ID] Serial: %s\n", droneSerialId);
    Serial.printf("[ID] Operator: %s\n", operatorId);
    Serial.printf("[ID] Home: %.6f, %.6f\n", homeLat, homeLon);
    Serial.printf("[ID] MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
                  fakeMac[0], fakeMac[1], fakeMac[2],
                  fakeMac[3], fakeMac[4], fakeMac[5]);
}

// =============================================================================
// Simulated flight path (circle around home point)
// =============================================================================
static void getSimulatedPosition(double *lat, double *lon, float *alt,
                                  float *speed, float *heading) {
    double elapsed = millis() / 1000.0;
    double angle = (elapsed / ORBIT_PERIOD_SEC) * 2.0 * M_PI;

    *lat = homeLat + CIRCLE_RADIUS_DEG * sin(angle);
    *lon = homeLon + CIRCLE_RADIUS_DEG * cos(angle);
    *alt = BASE_ALTITUDE_M + 10.0 * sin(angle * 3.0);  // Gentle altitude wobble
    *speed = SPEED_MPS + 2.0 * sin(angle * 2.0);       // Speed variation
    *heading = fmod((360.0 - (angle * 180.0 / M_PI) + 90.0), 360.0);
    if (*heading < 0) *heading += 360.0;
}

// =============================================================================
// Encode and transmit ODID using the official library builder
// =============================================================================
static ODID_UAS_Data uasData;
static uint8_t txFrame[1024];
static uint32_t txCount = 0;
static uint8_t sendCounter = 0;

static void broadcastRemoteId() {
    double lat, lon;
    float alt, spd, hdg;
    getSimulatedPosition(&lat, &lon, &alt, &spd, &hdg);

    // Zero out
    memset(&uasData, 0, sizeof(uasData));

    // Basic ID
    uasData.BasicID[0].UAType = ODID_UATYPE_HELICOPTER_OR_MULTIROTOR;
    uasData.BasicID[0].IDType = ODID_IDTYPE_SERIAL_NUMBER;
    strncpy((char *)uasData.BasicID[0].UASID, droneSerialId, ODID_ID_SIZE);
    uasData.BasicIDValid[0] = 1;

    // Location
    uasData.Location.Latitude = lat;
    uasData.Location.Longitude = lon;
    uasData.Location.AltitudeGeo = alt;
    uasData.Location.AltitudeBaro = alt - 5.0;
    uasData.Location.Height = alt;
    uasData.Location.HeightType = ODID_HEIGHT_REF_OVER_TAKEOFF;
    uasData.Location.HorizAccuracy = ODID_HOR_ACC_10_METER;
    uasData.Location.VertAccuracy = ODID_VER_ACC_10_METER;
    uasData.Location.SpeedAccuracy = ODID_SPEED_ACC_3_METERS_PER_SECOND;
    uasData.Location.TSAccuracy = ODID_TIME_ACC_1_5_SECOND;
    uasData.Location.SpeedHorizontal = spd;
    uasData.Location.Direction = hdg;
    uasData.Location.Status = ODID_STATUS_AIRBORNE;
    uasData.Location.TimeStamp = (float)(millis() % 36000000) / 10000.0;
    uasData.LocationValid = 1;

    // System
    uasData.System.OperatorLocationType = ODID_OPERATOR_LOCATION_TYPE_TAKEOFF;
    uasData.System.OperatorLatitude = homeLat;
    uasData.System.OperatorLongitude = homeLon;
    uasData.System.AreaCount = 1;
    uasData.System.AreaRadius = 0;
    uasData.System.AreaCeiling = alt + 50.0;
    uasData.System.AreaFloor = 0;
    uasData.System.ClassificationType = ODID_CLASSIFICATION_TYPE_EU;
    uasData.SystemValid = 1;

    // Operator ID
    uasData.OperatorID.OperatorIdType = ODID_OPERATOR_ID;
    strncpy((char *)uasData.OperatorID.OperatorId, operatorId, ODID_ID_SIZE);
    uasData.OperatorIDValid = 1;

    // Use the ODID library's own NAN Action Frame builder
    // This guarantees the frame matches what the receiver parser expects
    int frameLen = odid_wifi_build_message_pack_nan_action_frame(
        &uasData, (char *)fakeMac, sendCounter++, txFrame, sizeof(txFrame));

    if (frameLen <= 0) {
        Serial.printf("[TX] Frame build FAILED: %d\n", frameLen);
        return;
    }

    // Rotate through 5GHz channels
    uint8_t ch = channels_5g[chanIdx];
    esp_err_t chErr = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    chanIdx = (chanIdx + 1) % NUM_5G_CH;

    // Transmit raw frame
    esp_err_t txErr = esp_wifi_80211_tx(WIFI_IF_STA, txFrame, frameLen, false);

    txCount++;
    // Log every TX for first 20, then every 5 seconds
    if (txCount <= 20 || txCount % (BROADCAST_HZ * 5) == 0) {
        Serial.printf("[TX #%lu] ch%d(set:%s) | %.6f, %.6f | alt:%.0fm spd:%.0f hdg:%.0f | tx:%s | len:%d\n",
                      txCount, ch,
                      (chErr == ESP_OK) ? "OK" : esp_err_to_name(chErr),
                      lat, lon, alt, spd, hdg,
                      (txErr == ESP_OK) ? "OK" : esp_err_to_name(txErr),
                      frameLen);
    }
}

// =============================================================================
// Setup
// =============================================================================
void setup() {
    delay(2000);
    Serial.begin(115200);

    Serial.println();
    Serial.println("================================================");
    Serial.println("  REMOTE ID TEST TRANSMITTER - XIAO ESP32-C5");
    Serial.println("  5GHz UNII-3 NAN Action Frames");
    Serial.println("  !! FOR TESTING ONLY - DO NOT DISTRIBUTE !!");
    Serial.println("================================================");

    // Seed random from hardware RNG
    uint32_t seed;
    esp_fill_random(&seed, sizeof(seed));
    randomSeed(seed);

    generateRandomIdentity();

    // Init WiFi in STA mode for raw TX
    nvs_flash_init();
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();

    // Enable promiscuous mode (required for raw TX on some channels)
    esp_wifi_set_promiscuous(true);

    // Set to first 5GHz channel
    esp_wifi_set_channel(channels_5g[0], WIFI_SECOND_CHAN_NONE);

    // Max TX power
    esp_wifi_set_max_tx_power(TX_POWER_DBM * 4);  // Units are 0.25dBm

    // Buzzer init
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);

    Serial.println();
    Serial.printf("[+] Broadcasting on 5GHz UNII-3 (ch 149-165)\n");
    Serial.printf("[+] Rate: %d Hz, rotating channels each TX\n", BROADCAST_HZ);
    Serial.printf("[+] Simulated circular flight: radius ~220m, period %ds\n",
                  (int)ORBIT_PERIOD_SEC);
    Serial.printf("[+] Buzzer: GPIO%d (Pac-Man power pellet siren)\n", BUZZER_PIN);
    Serial.println("[+] Transmitting...\n");

    // Pac-Man "waka waka" intro before siren starts
    for (int i = 0; i < 4; i++) {
        tone(BUZZER_PIN, 440, 80);   // waka
        delay(100);
        tone(BUZZER_PIN, 330, 80);   // waka
        delay(100);
    }
    noTone(BUZZER_PIN);
    delay(200);
}

// =============================================================================
// Loop
// =============================================================================
void loop() {
    broadcastRemoteId();
    updateSiren();  // Pac-Man ghost chase siren -- wails while transmitting
    delay(BROADCAST_MS);
}
