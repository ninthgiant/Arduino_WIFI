// Standalone AirLift provisioning tool for Uno R4 WiFi + Adafruit AirLift
// Shield #4285. Brings a fresh (or wedged) AirLift to a known-good state in
// one power cycle:
//   1. probe nina-fw via WiFi.firmwareVersion()
//   2. if dead, read ESP32 efuse XPD_SDIO bits
//   3. if unset, burn them (forces VDD_SDIO=3.3V regardless of IO12 strap)
//      and NVIC reset so the strap re-latches
//   4. flash NINAFW.BIN from SD root (MD5-verified)
//   5. NVIC reset so the freshly-flashed firmware cold-boots
//
// Single-purpose: no data acquisition, no UDP, no RTC, no AD7193.
// Designed for fleet provisioning - one R4 + one SD card, swap AirLift
// shields between boards. SD card needs ONLY:
//   NINAFW.BIN  (validated nina-fw 3.3.0 fork, 1,333,248 bytes,
//                MD5 7b50dfbc97f488fce09e49a3bd8c779c -- a copy is included
//                next to this .ino file at sketches/Arduino_WIFI_Flasher/
//                NINAFW.BIN; copy it to the SD root as NINAFW.BIN)
//
// Library dependency: requires the patched ESPSerialFlasher library (R4
// pin map, MD5_ENABLED, 921600 baud, 4 KB chunks). Same one shipped with
// the Arduino_WIFI_All auto-recovery PR; if that PR isn't merged install
// the library separately from
// https://github.com/TacunaSystems/Arduino_WIFI_AirLift/tree/airlift-port/libraries/ESPSerialFlasher
//
// If a chip is genuinely unrecoverable the unit boot-loops on power-up
// (flash succeeds, MD5 verifies, but post-reboot the chip still won't run
// nina-fw). Operator watches LCD/serial and pulls power.

#include <SPI.h>
#include <SD.h>
#include <WiFiNINA.h>
#include <LiquidCrystal.h>
#include "src/ESPSerialFlasher/ESPSerialFlasher.h"
#include "src/ESPSerialFlasher/esp_loader.h"

// AirLift Shield #4285 pin map for Uno R4 WiFi.
#define AIRLIFT_CS    15   // A1
#define AIRLIFT_BUSY   7
#define AIRLIFT_RESET 14   // A0
#define AIRLIFT_GPIO0 -1   // G0 jumper open on shield
#define SD_CS         10

#define NINA_FW_FILENAME "NINAFW.BIN"

// LCD is optional. Comment out the next line if you don't have one wired.
#define USE_LCD 1
#ifdef USE_LCD
const int rs = 9, en = 8, d4 = 5, d5 = 4, d6 = 3, d7 = 2;
LiquidCrystal lcd(rs, en, d4, d5, d6, d7);
static void lcdLine(uint8_t row, const char *text16) {
  lcd.setCursor(0, row);
  lcd.print(text16);
}
#else
static void lcdLine(uint8_t, const char *) {}
#endif

// ESP32 EFUSE_BLK0 registers and the three VDD_SDIO bits we burn.
// Espressif factory-burns these on production WROOM modules; raw modules
// don't have them set, so flash voltage gets decided by the IO12 strap.
// Adafruit AirLifts don't pull IO12, so unburned chips boot ROM but the
// app won't run. Burning these three bits permanently overrides the strap
// to 3.3V.
#define EFUSE_BASE          0x3FF5A000
#define EFUSE_BLK0_RDATA4   (EFUSE_BASE + 0x10)
#define EFUSE_BLK0_WDATA4   (EFUSE_BASE + 0x2C)
#define EFUSE_CONF_REG      (EFUSE_BASE + 0xFC)
#define EFUSE_CMD_REG       (EFUSE_BASE + 0x104)
#define EFUSE_CONF_WRITE    0x5A5A
#define EFUSE_CONF_READ     0x5AA5
#define EFUSE_CMD_PGM       0x02
#define EFUSE_CMD_READ      0x01
#define BIT_XPD_SDIO_REG     14
#define BIT_XPD_SDIO_TIEH    15
#define BIT_XPD_SDIO_FORCE   16

static bool waitEfuseCmdClear(uint32_t cmdBit, uint32_t timeoutMs) {
  uint32_t t0 = millis();
  while (millis() - t0 < timeoutMs) {
    uint32_t v = 0;
    if (esp_loader_read_register(EFUSE_CMD_REG, &v) != ESP_LOADER_SUCCESS) return false;
    if ((v & cmdBit) == 0) return true;
    delay(5);
  }
  return false;
}

// Returns true if a burn fired (caller must NVIC reset to re-latch strap).
// Returns false if already burned or on connect/read/verify failure.
static bool maybeBurnEfuse() {
  Serial.println(F("[efuse] reading chip state"));
  lcdLine(0, "Efuse check...  ");
  lcdLine(1, "                ");

  ESPFlasherInit(true, &Serial);
  if (ESPFlasherConnect() != ESP_LOADER_SUCCESS) {
    Serial.println(F("[efuse] flasher connect failed"));
    lcdLine(1, "Connect FAIL    ");
    delay(2000);
    return false;
  }

  uint32_t rdata4 = 0;
  if (esp_loader_read_register(EFUSE_BLK0_RDATA4, &rdata4) != ESP_LOADER_SUCCESS) {
    Serial.println(F("[efuse] RDATA4 read failed"));
    lcdLine(1, "Read FAIL       ");
    delay(2000);
    return false;
  }

  const uint32_t wantMask = (1U << BIT_XPD_SDIO_REG)
                          | (1U << BIT_XPD_SDIO_FORCE)
                          | (1U << BIT_XPD_SDIO_TIEH);
  uint32_t toBurn = wantMask & ~rdata4;

  Serial.print(F("[efuse] RDATA4=0x")); Serial.print(rdata4, HEX);
  Serial.print(F(" toBurn=0x")); Serial.println(toBurn, HEX);

  if (toBurn == 0) {
    Serial.println(F("[efuse] already burned - no-op"));
    lcdLine(1, "Already burned  ");
    delay(1000);
    return false;
  }

  lcdLine(0, "Efuse burn...   ");
  lcdLine(1, "Do NOT power off");
  Serial.println(F("[efuse] burning XPD_SDIO bits"));

  bool seqOk =
      esp_loader_write_register(EFUSE_BLK0_WDATA4, toBurn)        == ESP_LOADER_SUCCESS
   && esp_loader_write_register(EFUSE_CONF_REG, EFUSE_CONF_WRITE) == ESP_LOADER_SUCCESS
   && esp_loader_write_register(EFUSE_CMD_REG, EFUSE_CMD_PGM)     == ESP_LOADER_SUCCESS
   && waitEfuseCmdClear(EFUSE_CMD_PGM, 2000)
   && esp_loader_write_register(EFUSE_CONF_REG, EFUSE_CONF_READ)  == ESP_LOADER_SUCCESS
   && esp_loader_write_register(EFUSE_CMD_REG, EFUSE_CMD_READ)    == ESP_LOADER_SUCCESS
   && waitEfuseCmdClear(EFUSE_CMD_READ, 2000);

  if (!seqOk) {
    Serial.println(F("[efuse] burn sequence failed"));
    lcdLine(0, "Efuse FAIL      ");
    delay(2000);
    return false;
  }

  uint32_t after = 0;
  if (esp_loader_read_register(EFUSE_BLK0_RDATA4, &after) != ESP_LOADER_SUCCESS
      || (after & wantMask) != wantMask) {
    Serial.print(F("[efuse] verify failed: after=0x")); Serial.println(after, HEX);
    lcdLine(0, "Efuse VERIFY    ");
    delay(2000);
    return false;
  }

  Serial.print(F("[efuse] burn verified: after=0x")); Serial.println(after, HEX);
  lcdLine(0, "Efuse OK        ");
  lcdLine(1, "Rebooting...    ");
  delay(1500);
  return true;
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  delay(2000);
  Serial.println();
  Serial.println(F("=== AirLift Provisioner ==="));

#ifdef USE_LCD
  lcd.begin(16, 2);
  lcdLine(0, "AirLift provis. ");
  lcdLine(1, "                ");
#endif

  if (!SD.begin(SD_CS)) {
    Serial.println(F("SD.begin failed - cannot provision"));
    lcdLine(0, "SD FAIL         ");
    lcdLine(1, "Check card      ");
    while (1) { digitalWrite(LED_BUILTIN, HIGH); delay(100); digitalWrite(LED_BUILTIN, LOW); delay(100); }
  }
  if (!SD.exists(NINA_FW_FILENAME)) {
    Serial.print(F("Missing on SD: ")); Serial.println(NINA_FW_FILENAME);
    lcdLine(0, "NINAFW.BIN n/f  ");
    lcdLine(1, "Check SD card   ");
    while (1) { digitalWrite(LED_BUILTIN, HIGH); delay(100); digitalWrite(LED_BUILTIN, LOW); delay(100); }
  }
  Serial.print(F("SD OK, ")); Serial.print(NINA_FW_FILENAME); Serial.println(F(" present"));

  WiFi.setPins(AIRLIFT_CS, AIRLIFT_BUSY, AIRLIFT_RESET, AIRLIFT_GPIO0);
  lcdLine(0, "WiFi check...   ");
  Serial.println(F("[wifi] probing firmwareVersion()"));
  String fw = WiFi.firmwareVersion();
  bool fwOk = (fw.length() >= 5) && (fw[0] != (char)0xFF) && (fw[0] != 0);
  if (fwOk) {
    Serial.print(F("[wifi] firmware OK: ")); Serial.println(fw);
    char line[17];
    snprintf(line, sizeof(line), "WiFi fw: %.7s", fw.c_str());
    lcdLine(0, line);
    lcdLine(1, "Provisioned     ");
    // Solid LED = done, board already healthy, nothing to do.
    while (1) { digitalWrite(LED_BUILTIN, HIGH); delay(1000); }
  }
  Serial.println(F("[wifi] firmware unreadable - provisioning"));
  lcdLine(0, "WiFi DEAD       ");
  delay(800);

  if (maybeBurnEfuse()) {
    Serial.println(F("[efuse] burn done; resetting"));
    delay(500);
    NVIC_SystemReset();
  }

  lcdLine(0, "Flashing nina-fw");
  lcdLine(1, "Do NOT power off");
  Serial.println(F("[wifi] starting flash from SD..."));
  ESPFlasherInit(true, &Serial);
  esp_loader_error_t cerr = ESPFlasherConnect();
  if (cerr != ESP_LOADER_SUCCESS) {
    Serial.print(F("[wifi] ESPFlasherConnect failed: ")); Serial.println(cerr);
    lcdLine(0, "Flash connect   ");
    char line[17]; snprintf(line, sizeof(line), "FAIL e=%d        ", (int)cerr);
    lcdLine(1, line);
    while (1) { digitalWrite(LED_BUILTIN, HIGH); delay(100); digitalWrite(LED_BUILTIN, LOW); delay(100); }
  }
  esp_loader_error_t ferr = ESPFlashBin(NINA_FW_FILENAME);
  if (ferr != ESP_LOADER_SUCCESS) {
    Serial.print(F("[wifi] ESPFlashBin failed: ")); Serial.println(ferr);
    lcdLine(0, "Flash FAIL      ");
    char line[17]; snprintf(line, sizeof(line), "err=%d           ", (int)ferr);
    lcdLine(1, line);
    while (1) { digitalWrite(LED_BUILTIN, HIGH); delay(100); digitalWrite(LED_BUILTIN, LOW); delay(100); }
  }
  Serial.println(F("[wifi] reflash done; rebooting"));
  lcdLine(0, "Reflashed       ");
  lcdLine(1, "Rebooting...    ");
  delay(1500);
  NVIC_SystemReset();
}

void loop() {
  // Slow heartbeat - setup() ran to completion without an NVIC reset
  // means we're in the "already provisioned" branch.
  digitalWrite(LED_BUILTIN, HIGH); delay(1000);
  digitalWrite(LED_BUILTIN, LOW);  delay(1000);
}
