/*
    FILE: AD7193_2026_WIFI renamed from  AD7193_MOM_v01 on June 5, 2024
    AUTHOR: RAM
    DATE: 02/18/2026
    PURPOSE: Implement ability to 1) save only during daylight hours and 2) comm with WIFI to send prev day's data
    STATUS: In progress

    Based on AD7193_MOM_v01 which was operationals from June 2024 thru Dec 2025

    June 5, 2024 - when went operational
    --- renamed AAD7193_MOM_v01 because no longer a beta version. Installed on all R4 arduinos for Kent Island

    Feb 18, 2026
    --- Time the speed HZ before any testing - 51.59 hz (summer were 56.9hz - card difference)
    --- Added time check in loop so it only saves data in day time (default 7AM, 8PM)
    --- Time the speed HZ with time check added - 51.56 - NO TIME ADDED for the check

    May 20, 2026
    --- Added check for empty DL file to prevent endless failed loop in trim process when no raw files are present. If no raw files, will skip trim and proceed to WiFi mode with empty file, which controller can handle as a no-data day.
    */

// libraries needed

#include <PRDC_AD7193.h>
#include <LiquidCrystal.h>
#include <SD.h>
#include <SPI.h>
#include <Wire.h>

// Select exactly one WiFi hardware profile before compiling.
// Default is the current Uno R4 WiFi onboard ESP32-S3 radio.

// #define WIFI_PROFILE_R4_WIFI 1 // this is the R4 WIFI setup - change version string below. This worked on S74C with tacuna board
#define WIFI_PROFILE_AIRLIFT 1

#if defined(WIFI_PROFILE_R4_WIFI) && defined(WIFI_PROFILE_AIRLIFT)
#error "Select only one WiFi profile: WIFI_PROFILE_R4_WIFI or WIFI_PROFILE_AIRLIFT"
#elif defined(WIFI_PROFILE_AIRLIFT)
#include <WiFiNINA.h>
#include "ESPSerialFlasher.h"   // self-heal: reflash nina-fw from SD if AirLift is dead
#include "esp_loader.h"         // register-level access for sentinel-gated efuse burn
#elif defined(WIFI_PROFILE_R4_WIFI)
#include <WiFiS3.h>
#else
#error "Select a WiFi profile: WIFI_PROFILE_R4_WIFI or WIFI_PROFILE_AIRLIFT"
#endif

#define BSM_SENSOR_HOOK_ENABLED 1 // version 4.0 only has data collection true

#include <WiFiUdp.h>
#include <limits.h>
#include "Time.h"
#include "RTClib.h"
#include "secrets.h"

// Runtime state and hardware handles.
File myFile;
String myFilename;

// PCB variable defined by Tacuna code
#define SRAM_CS 1 //Use A0 for Uno R3.  Use 1 for Uno R4
#define SD_CS 10
#define AD7193_CS 0 //Use A1 for Uno R3. Use 0 for Uno R4

#if defined(WIFI_PROFILE_AIRLIFT)
// Adafruit AirLift Shield (#4285) pin map for WiFiNINA.
// Requires shield hardware mods documented in README.md.
#define AIRLIFT_CS    15   // A1
#define AIRLIFT_BUSY   7
#define AIRLIFT_RESET 14   // A0
#define AIRLIFT_GPIO0 -1   // G0 jumper open

// AirLift auto-recovery: on boot, probe nina-fw via WiFi.firmwareVersion().
// If the AirLift is unresponsive, reflash from SD root file NINA_FW_FILENAME
// (8.3 filename — the SD library on Renesas doesn't do long names) and
// hardware-reset. Set WIFI_AUTO_RECOVERY to 0 to disable.
//
// No retry counter and no per-recovery state on the SD: the fleet
// provisioning workflow is one Arduino + one SD card cycled through
// many AirLift shields, so any SD-persistent counter would carry
// stale state from a prior board into a fresh one. If the chip is
// genuinely unrecoverable the sketch will loop on power-up reflash;
// the operator watches and pulls power.
#define WIFI_AUTO_RECOVERY 1
#define NINA_FW_FILENAME   "NINAFW.BIN"

// Efuse burn fires during recovery when (a) ESPFlasher syncs to the
// chip and (b) the chip's live efuse read shows XPD_SDIO_{REG,FORCE,
// TIEH} unset. Source of truth is the chip itself read live every
// boot — no SD marker, so one SD card can provision many boards.
// Set WIFI_AUTO_EFUSE_BURN to 0 to disable the burn path entirely.
#define WIFI_AUTO_EFUSE_BURN  1
#endif

// Handle the ADC PCB unit
// PRDC_AD7193 AD7193;

// RTC
RTC_DS1307 RTC;

// Setup time variables that will be used in multiple places
DateTime currenttime;
long int myUnixTime;

// LCD instantiation
const int rs = 9, en = 8, d4 = 5, d5 = 4, d6 = 3, d7 = 2;
LiquidCrystal lcd(rs, en, d4, d5, d6, d7);

// AD7193 ADC scale
PRDC_AD7193 scale;
long int strain;  // value of the scale at any point in time

// Main loop cycle counter (debug/visibility only).
int tCounter = 0;
String deviceId;
String deviceID_6;
// Startup calibration-window state machine flags.
bool startupCalWindowInitialized = false;
bool startupCalWindowComplete = false;
uint32_t startupCalWindowEndTs = 0;
bool startupWifiCheckPending = true;
bool bootedInWifiWindow = false;
bool thisBootSatisfiedWifiRebootGate = false;
bool wifiSessionArmed = false;
bool wasInWifiWindow = false;
bool wifiWindowCycleInitialized = false;
bool wifiIdleAnnounced = false;
uint32_t wifiOutWindowSinceTs = 0;
bool wifiLowPowerStandby = false;
uint32_t wifiLastActivityTs = 0;
uint32_t wifiNextStandbyProbeTs = 0;
bool wifiCommandHandled = false;
uint32_t nextWifiConnectAttemptTs = 0;
uint8_t wifiConnectFailedAttempts = 0;
bool wifiConnectGiveUpThisWindow = false;
uint8_t wifiPolicy = 0;
uint16_t wifiPolicyGraceMinutes = 60;
uint8_t wifiPolicyWakeHour = 16;
bool readyBeaconAcked = false;
bool uploadCompletedThisWindow = false;
uint32_t nextReadyBeaconMs = 0;
String readyUploadFilename = "";
uint32_t readyBeaconSendCount = 0;
bool haveLastAcqSample = false;
long lastAcqSampleValue = 0;
// Error tracking for diagnostics
uint32_t i2cErrorCount = 0;
uint32_t rtcErrorCount = 0;
uint32_t sdErrorCount = 0;
uint32_t lastAcqLcdUpdateMs = 0;

const char POLL_MESSAGE[] = "POLL_UID";
const char LIST_FILES_MESSAGE[] = "LIST_FILES";
const char DELETE_FILE_MESSAGE[] = "DELETE_FILE";
const char START_FILE_MESSAGE[] = "START_FILE";
const char RESUME_MESSAGE[] = "RESUME";
const char SET_TIME_MESSAGE[] = "SET_TIME";
const char GET_TIME_MESSAGE[] = "GET_TIME";
const char PING_MESSAGE[] = "PING";
const char GET_STATUS_MESSAGE[] = "GET_STATUS";
const char GET_CONFIG_MESSAGE[] = "GET_CONFIG";
const char GET_DIAGNOSTICS_MESSAGE[] = "GET_DIAGNOSTICS";
const char GET_LAST_DATA_MESSAGE[] = "GET_LAST_DATA";
const char GET_NET_UID_MESSAGE[] = "GET_NET_UID";
const char GET_VERSION_MESSAGE[] = "GET_VERSION";
const char SET_CONFIG_MESSAGE[] = "SET_CONFIG";
const char REBOOT_MESSAGE[] = "REBOOT";
const char ENTER_DATA_MODE_MESSAGE[] = "ENTER_DATA_MODE";
const char GET_LOGS_MESSAGE[] = "GET_LOGS";
const char CLEAR_ERRORS_MESSAGE[] = "CLEAR_ERRORS";
const char READY_TO_UPLOAD_MESSAGE[] = "READY_TO_UPLOAD";
const char ACK_READY_MESSAGE[] = "ACK_READY";
const char SET_WIFI_POLICY_MESSAGE[] = "SET_WIFI_POLICY";
const char GET_WIFI_POLICY_MESSAGE[] = "GET_WIFI_POLICY";

// Cached time support. RTC/Gateway establish the base; millis() advances it.
uint32_t rtcBaseUnixTs = 0;          // Trusted Unix time at rtcBaseMs
uint32_t rtcBaseMs = 0;              // millis() when trusted Unix time was set
uint32_t rtcFallbackUnixTs = 0;      // startup-derived fallback epoch if RTC read glitches
String rtcCacheSource = "unset";     // boot-rtc, gateway-set-time, rtc, or fallback
const uint8_t RTC_STABLE_READ_MAX = 12;
const uint16_t RTC_STABLE_READ_DELAY_MS = 80;
const uint8_t RTC_BOOT_RECOVERY_PASSES = 2;
const uint8_t RTC_NTP_ATTEMPTS = 8;
const uint16_t RTC_NTP_RETRY_DELAY_MS = 500;
const long RTC_NTP_LOCAL_OFFSET_SECONDS = -3L * 3600L;  // Align with controller local offset (UTC-3h).

// Time window for WiFi phase (hours in local controller time). Default will be 7 and 19. Currently changed for testing during the day
uint8_t START_HOUR = 6; // 12; //8;    // testing using 1 hr window. Return to 8 to 17 for deployment and keeping wifi to min
uint8_t END_HOUR = 19; // 14; //19;
// TCP chunk size used for file transfer to controller.
// WiFiNINA/AirLift is more reliable with smaller chunks; R4 WiFi can use larger chunks.
#if defined(WIFI_PROFILE_AIRLIFT)
const size_t FILE_CHUNK_SIZE = 1024;
#elif defined(WIFI_PROFILE_R4_WIFI)
const size_t FILE_CHUNK_SIZE = 4096;
#endif
// Mandatory raw-capture period immediately after reboot.
// Set to 300s for normal time to reach WiFi mode quickly after reboot. 10 for testing
const uint32_t STARTUP_CAL_CAPTURE_SECONDS = 600UL; // 60UL; // for testing, set to 60s to speed up trim logic testing. Set to 600s for normal use to capture more calibration data and reach WiFi mode faster after reboot.
// Duration from file start treated same as calibration section for trim logic.
const uint32_t TRIM_CALIBRATION_SECONDS = STARTUP_CAL_CAPTURE_SECONDS;
// Fallback TR file duration when trim threshold calibration fails.
const uint32_t TRIM_FALLBACK_SECONDS = 60UL;
// Small problem TR file size when the expected raw DL file is missing/empty.
const uint32_t TRIM_PROBLEM_FILE_BYTES = 102400UL; // ~0.1 MB
// Optional guard from file start before event detection can begin.
const uint32_t TRIM_START_GUARD_SECONDS = TRIM_CALIBRATION_SECONDS;
// Seconds of context retained before event trigger time.
const uint32_t TRIM_PRE_EVENT_SECONDS = 180UL;
// Seconds retained after event trigger time.
const uint32_t TRIM_POST_EVENT_SECONDS = 600UL;
// Minimum threshold distance above baseline used in trim detection.
const long TRIM_TRIGGER_MIN_DELTA = 2000L;
// Fraction used to split calibration values into baseline vs elevated segments.
const float TRIM_CAL_SPLIT_FRACTION = 0.35f;
// Debounce hold time in seconds for event enter/exit state.
const float TRIM_DEBOUNCE_SECONDS = 0.5f;
// Progress logging frequency while analyzing/writing trim files.
const uint32_t TRIM_PROGRESS_ROWS = 50000UL;
// Maximum merged keep-intervals stored in RAM for trim pass.
const int MAX_TRIM_INTERVALS = 128;
// Calibration threshold detection buckets. The timestamp may be reused for many
// rows, so use sample-count buckets rather than Unix-second buckets.
const uint16_t TRIM_CAL_BUCKET_SAMPLES = 60;
const uint16_t TRIM_CAL_BUCKET_MIN_SAMPLES = 30;
const float TRIM_CAL_BASELINE_LOWEST_FRACTION = 0.20f;
const float TRIM_CAL_STEADY_SD_MULTIPLIER = 4.0f;
const float TRIM_CAL_BASELINE_SEPARATION_SD = 8.0f;
const int TRIM_CAL_BASELINE_BUCKET_MAX = 160;
// Require this many seconds continuously outside the WiFi window before leaving WiFi mode.
const uint32_t WIFI_EXIT_DEBOUNCE_SECONDS = 120UL;
// WiFi-window low-power behavior.
// Policy STAY_ACTIVE preserves existing behavior. Policy MORNING_ONLY can drop
// WiFi after READY/upload work and wake again at the configured hour.
const bool WIFI_STANDBY_ENABLED = true;
const uint8_t WIFI_POLICY_STAY_ACTIVE = 0;
const uint8_t WIFI_POLICY_MORNING_ONLY = 1;
const uint32_t WIFI_STANDBY_CHECK_INTERVAL_SECONDS = 30UL;
const uint32_t WIFI_CONNECT_TOTAL_TIMEOUT_MS = 120000UL;
const uint32_t WIFI_CONNECT_RETRY_INTERVAL_SECONDS = 600UL;
const uint8_t WIFI_CONNECT_MAX_FAILED_ATTEMPTS = 6;
const uint32_t READY_BEACON_INTERVAL_MS = 45000UL;
const uint32_t READY_BEACON_JITTER_MS = 10000UL;
// Raw file selection policy for trim phase.
// false: default to yesterday's DL file
// true : use today's DL file (test mode)
const bool TRIM_USE_TODAY_FILENAME = false;

/////////////////////
//  set constants
/////////////////////
// set the samples to average when getting data - thru 2025 was 80 which gave 56.9hz - use 70 -> 59 at home
const int myAVG = 80;

// initialize variables for SD -- use chipSelect = 4 without RTC board
const int chipSelect = 10;  // for the Wigoneer board and Adafruit board

// set the communications speed
const int comLevel = 115200;

// set flag for amount of feedback - false means to give us too much info
const bool verbose = true;

// set flag for printing to LCD
const bool printLCD = true;
// LCD refresh cadence during startup calibration acquisition phase.
const uint32_t LCD_CAL_UPDATE_INTERVAL_MS = 1000UL;
// LCD refresh cadence during normal acquisition phase.
// Set to 0 to disable periodic normal-run sample display updates. For now, every 5 seconds is a good compromise between visibility and pause spikes.
const uint32_t LCD_RUN_UPDATE_INTERVAL_MS = 5000UL;
// Acquisition write batching: keep chunk small to reduce pause spikes.
const uint16_t ACQ_LINES_PER_CHUNK = 12;
const uint32_t ACQ_FLUSH_INTERVAL_MS = 5000UL;

// set flag to print somethnig only if debugging
const bool debug = false;

// flag for countdown
const bool countdown = true;
// show which build we are making
#if defined(WIFI_PROFILE_AIRLIFT) && BSM_SENSOR_HOOK_ENABLED
const char VERSION[] = "4.6ctd";
#elif defined(WIFI_PROFILE_AIRLIFT)
const char VERSION[] = "4.6ctp";
#elif defined(WIFI_PROFILE_R4_WIFI) && BSM_SENSOR_HOOK_ENABLED
const char VERSION[] = "4.6cwd";
#else
const char VERSION[] = "4.6cwp";
#endif


// Interval of file timestamps to retain in trimmed output.
struct TrimInterval {
  uint32_t start_ts;
  uint32_t end_ts;
};

// Calibration-derived thresholds for trim event detection.
struct CalibrationThresholds {
  bool ok;
  uint32_t firstTs;
  long baselineMean;
  long lowCalibrationMean;
  long enterThreshold;
  long exitThreshold;
};

TrimInterval trimIntervals[MAX_TRIM_INTERVALS];
int trimIntervalCount = 0;

// WiFi and UDP globals
WiFiUDP udp;
bool sdReady = false;
IPAddress targetIp;
bool wifiInitialized = false;
bool wifiModeActive = false;
String networkHostname;
File acqDataFile;
bool acqDataFileOpen = false;
String acqOpenFilename = "";
char acqWriteBuf[512];
size_t acqWriteLen = 0;
uint32_t lastAcqFlushMs = 0;

/***********************
 * Returns the MCU unique ID as a 32-hex-character string.
 * @return Device unique identifier (uppercase hex).
 ***********************/
String getChipIdHex() {
  const bsp_unique_id_t *uid = R_BSP_UniqueIdGet();
  char id[33];
  snprintf(
    id, sizeof(id),
    "%08lX%08lX%08lX%08lX",
    (unsigned long) uid->unique_id_words[0],
    (unsigned long) uid->unique_id_words[1],
    (unsigned long) uid->unique_id_words[2],
    (unsigned long) uid->unique_id_words[3]
  );
  return String(id);
}

String getFirmwareVersion() {
  return String(VERSION);
}

String wifiPolicyName() {
  if (wifiPolicy == WIFI_POLICY_MORNING_ONLY) return "MORNING_ONLY";
  return "STAY_ACTIVE";
}

uint8_t parseWifiPolicyName(const char *value) {
  if (value == nullptr) return WIFI_POLICY_STAY_ACTIVE;
  if (strcmp(value, "MORNING_ONLY") == 0) return WIFI_POLICY_MORNING_ONLY;
  return WIFI_POLICY_STAY_ACTIVE;
}

bool wifiPolicyMaySleep() {
  return WIFI_STANDBY_ENABLED
    && wifiPolicy == WIFI_POLICY_MORNING_ONLY
    && (readyBeaconAcked || uploadCompletedThisWindow);
}

bool wifiPolicyWakeTimeReached(uint32_t unixTs) {
  uint32_t hour = (unixTs % 86400UL) / 3600UL;
  return hour >= wifiPolicyWakeHour;
}

uint32_t wifiPolicyGraceSeconds() {
  return (uint32_t)wifiPolicyGraceMinutes * 60UL;
}

void resetWifiConnectRetryState() {
  nextWifiConnectAttemptTs = 0;
  wifiConnectFailedAttempts = 0;
  wifiConnectGiveUpThisWindow = false;
}

bool canAttemptWifiConnect(uint32_t unixTs) {
  if (wifiConnectGiveUpThisWindow) return false;
  if (nextWifiConnectAttemptTs == 0) return true;
  return unixTs >= nextWifiConnectAttemptTs;
}

void noteWifiConnectFailure(uint32_t unixTs) {
  if (wifiConnectFailedAttempts < 255) wifiConnectFailedAttempts++;
  Serial.print(F("WiFi connect failed attempt "));
  Serial.print(wifiConnectFailedAttempts);
  Serial.print(F("/"));
  Serial.println(WIFI_CONNECT_MAX_FAILED_ATTEMPTS);

  if (wifiConnectFailedAttempts >= WIFI_CONNECT_MAX_FAILED_ATTEMPTS) {
    wifiConnectGiveUpThisWindow = true;
    nextWifiConnectAttemptTs = 0;
    Serial.println(F("WiFi connect attempts exhausted for this window."));
    if (printLCD) setLcdStatusLine1("WiFi: give up");
    return;
  }

  nextWifiConnectAttemptTs = unixTs + WIFI_CONNECT_RETRY_INTERVAL_SECONDS;
  Serial.print(F("Next WiFi connect attempt at unix "));
  Serial.println((unsigned long)nextWifiConnectAttemptTs);
  if (printLCD) setLcdStatusLine1("WiFi: retry wait");
}

/***********************
 * Builds a stable 6-char short UID from full chip ID.
 * Uses FNV-1a hash + extra mixing + Base32 alphabet.
 * @param fullId Full chip unique ID (hex string).
 * @param outLen Number of chars to emit (default 6).
 * @return Deterministic short UID.
 ***********************/
String shortUidFromHash(const String &fullId, size_t outLen = 6) {
  uint32_t h = 2166136261UL;  // FNV offset basis
  for (size_t i = 0; i < fullId.length(); ++i) {
    h ^= (uint8_t)fullId[i];
    h *= 16777619UL;  // FNV prime
  }

  // Extra avalanche for better bit diffusion.
  h ^= (h >> 16);
  h *= 0x7feb352dUL;
  h ^= (h >> 15);
  h *= 0x846ca68bUL;
  h ^= (h >> 16);

  const char ALPHABET[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  String out = "";
  out.reserve(outLen);
  uint32_t x = h;
  for (size_t i = 0; i < outLen; ++i) {
    uint8_t idx = x & 31U;
    out += ALPHABET[idx];
    x = (x >> 5) ^ (x << 27);
  }
  return out;
}

/***********************
 * Returns the ESP32-S3 station MAC address as uppercase hex.
 * @return MAC string without separators, e.g. AABBCCDDEEFF.
 ***********************/
String getWifiMacHex() {
  uint8_t mac[6] = {0};
  WiFi.macAddress(mac);
  char buf[13];
  snprintf(
    buf, sizeof(buf),
    "%02X%02X%02X%02X%02X%02X",
    mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]
  );
  return String(buf);
}

/***********************
 * Builds a stable network UID for external controller use.
 * Format mirrors common controller naming style.
 * @return UID like ESP32S3-ABCDE (fallback: ESP32S3-<deviceID_6>).
 ***********************/
String getNetworkUid() {
  String macHex = getWifiMacHex();
  if (macHex.length() >= 5) {
    return "ESP32S3-" + macHex.substring(macHex.length() - 5);
  }
  return "ESP32S3-" + deviceID_6;
}

/***********************
 * Builds hostname advertised to WiFi/DHCP controllers (e.g., UniFi).
 * Includes both network UID and MCU short ID for easier field matching.
 ***********************/
String buildNetworkHostname() {
  String host = getNetworkUid() + "-" + deviceID_6;
  if (host.length() > 31) {
    host = host.substring(0, 31);
  }
  return host;
}

/***********************
 * Returns a 4-hex suffix derived from ESP32 network UID.
 * @return Last 4 chars of network UID token.
 ***********************/
String getNetworkUidSuffix4() {
  String netUid = getNetworkUid();
  int dash = netUid.lastIndexOf('-');
  String token = (dash >= 0) ? netUid.substring(dash + 1) : netUid;
  if (token.length() >= 4) {
    return token.substring(token.length() - 4);
  }
  return token;
}

/***********************
 * Builds LCD UID line content.
 * Mode:
 * - R4   => UID: <deviceID_6>
 * - ESP  => UID: <esp4>
 * - BOTH => UID: <deviceID_6>-<esp4>
 * Default (unknown/empty): R4
 ***********************/
String getLcdUidLineBase(const String &modeIn) {
  String mode = modeIn;
  mode.toUpperCase();
  if (mode == "ESP") {
    return "UID: " + getNetworkUidSuffix4();
  }
  if (mode == "BOTH") {
    return "UID: " + deviceID_6 + "-" + getNetworkUidSuffix4();
  }
  return "UID: " + deviceID_6;
}

/***********************
 * Validates RTC date range to reject invalid/uninitialized reads.
 * @param dt RTC date-time to validate.
 * @return True when date components are in expected ranges.
 ***********************/
bool isRtcDateSane(const DateTime &dt) {
  int y = dt.year();
  int m = dt.month();
  int d = dt.day();
  return (y >= 2024 && y <= 2099 && m >= 1 && m <= 12 && d >= 1 && d <= 31);
}

void logRtcReadSample(const char *tag, uint8_t attemptIdx, const DateTime &dt) {
  Serial.print(F("RTC bad read"));
  if (tag != nullptr && tag[0] != '\0') {
    Serial.print(F(" ["));
    Serial.print(tag);
    Serial.print(F("]"));
  }
  Serial.print(F(" attempt "));
  Serial.print((unsigned int)(attemptIdx + 1));
  Serial.print(F(": "));
  Serial.print(dt.year());
  Serial.print(F("-"));
  Serial.print(dt.month());
  Serial.print(F("-"));
  Serial.print(dt.day());
  Serial.print(F("T"));
  Serial.print(dt.hour());
  Serial.print(F(":"));
  Serial.print(dt.minute());
  Serial.print(F(":"));
  Serial.print(dt.second());
  Serial.print(F(" unix="));
  Serial.println((unsigned long)dt.unixtime());
}

/***********************
 * Reads RTC repeatedly until values are stable/sane.
 * @param out Populated with a stable DateTime on success.
 * @return True if a stable/sane time was obtained; false otherwise.
 ***********************/
bool readRtcStable(
  DateTime &out,
  const char *tag = "",
  uint8_t maxReads = RTC_STABLE_READ_MAX,
  uint16_t delayMs = RTC_STABLE_READ_DELAY_MS
) {
  DateTime prev((uint32_t)0);
  bool hasPrev = false;
  for (uint8_t i = 0; i < maxReads; ++i) {
    DateTime cur = RTC.now();
    if (!isRtcDateSane(cur)) {
      logRtcReadSample(tag, i, cur);
      delay(delayMs);
      continue;
    }
    if (hasPrev) {
      uint32_t tPrev = prev.unixtime();
      uint32_t tCur = cur.unixtime();
      uint32_t dt = (tCur >= tPrev) ? (tCur - tPrev) : (tPrev - tCur);
      if (dt <= 2) {
        out = cur;
        return true;
      }
    }
    prev = cur;
    hasPrev = true;
    delay(delayMs);
  }
  return false;
}

bool syncRtcFromWifiNtp() {
  if (WiFi.status() != WL_CONNECTED) return false;

  unsigned long epochUtc = 0;
  for (uint8_t i = 0; i < RTC_NTP_ATTEMPTS; ++i) {
    epochUtc = WiFi.getTime();
    if (epochUtc > 1700000000UL) break;  // basic sanity gate (after 2023).
    delay(RTC_NTP_RETRY_DELAY_MS);
  }
  if (epochUtc <= 1700000000UL) {
    Serial.println(F("RTC NTP sync skipped: WiFi time unavailable."));
    return false;
  }

  long long adjusted = (long long)epochUtc + (long long)RTC_NTP_LOCAL_OFFSET_SECONDS;
  if (adjusted <= 0LL) {
    Serial.println(F("RTC NTP sync skipped: adjusted epoch invalid."));
    return false;
  }

  RTC.adjust(DateTime((uint32_t)adjusted));
  delay(50);
  DateTime verify((uint32_t)0);
  if (!readRtcStable(verify, "ntp-verify")) {
    Serial.println(F("RTC NTP sync failed: verify read unstable."));
    return false;
  }

  Serial.print(F("RTC synced from WiFi time. TIMESTAMP:\t"));
  Serial.println(verify.timestamp(DateTime::TIMESTAMP_FULL));
  Update_TimeStamp_Cache_From_RTC();
  return true;
}

/***********************
 * Attempts RTC.begin() with retries.
 * @param attempts Number of begin attempts.
 * @return True if RTC initialized; false if all attempts fail.
 ***********************/
bool beginRtcWithRetry(uint8_t attempts = 3) {
  for (uint8_t i = 0; i < attempts; ++i) {
    if (RTC.begin()) return true;
    delay(100);
  }
  return false;
}

/***********************
 * Updates cached time from a trusted Unix timestamp.
 * @param epoch Trusted Unix timestamp in local controller time.
 * @param source Short diagnostic label.
 ***********************/
void Update_TimeStamp_Cache_From_Epoch(uint32_t epoch, const char *source = "") {
  rtcBaseUnixTs = epoch;
  rtcBaseMs = millis();
  rtcFallbackUnixTs = rtcBaseUnixTs;
  rtcCacheSource = (source && source[0] != '\0') ? String(source) : String("unknown");
  Serial.print(F("Time cache set"));
  if (source && source[0] != '\0') {
    Serial.print(F(" ["));
    Serial.print(source);
    Serial.print(F("]"));
  }
  Serial.print(F(": "));
  Serial.println((unsigned long)rtcBaseUnixTs);
}

/***********************
 * Refreshes cached timestamp base from RTC.
 * Use at boot or explicit RTC recovery only. Normal sampling uses millis().
 * @return True when cache updated from a stable RTC read.
 ***********************/
bool Update_TimeStamp_Cache_From_RTC() {
  DateTime nowRtc((uint32_t)0);
  if (!readRtcStable(nowRtc)) {
    return false;
  }
  Update_TimeStamp_Cache_From_Epoch(nowRtc.unixtime(), "rtc");
  return true;
}

/***********************
 * Returns current time from the trusted cache advanced by millis().
 ***********************/
uint32_t Estimated_TimeStamp_From_Cache(uint32_t nowMs) {
  if (rtcBaseUnixTs != 0) {
    return rtcBaseUnixTs + ((nowMs - rtcBaseMs) / 1000UL);
  }
  if (rtcFallbackUnixTs == 0) {
    rtcFallbackUnixTs = DateTime(F(__DATE__), F(__TIME__)).unixtime();
    rtcCacheSource = "compile-fallback";
  }
  return rtcFallbackUnixTs + (nowMs / 1000UL);
}

/***********************
 * Writes the current cached/millis estimate back to the RTC.
 * Used before WiFi-window reboot so the next boot reads a sane RTC.
 ***********************/
void Write_Estimated_Time_To_RTC(const char *reason = "") {
  uint32_t estimatedTs = Estimated_TimeStamp_From_Cache(millis());
  RTC.adjust(DateTime(estimatedTs));
  delay(10);
  Serial.print(F("RTC updated from cached time"));
  if (reason && reason[0] != '\0') {
    Serial.print(F(" ["));
    Serial.print(reason);
    Serial.print(F("]"));
  }
  Serial.print(F(": "));
  Serial.println((unsigned long)estimatedTs);
}

/***********************
 * Attempts to recover a stuck I2C bus by pulsing SCL.
 * Used after reset when peripherals may still be powered.
 ***********************/
void recoverI2CBus() {
  // Attempt to recover a stuck I2C bus after MCU reset while peripherals remain powered.
  pinMode(SDA, INPUT_PULLUP);
  pinMode(SCL, INPUT_PULLUP);
  delay(2);

  // If SDA is low, pulse SCL to release a stuck slave state machine.
  if (digitalRead(SDA) == LOW) {
    pinMode(SCL, OUTPUT);
    for (uint8_t i = 0; i < 9; ++i) {
      digitalWrite(SCL, HIGH);
      delayMicroseconds(10);
      digitalWrite(SCL, LOW);
      delayMicroseconds(10);
    }
    pinMode(SCL, INPUT_PULLUP);
    delay(2);
  }
}

/***********************
 * Incremental CRC32 update helper.
 * @param crc Current CRC state.
 * @param data Byte buffer.
 * @param len Number of bytes.
 * @return Updated CRC32 value.
 ***********************/
uint32_t crc32Update(uint32_t crc, const uint8_t *data, size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int j = 0; j < 8; ++j) {
      crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320UL : (crc >> 1);
    }
  }
  return ~crc;
}

/***********************
 * Formats CRC32 value as 8-char uppercase hex.
 * @param value CRC32 value.
 * @return Hex string.
 ***********************/
String crc32Hex(uint32_t value) {
  char out[9];
  snprintf(out, sizeof(out), "%08lX", (unsigned long) value);
  return String(out);
}

/***********************
 * Writes status text to LCD line 1 (padded/truncated to 16 chars).
 * @param status Text to show.
 ***********************/
void setLcdStatusLine1(const String &status) {
  if (!printLCD) return;
  lcd.setCursor(0, 0);
  String text = status;
  while (text.length() < 16) text += " ";
  lcd.print(text.substring(0, 16));
}

/***********************
 * Writes UID line to LCD line 2, optionally appending CAL tag.
 * @param showCalTag True to show trailing "CAL", false to clear it.
 ***********************/
void setLcdUidLine(bool showCalTag) {
  if (!printLCD) return;
  lcd.setCursor(0, 1);
  String line2 = getLcdUidLineBase("BOTH");
  if (showCalTag) {
    if (line2.length() <= 13) {
      while (line2.length() < 13) line2 += " ";
      line2 += "CAL";
    }
  } else {
    while (line2.length() < 16) line2 += " ";
  }
  lcd.print(line2.substring(0, 16));
}

/***********************
 * Writes acquisition line 1 with mode prefix and latest sample value.
 * Ensures "Cal:"/"Data:" are never shown without a value.
 * @param inCalibrationPhase True to use "Cal:" prefix, false for "Data:".
 * @param sampleValue Latest acquisition sample to display.
 ***********************/
void setLcdAcqLine1WithValue(bool inCalibrationPhase, long sampleValue) {
  if (!printLCD) return;
  (void) inCalibrationPhase;
  const char *prefix = "Data:";
  char line[32];
  snprintf(line, sizeof(line), "%s%ld", prefix, sampleValue);
  String text = String(line);
  while (text.length() < 16) text += " ";
  lcd.setCursor(0, 0);
  lcd.print(text.substring(0, 16));
}

/***********************
 * Determines whether acquisition-path sample LCD display should update now.
 * @param inCalibrationPhase True during startup calibration capture phase.
 * @return True when sample display should be refreshed.
 ***********************/
bool shouldUpdateAcqLcd(bool inCalibrationPhase) {
  if (!printLCD) return false;
  uint32_t intervalMs = inCalibrationPhase ? LCD_CAL_UPDATE_INTERVAL_MS : LCD_RUN_UPDATE_INTERVAL_MS;
  if (intervalMs == 0UL) return false;
  uint32_t nowMs = millis();
  if (lastAcqLcdUpdateMs == 0 || (uint32_t)(nowMs - lastAcqLcdUpdateMs) >= intervalMs) {
    lastAcqLcdUpdateMs = nowMs;
    return true;
  }
  return false;
}

/***********************
 * Flushes pending acquisition-buffer bytes to SD and optionally syncs media.
 * @param syncToCard True to call File.flush() after writing buffered bytes.
 * @return True on success.
 ***********************/
bool flushAcqBuffer(bool syncToCard) {
  if (!acqDataFileOpen) return false;
  if (acqWriteLen > 0) {
    size_t written = acqDataFile.write((const uint8_t *) acqWriteBuf, acqWriteLen);
    if (written != acqWriteLen) {
      acqWriteLen = 0;
      return false;
    }
    acqWriteLen = 0;
  }
  if (syncToCard) {
    acqDataFile.flush();
    lastAcqFlushMs = millis();
  }
  return true;
}

/***********************
 * Closes active acquisition file after writing buffered bytes.
 ***********************/
void closeAcqDataFile() {
  if (!acqDataFileOpen) {
    acqWriteLen = 0;
    return;
  }
  flushAcqBuffer(true);
  acqDataFile.close();
  acqDataFileOpen = false;
  acqOpenFilename = "";
  acqWriteLen = 0;
}

/***********************
 * Ensures acquisition file is open for append using current filename.
 * @param filename Target filename.
 * @return True when file is ready for buffered appends.
 ***********************/
bool ensureAcqDataFileOpen(const String &filename) {
  if (acqDataFileOpen && acqOpenFilename == filename) return true;
  if (acqDataFileOpen) closeAcqDataFile();
  acqDataFile = SD.open(filename.c_str(), FILE_WRITE);
  if (!acqDataFile) {
    acqDataFileOpen = false;
    acqOpenFilename = "";
    return false;
  }
  acqDataFileOpen = true;
  acqOpenFilename = filename;
  acqWriteLen = 0;
  lastAcqFlushMs = millis();
  return true;
}

/***********************
 * Appends one acquisition CSV line to in-memory write buffer.
 * @param sampleValue ADC sample value.
 * @param unixTs Cached timestamp for this acquisition batch.
 * @return True when buffered/written successfully.
 ***********************/
bool appendAcqLineToBuffer(long sampleValue, uint32_t unixTs) {
  char line[40];
  int n = snprintf(line, sizeof(line), "%ld, %lu\n", sampleValue, (unsigned long) unixTs);
  if (n <= 0 || (size_t) n >= sizeof(line)) return false;
  if (acqWriteLen + (size_t) n > sizeof(acqWriteBuf)) {
    if (!flushAcqBuffer(false)) return false;
  }
  memcpy(acqWriteBuf + acqWriteLen, line, (size_t) n);
  acqWriteLen += (size_t) n;
  return true;
}

/***********************
 * Sends one UDP text message.
 * @param msg Message payload.
 * @param ip Destination IP.
 * @param port Destination UDP port.
 ***********************/
void sendUdpMessage(const String &msg, const IPAddress &ip, uint16_t port) {
  udp.beginPacket(ip, port);
  udp.print(msg);
  udp.endPacket();
}

/***********************
 * CSV splitter in-place using strtok_r.
 * @param input Mutable C-string to split.
 * @param fields Output pointers to tokens.
 * @param maxFields Capacity of fields[].
 * @return Number of parsed fields.
 ***********************/
int splitCsv(char *input, char *fields[], int maxFields) {
  int count = 0;
  char *savePtr = nullptr;
  char *token = strtok_r(input, ",", &savePtr);
  while (token != nullptr && count < maxFields) {
    fields[count++] = token;
    token = strtok_r(nullptr, ",", &savePtr);
  }
  return count;
}

/***********************
 * Reads one newline-terminated data line from SD file.
 * @param f Open SD File.
 * @param buf Output buffer.
 * @param n Buffer size.
 * @return True when a non-empty line was read.
 ***********************/
bool readDataLine(File &f, char *buf, size_t n) {
  if (!f.available()) return false;
  size_t len = f.readBytesUntil('\n', buf, n - 1);
  buf[len] = '\0';
  while (len > 0 && (buf[len - 1] == '\r' || buf[len - 1] == '\n' || buf[len - 1] == ' ' || buf[len - 1] == '\t')) {
    buf[len - 1] = '\0';
    len--;
  }
  return len > 0;
}

/***********************
 * Parses "value, unix_time" CSV row.
 * @param line Mutable CSV line.
 * @param valueOut Parsed sample value.
 * @param tsOut Parsed unix timestamp.
 * @return True when parsing succeeds.
 ***********************/
bool parseDataCsvLine(char *line, long &valueOut, uint32_t &tsOut) {
  char *comma = strchr(line, ',');
  if (!comma) return false;
  *comma = '\0';
  char *left = line;
  char *right = comma + 1;
  while (*left == ' ' || *left == '\t') left++;
  while (*right == ' ' || *right == '\t') right++;

  char *end1 = nullptr;
  char *end2 = nullptr;
  long value = strtol(left, &end1, 10);
  unsigned long ts = strtoul(right, &end2, 10);
  if (end1 == left || end2 == right || ts == 0UL) return false;
  valueOut = value;
  tsOut = (uint32_t) ts;
  return true;
}

/***********************
 * Converts raw filename prefix DL->TR.
 * @param rawName Raw filename (typically DL*.TXT).
 * @return Trim filename.
 ***********************/
String trimFilenameFromRaw(const String &rawName) {
  if (rawName.length() >= 2 && rawName[0] == 'D' && rawName[1] == 'L') {
    String out = rawName;
    out.setCharAt(0, 'T');
    out.setCharAt(1, 'R');
    return out;
  }
  return "TR_" + rawName;
}

/***********************
 * Sidecar marker filename used to validate completed trim outputs.
 * @param trimName Trimmed data filename.
 * @return Marker filename.
 ***********************/
String trimReadyMarkerFilename(const String &trimName) {
  // Keep marker filename SD/FAT 8.3-safe.
  // Example: TR260427.TXT -> TR260427.OK
  int dot = trimName.lastIndexOf('.');
  if (dot > 0) {
    return trimName.substring(0, dot) + ".OK";
  }
  return trimName + ".OK";
}

/***********************
 * Returns true only when a trim completion marker exists and is non-empty.
 * @param trimName Trimmed data filename.
 * @return True when marker indicates a completed trim write.
 ***********************/
bool hasTrimReadyMarker(const String &trimName) {
  String markerName = trimReadyMarkerFilename(trimName);
  if (!SD.exists(markerName.c_str())) return false;
  File marker = SD.open(markerName.c_str(), FILE_READ);
  if (!marker) return false;
  unsigned long sz = (unsigned long) marker.size();
  marker.close();
  return sz > 0;
}

/***********************
 * Builds DL filename from epoch date.
 * @param epoch Unix epoch seconds.
 * @return Filename in DLYYMMDD.TXT format.
 ***********************/
String dlFilenameFromEpoch(uint32_t epoch) {
  DateTime dt(epoch);
  int yy = dt.year() % 100;
  int mm = dt.month();
  int dd = dt.day();
  char name[13];
  snprintf(name, sizeof(name), "DL%02d%02d%02d.TXT", yy, mm, dd);
  return String(name);
}

/***********************
 * Chooses trim target raw filename by date policy.
 * @return Selected DL filename or empty string if unavailable.
 ***********************/
String trimRawFilenameByDatePolicy() {
  uint32_t nowEpoch = Get_TimeStamp();
  uint32_t dayOffset = TRIM_USE_TODAY_FILENAME ? 0UL : 86400UL;
  if (nowEpoch <= dayOffset) return "";

  return dlFilenameFromEpoch(nowEpoch - dayOffset);
}

/***********************
 * Returns true when a raw DL filename is today's active acquisition file.
 * @param rawName Filename to test.
 * @return True when rawName matches myFilename or today's DL filename.
 ***********************/
bool isTodayRawFilename(const String &rawName) {
  if (rawName.length() == 0) return false;
  if (myFilename.length() > 0 && rawName == myFilename) return true;
  String todayName = dlFilenameFromEpoch(Get_TimeStamp());
  return (todayName.length() > 0 && rawName == todayName);
}

/***********************
 * Returns true when a TR filename is today's trimmed file.
 * @param trimName Filename to test.
 * @return True when trimName matches today's TR filename.
 ***********************/
bool isTodayTrimFilename(const String &trimName) {
  if (trimName.length() == 0) return false;
  String todayRaw = dlFilenameFromEpoch(Get_TimeStamp());
  if (todayRaw.length() == 0) return false;
  return trimName == trimFilenameFromRaw(todayRaw);
}

/***********************
 * Scans SD root and returns latest DL*.TXT by lexical date token.
 * @return Latest matching filename or empty string.
 ***********************/
String findLatestRawDlFilename() {
  File root = SD.open("/");
  if (!root || !root.isDirectory()) return "";

  String best = "";
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) {
      String n = String(entry.name());
      if (n.length() == 11 && n.startsWith("DL") && n.endsWith(".TXT")) {
        if (best.length() == 0 || n > best) best = n;
      }
    }
    entry.close();
  }
  root.close();
  return best;
}

/***********************
 * Scans SD root and returns latest TR*.TXT by lexical date token.
 * @return Latest matching filename or empty string.
 ***********************/
String findLatestTrimmedTrFilename() {
  File root = SD.open("/");
  if (!root || !root.isDirectory()) return "";

  String best = "";
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) {
      String n = String(entry.name());
      if (n.length() == 11 && n.startsWith("TR") && n.endsWith(".TXT")) {
        if (best.length() == 0 || n > best) best = n;
      }
    }
    entry.close();
  }
  root.close();
  return best;
}

/***********************
 * Determines preferred READY_TO_UPLOAD filename for this WiFi window.
 * @return Upload candidate filename.
 ***********************/
String determineReadyUploadFilename() {
  String rawName = trimRawFilenameByDatePolicy();
  if (rawName.length() > 0) {
    String trimName = trimFilenameFromRaw(rawName);
    if (SD.exists(trimName.c_str())) return trimName;
    if (!TRIM_USE_TODAY_FILENAME) return "";
  }
  String latestTr = findLatestTrimmedTrFilename();
  if (latestTr.length() > 0 && (TRIM_USE_TODAY_FILENAME || !isTodayTrimFilename(latestTr))) return latestTr;
  if (TRIM_USE_TODAY_FILENAME && myFilename.length() > 0 && SD.exists(myFilename.c_str())) return myFilename;
  return "";
}

/***********************
 * Sends READY_TO_UPLOAD beacon when due and not yet acknowledged.
 ***********************/
void maybeSendReadyToUploadBeacon() {
  if (!wifiInitialized || !wifiModeActive) return;
  if (readyBeaconAcked || uploadCompletedThisWindow) return;
  uint32_t nowMs = millis();
  if ((int32_t)(nowMs - nextReadyBeaconMs) < 0) return;

  if (readyUploadFilename.length() == 0) readyUploadFilename = determineReadyUploadFilename();
  if (readyUploadFilename.length() == 0) return;

  unsigned long fileSize = 0UL;
  File f = SD.open(readyUploadFilename.c_str(), FILE_READ);
  if (f) {
    fileSize = (unsigned long)f.size();
    f.close();
  }

  String msg = String(READY_TO_UPLOAD_MESSAGE) + "," + deviceId + "," + readyUploadFilename + "," + String(fileSize) + "," + String((unsigned long)Get_TimeStamp());
  sendUdpMessage(msg, targetIp, UDP_TARGET_PORT);
  readyBeaconSendCount++;
  Serial.print(F("Sent I'm ready signal ("));
  Serial.print(readyBeaconSendCount);
  Serial.println(F(")"));
  Serial.print(F("READY payload: "));
  Serial.println(msg);
  if (!readyBeaconAcked) {
    Serial.println(F("Waiting for ACK_READY from bsm_network..."));
  }

  uint32_t jitter = (READY_BEACON_JITTER_MS > 0) ? (uint32_t)random(0L, (long)READY_BEACON_JITTER_MS + 1L) : 0UL;
  nextReadyBeaconMs = nowMs + READY_BEACON_INTERVAL_MS + jitter;
}

/***********************
 * Adds/merges a keep-interval for trim output.
 * @param startTs Interval start timestamp.
 * @param endTs Interval end timestamp.
 ***********************/
void addTrimInterval(uint32_t startTs, uint32_t endTs) {
  if (endTs <= startTs) return;
  if (trimIntervalCount == 0) {
    trimIntervals[0].start_ts = startTs;
    trimIntervals[0].end_ts = endTs;
    trimIntervalCount = 1;
    return;
  }
  TrimInterval &last = trimIntervals[trimIntervalCount - 1];
  if (startTs <= last.end_ts) {
    if (endTs > last.end_ts) last.end_ts = endTs;
    return;
  }
  if (trimIntervalCount >= MAX_TRIM_INTERVALS) {
    if (endTs > last.end_ts) last.end_ts = endTs;
    return;
  }
  trimIntervals[trimIntervalCount].start_ts = startTs;
  trimIntervals[trimIntervalCount].end_ts = endTs;
  trimIntervalCount++;
}

/***********************
 * Derives baseline/low thresholds from file calibration section.
 * @param inputName Raw SD filename to analyze.
 * @return CalibrationThresholds with ok=false when derivation fails.
 ***********************/
CalibrationThresholds deriveCalibrationThresholdsFromFile(const String &inputName) {
  CalibrationThresholds r = {false, 0UL, 0L, 0L, 0L, 0L};
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return r;

  if (in.size() == 0) {
    in.close();
    Serial.println(F("TRIM fail: empty file"));
    return r;
  }

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  bool haveFirst = false;
  long calMin = 0;
  long calMax = 0;
  uint32_t firstTs = 0;
  uint32_t bucketCount = 0;
  uint16_t bucketN = 0;

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    if (!haveFirst) {
      haveFirst = true;
      firstTs = ts;
      calMin = value;
      calMax = value;
    }
    if (ts > firstTs + TRIM_CALIBRATION_SECONDS) break;
    if (value < calMin) calMin = value;
    if (value > calMax) calMax = value;
    bucketN++;
    if (bucketN >= TRIM_CAL_BUCKET_SAMPLES) {
      bucketCount++;
      bucketN = 0;
    }
  }
  if (bucketN >= TRIM_CAL_BUCKET_MIN_SAMPLES) bucketCount++;
  in.close();
  if (!haveFirst) return r;
  if (bucketCount == 0) return r;

  uint16_t baselineKeep = (uint16_t) ((float) bucketCount * TRIM_CAL_BASELINE_LOWEST_FRACTION);
  if (baselineKeep < 3) baselineKeep = 3;
  if (baselineKeep > TRIM_CAL_BASELINE_BUCKET_MAX) baselineKeep = TRIM_CAL_BASELINE_BUCKET_MAX;

  in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return r;

  long lowMeans[TRIM_CAL_BASELINE_BUCKET_MAX];
  float lowSds[TRIM_CAL_BASELINE_BUCKET_MAX];
  uint16_t lowCount = 0;
  bucketN = 0;
  double bucketMean = 0.0;
  double bucketM2 = 0.0;

  auto finishBaselineBucket = [&]() {
    if (bucketN < TRIM_CAL_BUCKET_MIN_SAMPLES) {
      bucketN = 0;
      bucketMean = 0.0;
      bucketM2 = 0.0;
      return;
    }

    long m = (long) bucketMean;
    float sd = (bucketN > 1) ? (float) sqrt(bucketM2 / (double) (bucketN - 1)) : 0.0f;
    if (lowCount < baselineKeep) {
      lowMeans[lowCount] = m;
      lowSds[lowCount] = sd;
      lowCount++;
    } else {
      uint16_t maxIdx = 0;
      for (uint16_t i = 1; i < lowCount; i++) {
        if (lowMeans[i] > lowMeans[maxIdx]) maxIdx = i;
      }
      if (m < lowMeans[maxIdx]) {
        lowMeans[maxIdx] = m;
        lowSds[maxIdx] = sd;
      }
    }

    bucketN = 0;
    bucketMean = 0.0;
    bucketM2 = 0.0;
  };

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    if (ts > firstTs + TRIM_CALIBRATION_SECONDS) break;

    bucketN++;
    double delta = (double) value - bucketMean;
    bucketMean += delta / (double) bucketN;
    bucketM2 += delta * ((double) value - bucketMean);
    if (bucketN >= TRIM_CAL_BUCKET_SAMPLES) finishBaselineBucket();
  }
  finishBaselineBucket();
  in.close();

  if (lowCount < 2) return r;

  for (uint16_t i = 0; i + 1 < lowCount; i++) {
    for (uint16_t j = i + 1; j < lowCount; j++) {
      if (lowMeans[j] < lowMeans[i]) {
        long tm = lowMeans[i];
        lowMeans[i] = lowMeans[j];
        lowMeans[j] = tm;
        float tsd = lowSds[i];
        lowSds[i] = lowSds[j];
        lowSds[j] = tsd;
      }
    }
  }

  long baseline = lowMeans[lowCount / 2];
  if ((lowCount % 2) == 0 && lowCount >= 2) {
    baseline = (long) (((long long) lowMeans[(lowCount / 2) - 1] + (long long) lowMeans[lowCount / 2]) / 2LL);
  }
  for (uint16_t i = 0; i + 1 < lowCount; i++) {
    for (uint16_t j = i + 1; j < lowCount; j++) {
      if (lowSds[j] < lowSds[i]) {
        float tsd = lowSds[i];
        lowSds[i] = lowSds[j];
        lowSds[j] = tsd;
      }
    }
  }

  float baselineSd = lowSds[lowCount / 2];
  if ((lowCount % 2) == 0 && lowCount >= 2) {
    baselineSd = (lowSds[(lowCount / 2) - 1] + lowSds[lowCount / 2]) / 2.0f;
  }
  if (baselineSd <= 0.0f) baselineSd = 1.0f;
  float steadySdLimit = baselineSd * TRIM_CAL_STEADY_SD_MULTIPLIER;
  if (steadySdLimit < baselineSd + 1.0f) steadySdLimit = baselineSd + 1.0f;
  double aboveBaselineMin = (double) baseline + (double) baselineSd * (double) TRIM_CAL_BASELINE_SEPARATION_SD;
  if (aboveBaselineMin < (double) baseline + 1.0) aboveBaselineMin = (double) baseline + 1.0;

  in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return r;

  long lowMean = LONG_MAX;
  bucketN = 0;
  bucketMean = 0.0;
  bucketM2 = 0.0;

  auto finishLowBucket = [&]() {
    if (bucketN < TRIM_CAL_BUCKET_MIN_SAMPLES) {
      bucketN = 0;
      bucketMean = 0.0;
      bucketM2 = 0.0;
      return;
    }

    long m = (long) bucketMean;
    float sd = (bucketN > 1) ? (float) sqrt(bucketM2 / (double) (bucketN - 1)) : 0.0f;
    if (sd <= steadySdLimit && (double) m > aboveBaselineMin && m < lowMean) {
      lowMean = m;
    }

    bucketN = 0;
    bucketMean = 0.0;
    bucketM2 = 0.0;
  };

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    if (ts > firstTs + TRIM_CALIBRATION_SECONDS) break;

    bucketN++;
    double delta = (double) value - bucketMean;
    bucketMean += delta / (double) bucketN;
    bucketM2 += delta * ((double) value - bucketMean);
    if (bucketN >= TRIM_CAL_BUCKET_SAMPLES) finishLowBucket();
  }
  finishLowBucket();
  in.close();

  if (lowMean == LONG_MAX || lowMean < calMin || lowMean > calMax) return r;

  r.ok = true;
  r.firstTs = firstTs;
  r.baselineMean = baseline;
  r.lowCalibrationMean = lowMean;
  r.enterThreshold = lowMean;
  r.exitThreshold = lowMean;
  return r;
}

/***********************
 * Builds merged trim keep-intervals using event detection rules.
 * @param inputName Raw SD filename.
 * @param cal Thresholds derived from calibration section.
 * @return True on success.
 ***********************/
bool buildTrimIntervalsForFile(const String &inputName, const CalibrationThresholds &cal) {
  trimIntervalCount = 0;
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return false;

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  uint32_t firstTs = 0;
  uint32_t lastTs = 0;
  bool haveFirst = false;
  bool eventLatched = false;
  bool captureActive = false;
  uint32_t captureUntil = 0;
  uint32_t currentCaptureStart = 0;
  uint32_t aboveSince = 0;
  uint32_t belowSince = 0;
  uint32_t parsedRows = 0;
  uint32_t nextProgress = TRIM_PROGRESS_ROWS;

  while (readDataLine(in, line, sizeof(line))) {
    if (!parseDataCsvLine(line, value, ts)) continue;
    parsedRows++;
    if (!haveFirst) {
      haveFirst = true;
      firstTs = ts;
      addTrimInterval(firstTs, firstTs + TRIM_CALIBRATION_SECONDS);
    }
    lastTs = ts;

    if (ts <= firstTs + TRIM_CALIBRATION_SECONDS) continue;
    if (ts < firstTs + TRIM_START_GUARD_SECONDS) continue;

    if (value >= cal.enterThreshold) {
      if (aboveSince == 0) aboveSince = ts;
      belowSince = 0;
    } else if (value <= cal.exitThreshold) {
      if (belowSince == 0) belowSince = ts;
      aboveSince = 0;
    } else {
      aboveSince = 0;
      belowSince = 0;
    }

    if (!eventLatched && aboveSince != 0 && (ts - aboveSince) >= (uint32_t) TRIM_DEBOUNCE_SECONDS) {
      eventLatched = true;
      uint32_t preStart = (ts > TRIM_PRE_EVENT_SECONDS) ? (ts - TRIM_PRE_EVENT_SECONDS) : firstTs;
      if (preStart < firstTs) preStart = firstTs;
      if (!captureActive) {
        captureActive = true;
        currentCaptureStart = preStart;
      } else if (preStart < currentCaptureStart) {
        currentCaptureStart = preStart;
      }
      captureUntil = ts + TRIM_POST_EVENT_SECONDS;
      aboveSince = 0;
    }

    if (eventLatched && belowSince != 0 && (ts - belowSince) >= (uint32_t) TRIM_DEBOUNCE_SECONDS) {
      eventLatched = false;
      belowSince = 0;
    }

    if (captureActive && !eventLatched && ts >= captureUntil) {
      addTrimInterval(currentCaptureStart, captureUntil);
      captureActive = false;
    }

    if (parsedRows >= nextProgress) {
      Serial.print(F("TRIM analyze rows="));
      Serial.print(parsedRows);
      Serial.print(F(" intervals="));
      Serial.println(trimIntervalCount);
      nextProgress += TRIM_PROGRESS_ROWS;
    }
  }
  in.close();
  if (!haveFirst) return false;
  if (captureActive) {
    uint32_t endTs = (lastTs > captureUntil) ? captureUntil : lastTs;
    addTrimInterval(currentCaptureStart, endTs);
  }
  return true;
}

/***********************
 * Writes trimmed file by copying rows that fall inside keep-intervals.
 * @param inputName Source raw filename.
 * @param outputName Destination trim filename.
 * @return True on successful write.
 ***********************/
bool writeTrimmedFileFromIntervals(const String &inputName, const String &outputName) {
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return false;
  String markerName = trimReadyMarkerFilename(outputName);
  // Invalidate previous completion state before rewriting output.
  SD.remove(markerName.c_str());
  SD.remove(outputName.c_str());
  File out = SD.open(outputName.c_str(), FILE_WRITE);
  if (!out) {
    in.close();
    return false;
  }

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  int idx = 0;
  uint32_t total = 0;
  uint32_t kept = 0;
  uint32_t nextProgress = TRIM_PROGRESS_ROWS;

  while (readDataLine(in, line, sizeof(line))) {
    char parse[96];
    strncpy(parse, line, sizeof(parse) - 1);
    parse[sizeof(parse) - 1] = '\0';
    if (!parseDataCsvLine(parse, value, ts)) continue;

    total++;
    while (idx < trimIntervalCount && ts > trimIntervals[idx].end_ts) idx++;
    if (idx < trimIntervalCount && ts >= trimIntervals[idx].start_ts && ts <= trimIntervals[idx].end_ts) {
      out.println(line);
      kept++;
    }
    if (total >= nextProgress) {
      Serial.print(F("TRIM write rows="));
      Serial.print(total);
      Serial.print(F(" kept="));
      Serial.println(kept);
      nextProgress += TRIM_PROGRESS_ROWS;
    }
  }
  out.close();
  in.close();
  Serial.print(F("TRIM done rows="));
  Serial.print(total);
  Serial.print(F(" kept="));
  Serial.println(kept);
  File marker = SD.open(markerName.c_str(), FILE_WRITE);
  if (!marker) {
    Serial.println(F("TRIM fail: marker write"));
    return false;
  }
  marker.print(F("READY,"));
  marker.println(kept);
  marker.close();
  return true;
}

/***********************
 * Writes a small fallback trim file when event-threshold calibration fails.
 * Copies the first TRIM_FALLBACK_SECONDS of parseable raw rows, or all
 * parseable rows if the file is shorter than that.
 * @param inputName Source raw filename.
 * @param outputName Destination trim filename.
 * @return True when fallback file and ready marker were written.
 ***********************/
bool writeTrimFallbackCalibrationOnly(const String &inputName, const String &outputName) {
  File in = SD.open(inputName.c_str(), FILE_READ);
  if (!in) return false;
  String markerName = trimReadyMarkerFilename(outputName);
  SD.remove(markerName.c_str());
  SD.remove(outputName.c_str());
  File out = SD.open(outputName.c_str(), FILE_WRITE);
  if (!out) {
    in.close();
    return false;
  }

  char line[96];
  long value = 0;
  uint32_t ts = 0;
  uint32_t firstTs = 0;
  bool haveFirst = false;
  uint32_t total = 0;
  uint32_t kept = 0;

  while (readDataLine(in, line, sizeof(line))) {
    char parse[96];
    strncpy(parse, line, sizeof(parse) - 1);
    parse[sizeof(parse) - 1] = '\0';
    if (!parseDataCsvLine(parse, value, ts)) continue;
    total++;
    if (!haveFirst) {
      haveFirst = true;
      firstTs = ts;
    }
    if (ts > firstTs + TRIM_FALLBACK_SECONDS) break;
    out.println(line);
    kept++;
  }

  out.close();
  in.close();
  Serial.print(F("TRIM fallback rows="));
  Serial.print(total);
  Serial.print(F(" kept="));
  Serial.println(kept);

  File marker = SD.open(markerName.c_str(), FILE_WRITE);
  if (!marker) {
    Serial.println(F("TRIM fallback fail: marker write"));
    return false;
  }
  marker.print(F("PROBLEM,"));
  marker.println(kept);
  marker.close();
  return true;
}

/***********************
 * Writes a very small problem TR file when the date-policy raw file is unusable.
 * The tiny upload is intentional: it preserves the date-named TR file and signals
 * that the user should inspect that day.
 * @param rawName Expected raw filename.
 * @param reason Short marker reason.
 * @return True when problem file and ready marker were written.
 ***********************/
bool writeSmallProblemTrimFile(const String &rawName, const char *reason) {
  if (rawName.length() == 0) return false;

  String trimName = trimFilenameFromRaw(rawName);
  String markerName = trimReadyMarkerFilename(trimName);
  SD.remove(markerName.c_str());
  SD.remove(trimName.c_str());

  File out = SD.open(trimName.c_str(), FILE_WRITE);
  if (!out) {
    Serial.println(F("TRIM problem fail: open output"));
    return false;
  }

  uint32_t ts = Get_TimeStamp();
  uint32_t written = 0;
  uint32_t rows = 0;
  while (written < TRIM_PROBLEM_FILE_BYTES) {
    char line[48];
    int n = snprintf(line, sizeof(line), "0, %lu\n", (unsigned long)ts);
    if (n <= 0 || n >= (int)sizeof(line)) {
      out.close();
      Serial.println(F("TRIM problem fail: format"));
      return false;
    }
    size_t w = out.write((const uint8_t *)line, (size_t)n);
    if (w != (size_t)n) {
      out.close();
      Serial.println(F("TRIM problem fail: write"));
      return false;
    }
    written += (uint32_t)w;
    rows++;
  }
  out.close();

  File marker = SD.open(markerName.c_str(), FILE_WRITE);
  if (!marker) {
    Serial.println(F("TRIM problem fail: marker write"));
    return false;
  }
  marker.print(F("PROBLEM,"));
  marker.print(reason);
  marker.print(F(","));
  marker.print(rawName);
  marker.print(F(","));
  marker.println(rows);
  marker.close();

  Serial.print(F("TRIM problem file: "));
  Serial.print(trimName);
  Serial.print(F(" reason="));
  Serial.print(reason);
  Serial.print(F(" bytes="));
  Serial.print((unsigned long)written);
  Serial.print(F(" rows="));
  Serial.println((unsigned long)rows);
  return true;
}

/***********************
 * Ensures a TR file exists and is ready before WiFi upload session.
 * If TR exists and non-empty, skip re-trim; otherwise build it.
 * @return True when TR file is ready for transfer.
 ***********************/
bool ensureTrimmedFileReadyForWifi() {
  if (!sdReady) return false;

  String rawName = trimRawFilenameByDatePolicy();
  if (rawName.length() == 0) {
    Serial.println(F("TRIM fail: no date-policy raw target"));
    return false;
  }

  Serial.print(F("TRIM date target: "));
  Serial.println(rawName);

  String trimName = trimFilenameFromRaw(rawName);

  if (!TRIM_USE_TODAY_FILENAME && isTodayRawFilename(rawName)) {
    Serial.print(F("TRIM skip today raw: "));
    Serial.println(rawName);
    return true;
  }

  if (!SD.exists(rawName.c_str())) {
    if (SD.exists(trimName.c_str()) && hasTrimReadyMarker(trimName)) {
      Serial.print(F("TRIM ready problem: "));
      Serial.println(trimName);
      return true;
    }
    Serial.print(F("TRIM missing raw target: "));
    Serial.println(rawName);
    setLcdStatusLine1("Trim: no raw");
    return writeSmallProblemTrimFile(rawName, "MISSING_RAW");
  }

  Serial.print(F("TRIM raw target: "));
  Serial.println(rawName);

  // Check if raw file is empty; if so, skip trimming and proceed
  File rawFile = SD.open(rawName.c_str(), FILE_READ);
  if (rawFile) {
    if (rawFile.size() == 0) {
      rawFile.close();
      Serial.println(F("TRIM empty raw target"));
      setLcdStatusLine1("Trim: empty raw");
      return writeSmallProblemTrimFile(rawName, "EMPTY_RAW");
    }
    rawFile.close();
  }

  if (SD.exists(trimName.c_str())) {
    File f = SD.open(trimName.c_str(), FILE_READ);
    unsigned long sz = f ? (unsigned long) f.size() : 0UL;
    if (f) f.close();
    bool markerOk = hasTrimReadyMarker(trimName);
    if (sz > 0 && markerOk) {
      Serial.print(F("TRIM ready: "));
      Serial.println(trimName);
      return true;
    }
    if (sz > 0 && !markerOk) {
      Serial.println(F("TRIM incomplete: missing ready marker, rebuilding."));
    }
  }

  Serial.print(F("TRIM start raw="));
  Serial.print(rawName);
  Serial.print(F(" out="));
  Serial.println(trimName);
  setLcdStatusLine1("Trim: analyze");

  CalibrationThresholds cal = deriveCalibrationThresholdsFromFile(rawName);
  if (!cal.ok) {
    Serial.println(F("TRIM fail: calibration thresholds"));
    Serial.println(F("TRIM fallback: writing calibration-only TR file"));
    setLcdStatusLine1("Trim: fallback");
    if (!writeTrimFallbackCalibrationOnly(rawName, trimName)) {
      Serial.println(F("TRIM fallback fail"));
      setLcdStatusLine1("Trim: fail fb");
      return false;
    }
    return true;
  }
  Serial.print(F("TRIM thresholds baseline="));
  Serial.print(cal.baselineMean);
  Serial.print(F(" low="));
  Serial.println(cal.lowCalibrationMean);

  if (!buildTrimIntervalsForFile(rawName, cal)) {
    Serial.println(F("TRIM fail: interval build"));
    setLcdStatusLine1("Trim: fail int");
    return false;
  }
  Serial.print(F("TRIM intervals="));
  Serial.println(trimIntervalCount);

  setLcdStatusLine1("Trim: writing");
  if (!writeTrimmedFileFromIntervals(rawName, trimName)) {
    Serial.println(F("TRIM fail: write"));
    setLcdStatusLine1("Trim: fail wr");
    return false;
  }

  setLcdStatusLine1("Trim: complete");
  return true;
}

#if defined(WIFI_PROFILE_AIRLIFT)
// ----- AirLift auto-recovery (AirLift-only) ----------------------------------
// One-time cleanup of legacy SD-persistent state from earlier versions
// of this sketch. We no longer use RECOVCNT.TXT (retry counter) or EFUSE.DN
// (burn marker), and leaving them on the card causes stale-state confusion
// when one provisioning SD is moved across many boards. Removing them on
// every recovery boot is cheap (single SD.exists+SD.remove) and idempotent.
static void cleanLegacyRecoveryStateFiles() {
  if (SD.exists("RECOVCNT.TXT")) {
    SD.remove("RECOVCNT.TXT");
    Serial.println(F("[wifi] removed legacy RECOVCNT.TXT"));
  }
  if (SD.exists("EFUSE.DN")) {
    SD.remove("EFUSE.DN");
    Serial.println(F("[wifi] removed legacy EFUSE.DN"));
  }
}

#if WIFI_AUTO_EFUSE_BURN
// ESP32 EFUSE_BLK0 registers and the three VDD_SDIO bits we burn.
// Espressif factory-burns these on production WROOM modules; raw modules
// don't have them set, so flash voltage gets decided by the IO12 strap.
// Adafruit AirLifts don't pull IO12, so unburned chips boot ROM but the app
// won't run. Burning these three bits permanently overrides the strap to 3.3V.
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

// Returns true only when bits were actually burned this call (caller should
// reset to re-latch the flash voltage strap). Returns false on:
//   * chip already has all 3 bits set (no-op, returns false)
//   * connect / read / verify failure (safe to retry on next power cycle)
//
// No SD state is read or written — source of truth is the chip's actual
// efuse state read live every boot, which makes the same SD card usable
// across many boards without operator intervention.
static bool maybeBurnEfuse() {
  Serial.println(F("[efuse] checking chip state before flash"));
  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("Efuse check...  ");
    lcd.setCursor(0, 1); lcd.print("                ");
  }

  ESPFlasherInit(true, &Serial);
  if (ESPFlasherConnect() != ESP_LOADER_SUCCESS) {
    Serial.println(F("[efuse] flasher connect failed - skip burn"));
    if (printLCD) { lcd.setCursor(0, 1); lcd.print("Connect FAIL    "); }
    delay(2000);
    return false;
  }

  uint32_t rdata4 = 0;
  if (esp_loader_read_register(EFUSE_BLK0_RDATA4, &rdata4) != ESP_LOADER_SUCCESS) {
    Serial.println(F("[efuse] RDATA4 read failed - skip burn"));
    if (printLCD) { lcd.setCursor(0, 1); lcd.print("Read FAIL       "); }
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
    if (printLCD) { lcd.setCursor(0, 1); lcd.print("Already burned  "); }
    delay(1500);
    return false;
  }

  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("Efuse burn...   ");
    lcd.setCursor(0, 1); lcd.print("Do NOT power off");
  }
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
    if (printLCD) { lcd.setCursor(0, 0); lcd.print("Efuse FAIL      "); }
    delay(2000);
    return false;
  }

  uint32_t after = 0;
  if (esp_loader_read_register(EFUSE_BLK0_RDATA4, &after) != ESP_LOADER_SUCCESS
      || (after & wantMask) != wantMask) {
    Serial.print(F("[efuse] verify failed: after=0x")); Serial.println(after, HEX);
    if (printLCD) { lcd.setCursor(0, 0); lcd.print("Efuse VERIFY    "); }
    delay(2000);
    return false;
  }

  Serial.print(F("[efuse] burn verified: after=0x")); Serial.println(after, HEX);
  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("Efuse OK        ");
    lcd.setCursor(0, 1); lcd.print("Rebooting...    ");
  }
  delay(2000);
  return true;
}
#endif // WIFI_AUTO_EFUSE_BURN

/***********************
 * Probes the AirLift WiFi module. If WiFi.firmwareVersion() doesn't return a
 * sane string, attempts to reflash nina-fw from the SD card (NINAFW.BIN in the
 * root) and hardware-resets the R4 so the new firmware boots cleanly.
 *
 * Preconditions: WiFi.setPins() called; SD already initialised (sdReady set).
 * Behaviour when WiFi is dead:
 *   - SD not ready / NINAFW.BIN missing -> warns and returns.
 *   - Otherwise: live efuse read (burn if needed), then flash (MD5-verified
 *     internally), then NVIC reset. No retry counter; if the chip is genuinely
 *     unrecoverable the unit boot-loops until the operator pulls power.
 ***********************/
void checkAndMaybeFlashWiFi() {
#if WIFI_AUTO_RECOVERY
  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("WiFi check...   ");
    lcd.setCursor(0, 1); lcd.print("                ");
  }
  Serial.println(F("[wifi] checking AirLift firmware..."));
  String fw = WiFi.firmwareVersion();
  bool fwOk = (fw.length() >= 5) && (fw[0] != (char)0xFF) && (fw[0] != 0);
  if (fwOk) {
    Serial.print(F("[wifi] firmware OK: "));
    Serial.println(fw);
    if (printLCD) {
      lcd.setCursor(0, 0);
      lcd.print("WiFi fw: ");
      lcd.print(fw.substring(0, 7));
    }
    delay(800);
    return;
  }
  Serial.println(F("[wifi] firmware unreadable - reflash needed"));
  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("WiFi DEAD       ");
  }
  delay(800);
  if (!sdReady) {
    Serial.println(F("[wifi] sdReady=false; trying SD.begin for recovery"));
    sdReady = SD.begin(SD_CS);
  }
  if (!sdReady) {
    Serial.println(F("[wifi] no SD - cannot auto-flash"));
    if (printLCD) { lcd.setCursor(0, 1); lcd.print("No SD - manual  "); }
    delay(3000);
    return;
  }
  if (!SD.exists(NINA_FW_FILENAME)) {
    Serial.print(F("[wifi] "));
    Serial.print(NINA_FW_FILENAME);
    Serial.println(F(" not found on SD"));
    if (printLCD) { lcd.setCursor(0, 1); lcd.print("NINAFW.BIN n/f  "); }
    delay(3000);
    return;
  }
  cleanLegacyRecoveryStateFiles();
#if WIFI_AUTO_EFUSE_BURN
  // Live efuse read + burn-if-needed runs before the flash attempt. If a
  // burn fires, NVIC reset is required to re-latch the flash voltage strap;
  // the next boot's recovery flow will reflash nina-fw under correct voltage.
  if (maybeBurnEfuse()) {
    Serial.println(F("[efuse] burn done; resetting to apply strap"));
    delay(500);
    NVIC_SystemReset();
  }
#endif
  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("Flashing nina-fw");
    lcd.setCursor(0, 1); lcd.print("Do NOT power off");
  }
  Serial.println(F("[wifi] starting flash from SD..."));
  ESPFlasherInit(true, &Serial);
  esp_loader_error_t cerr = ESPFlasherConnect();
  if (cerr != ESP_LOADER_SUCCESS) {
    Serial.print(F("[wifi] ESPFlasherConnect failed: "));
    Serial.println(cerr);
    if (printLCD) {
      lcd.setCursor(0, 0); lcd.print("Flash connect   ");
      lcd.setCursor(0, 1); lcd.print("FAIL e="); lcd.print(cerr); lcd.print("        ");
    }
    delay(3000);
    return;
  }
  esp_loader_error_t ferr = ESPFlashBin(NINA_FW_FILENAME);
  if (ferr != ESP_LOADER_SUCCESS) {
    Serial.print(F("[wifi] ESPFlashBin failed: "));
    Serial.println(ferr);
    if (printLCD) {
      lcd.setCursor(0, 0); lcd.print("Flash FAIL      ");
      lcd.setCursor(0, 1); lcd.print("err="); lcd.print(ferr); lcd.print("           ");
    }
    delay(3000);
    return;
  }
  // Full success (including MD5 verify).
  if (printLCD) {
    lcd.setCursor(0, 0); lcd.print("Reflashed       ");
    lcd.setCursor(0, 1); lcd.print("Rebooting...    ");
  }
  Serial.println(F("[wifi] reflash done; rebooting in 2s"));
  delay(2000);
  NVIC_SystemReset();
#endif
}
#endif // WIFI_PROFILE_AIRLIFT

/***********************
 * Connects STA WiFi with retries.
 * @return True when connected and local IP assigned.
 ***********************/
bool connectWiFi() {
  int status = WiFi.status();
  if (status == WL_NO_MODULE) {
    Serial.println(F("WiFi module not detected."));
    return false;
  }

  // Use for naming, the 6-character UID string.
  if (networkHostname.length() == 0) {
    networkHostname = buildNetworkHostname();
  }
  Serial.print("Hostname: ");
  Serial.println(networkHostname);
  // Set hostname before WiFi.begin() so controller lists device consistently.
  WiFi.setHostname(networkHostname.c_str());
  delay(1000); // Short delay to ensure hostname is set before connection attempts

  unsigned long connectStart = millis();
  while (status != WL_CONNECTED) {
    if ((uint32_t)(millis() - connectStart) >= WIFI_CONNECT_TOTAL_TIMEOUT_MS) {
      Serial.println(F("WiFi connect timeout."));
      WiFi.disconnect();
      return false;
    }

    if (strlen(SECRET_PASS) == 0) {
      status = WiFi.begin(SECRET_SSID);
    } else {
      status = WiFi.begin(SECRET_SSID, SECRET_PASS);
    }
    unsigned long start = millis();
    while ((millis() - start) < 8000UL && WiFi.status() != WL_CONNECTED) {
      delay(200);
    }
    status = WiFi.status();
    if (status != WL_CONNECTED) {
      Serial.println(F("WiFi connect retry..."));
      delay(1000);
    }
  }

  unsigned long ipWaitStart = millis();
  while (WiFi.localIP() == IPAddress(0, 0, 0, 0) && (millis() - ipWaitStart) < 10000UL) {
    delay(100);
  }
  if (WiFi.localIP() == IPAddress(0, 0, 0, 0)) {
    Serial.println(F("WiFi connect failed: no DHCP address."));
    WiFi.disconnect();
    return false;
  }

  Serial.print(F("WiFi connected. Local IP: "));
  Serial.println(WiFi.localIP());
  return true;
}

/***********************
 * Resolves UDP target host/IP from secrets configuration.
 * @return True when targetIp is valid.
 ***********************/
bool resolveTargetIp() {
#ifdef UDP_TARGET_HOST
  if (WiFi.hostByName(UDP_TARGET_HOST, targetIp) == 1) {
    Serial.print(F("Resolved host to: "));
    Serial.println(targetIp);
    return true;
  }
#endif
  if (targetIp.fromString(UDP_TARGET_IP)) {
    return true;
  }
  return false;
}

/***********************
 * Prints WiFi diagnostics to Serial/LCD.
 ***********************/
void showWiFiInfo() {
  Serial.print(F("SSID: "));
  Serial.println(WiFi.SSID());
  Serial.print(F("IP: "));
  Serial.println(WiFi.localIP());
  Serial.print(F("RSSI: "));
  Serial.print(WiFi.RSSI());
  Serial.println(F(" dBm"));
  Serial.print(F("UDP local port: "));
  Serial.println(UDP_LOCAL_PORT);
  Serial.print(F("UDP target: "));
  Serial.print(targetIp);
  Serial.print(F(":"));
  Serial.println(UDP_TARGET_PORT);

  if (!printLCD) return;

  lcd.setCursor(0, 0);
  lcd.print("WiFi OK        ");
  lcd.setCursor(0, 1);
  lcd.print(WiFi.localIP().toString().substring(0, 16));
  delay(1200);

  lcd.setCursor(0, 0);
  lcd.print("UDP ");
  lcd.print(UDP_LOCAL_PORT);
  lcd.print("->");
  lcd.print(UDP_TARGET_PORT);
  lcd.print("   ");
  lcd.setCursor(0, 1);
  String uidLine = getLcdUidLineBase("BOTH");
  while (uidLine.length() < 16) uidLine += " ";
  lcd.print(uidLine.substring(0, 16));
  delay(1200);
}

/***********************
 * One-time startup WiFi self-check (connect, print info, disconnect).
 ***********************/
void runStartupWiFiCheck() {
  Serial.println(F("Startup WiFi check..."));

  if (!connectWiFi()) {
    Serial.println(F("Startup WiFi check failed: no connection."));
    if (printLCD) {
      lcd.setCursor(0, 0);
      lcd.print("WiFi check fail ");
      lcd.setCursor(0, 1);
      lcd.print("No connection   ");
      delay(3000);
    }
    return;
  }

  bool targetOk = resolveTargetIp();
  Serial.print(F("SSID: "));
  Serial.println(WiFi.SSID());
  Serial.print(F("IP: "));
  Serial.println(WiFi.localIP());
  Serial.print(F("RSSI: "));
  Serial.print(WiFi.RSSI());
  Serial.println(F(" dBm"));
  Serial.print(F("UDP local port: "));
  Serial.println(UDP_LOCAL_PORT);
  Serial.print(F("UDP target: "));
  if (targetOk) {
    Serial.print(targetIp);
  } else {
    Serial.print(F("UNRESOLVED("));
    Serial.print(UDP_TARGET_IP);
    Serial.print(F(")"));
  }
  Serial.print(F(":"));
  Serial.println(UDP_TARGET_PORT);

  if (printLCD) {
    lcd.setCursor(0, 0);
    String line1 = "WiFi " + WiFi.localIP().toString();
    while (line1.length() < 16) line1 += " ";
    lcd.print(line1.substring(0, 16));

    lcd.setCursor(0, 1);
    String line2 = "R" + String(WiFi.RSSI()) + " U:" + deviceID_6;
    while (line2.length() < 16) line2 += " ";
    lcd.print(line2.substring(0, 16));
    delay(1500);
  }

  // Host/controller SET_TIME is authoritative for RTC sync in this deployment.
  // Keep WiFi NTP disabled to avoid internet-time dependency.

  WiFi.disconnect();
  udp.stop();
  wifiInitialized = false;
  wifiModeActive = false;
  Serial.println(F("Startup WiFi check done."));
}

/***********************
 * Enters active WiFi command mode (connect + UDP bind).
 ***********************/
void enterWifiMode() {
  closeAcqDataFile();
  Serial.println(F("Entering WiFi mode"));
  setLcdStatusLine1("WiFi: connect");
  if (!connectWiFi()) {
    return;
  }
  if (!resolveTargetIp()) {
    Serial.println(F("Invalid UDP target config."));
    return;
  }
  if (startupWifiCheckPending) {
    // NTP sync intentionally disabled; controller-driven SET_TIME remains active.
    startupWifiCheckPending = false;
  }
  udp.begin(UDP_LOCAL_PORT);
  showWiFiInfo();
  wifiInitialized = true;
  wifiLowPowerStandby = false;
  wifiLastActivityTs = Get_TimeStamp();
  setLcdStatusLine1("WiFi: waiting");
}

/***********************
 * Leaves WiFi command mode and returns to local data state.
 ***********************/
void exitWifiMode() {
  closeAcqDataFile();
  Serial.println(F("Exiting WiFi mode"));
  udp.stop();
  WiFi.disconnect();
  wifiInitialized = false;
  if (haveLastAcqSample) {
    setLcdAcqLine1WithValue(false, lastAcqSampleValue);
  } else {
    setLcdStatusLine1("Data mode");
  }
}

/***********************
 * Sends remote file list over UDP control channel.
 * @param transferId Transfer correlation ID.
 * @param replyIp Destination IP for replies.
 * @param replyPort Destination UDP port for replies.
 ***********************/
void sendFileList(const String &transferId, const IPAddress &replyIp, uint16_t replyPort) {
  Serial.print(F("LIST_FILES begin: transferId="));
  Serial.print(transferId);
  Serial.print(F(" reply="));
  Serial.print(replyIp);
  Serial.print(F(":"));
  Serial.println(replyPort);

  if (!sdReady) {
    Serial.println(F("LIST_FILES error: SD not ready"));
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  int fileCount = 0;
  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    Serial.println(F("LIST_FILES error: cannot open root"));
    sendUdpMessage("ERROR," + transferId + ",SD_OPEN_FAILED,Cannot open root", replyIp, replyPort);
    return;
  }
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) fileCount++;
    entry.close();
  }
  root.close();

  Serial.print(F("LIST_FILES sending FILE_LIST_BEGIN count="));
  Serial.println(fileCount);
  sendUdpMessage("FILE_LIST_BEGIN," + transferId + "," + String(fileCount), replyIp, replyPort);
  root = SD.open("/");
  while (true) {
    File entry = root.openNextFile();
    if (!entry) break;
    if (!entry.isDirectory()) {
      String msg = "FILE_ITEM,";
      msg += transferId;
      msg += ",";
      msg += entry.name();
      msg += ",";
      msg += String((unsigned long) entry.size());
      msg += ",0";
      sendUdpMessage(msg, replyIp, replyPort);
    }
    entry.close();
  }
  root.close();
  sendUdpMessage("FILE_LIST_END," + transferId, replyIp, replyPort);
  Serial.println(F("LIST_FILES sent FILE_LIST_END"));
}

/***********************
 * Returns true when filename is safe to use on SD root path.
 ***********************/
bool isSafeSdFilename(const String &filename) {
  if (filename.length() == 0 || filename.length() > 40) return false;
  if (filename.indexOf('/') >= 0 || filename.indexOf('\\') >= 0) return false;
  if (filename.indexOf("..") >= 0) return false;
  return true;
}

/***********************
 * Deletes a specific file from SD card on controller request.
 * Command format: DELETE_FILE,<transferId>,<filename>
 ***********************/
void deleteFileFromSd(const String &transferId, const String &filenameIn, const IPAddress &replyIp, uint16_t replyPort) {
  if (!sdReady) {
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  String filename = filenameIn;
  filename.trim();
  if (filename.startsWith("/")) filename = filename.substring(1);

  if (!isSafeSdFilename(filename)) {
    sendUdpMessage("ERROR," + transferId + ",BAD_FILENAME," + filenameIn, replyIp, replyPort);
    return;
  }

  // Safety guard: do not delete the active acquisition file.
  if (filename.equalsIgnoreCase(myFilename)) {
    sendUdpMessage("ERROR," + transferId + ",ACTIVE_FILE," + filename, replyIp, replyPort);
    return;
  }

  String path = "/" + filename;
  bool exists = SD.exists(path.c_str()) || SD.exists(filename.c_str());
  if (!exists) {
    sendUdpMessage("ERROR," + transferId + ",FILE_NOT_FOUND," + filename, replyIp, replyPort);
    return;
  }

  bool removed = SD.remove(path.c_str());
  if (!removed) removed = SD.remove(filename.c_str());
  if (!removed) {
    sendUdpMessage("ERROR," + transferId + ",DELETE_FAILED," + filename, replyIp, replyPort);
    return;
  }

  Serial.print(F("SD delete OK: "));
  Serial.println(filename);
  sendUdpMessage("ACK_DELETE," + transferId + "," + filename, replyIp, replyPort);
}

/***********************
 * Streams one remote file over TCP using CHUNK headers.
 * @param transferId Transfer correlation ID.
 * @param filename Remote filename requested by controller.
 * @param controllerIp Controller IP for TCP connect.
 * @param controllerTcpPort Controller TCP listening port.
 * @param startOffset Byte offset for resume/start.
 * @param replyIp UDP reply IP.
 * @param replyPort UDP reply port.
 ***********************/
void sendFileOverTcp(
  const String &transferId,
  const String &filename,
  const IPAddress &controllerIp,
  uint16_t controllerTcpPort,
  unsigned long startOffset,
  const IPAddress &replyIp,
  uint16_t replyPort
) {
  setLcdStatusLine1("Xfer: start");
  if (!sdReady) {
    sendUdpMessage("ERROR," + transferId + ",SD_NOT_READY,SD init failed", replyIp, replyPort);
    return;
  }

  String path = filename;
  if (!path.startsWith("/")) path = "/" + path;
  File file = SD.open(path.c_str(), FILE_READ);
  if (!file) {
    sendUdpMessage("ERROR," + transferId + ",FILE_NOT_FOUND," + filename, replyIp, replyPort);
    return;
  }

  unsigned long fileSize = (unsigned long) file.size();
  if (startOffset > fileSize || !file.seek(startOffset)) {
    file.close();
    sendUdpMessage("ERROR," + transferId + ",BAD_OFFSET," + String(startOffset), replyIp, replyPort);
    return;
  }

  String info = "FILE_INFO,";
  info += transferId;
  info += ",";
  info += filename;
  info += ",";
  info += String(fileSize);
  info += ",";
  info += String((unsigned long) FILE_CHUNK_SIZE);
  sendUdpMessage(info, replyIp, replyPort);

  WiFiClient client;
  if (!client.connect(controllerIp, controllerTcpPort)) {
    file.close();
    sendUdpMessage("ERROR," + transferId + ",TCP_CONNECT_FAILED," + String(controllerTcpPort), replyIp, replyPort);
    return;
  }
  setLcdStatusLine1("Xfer: sending");

  uint8_t buffer[FILE_CHUNK_SIZE];
  char header[128];
  unsigned long offset = startOffset;
  unsigned long chunkIndex = 0;
  uint32_t fullCrc = 0;
  unsigned long nextProgressOffset = startOffset + 262144UL;  // 256 KB

  while (offset < fileSize) {
    if (!client.connected()) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_DISCONNECTED," + String(offset), replyIp, replyPort);
      return;
    }

    size_t toRead = FILE_CHUNK_SIZE;
    unsigned long remaining = fileSize - offset;
    if (remaining < toRead) toRead = remaining;

    int bytesRead = file.read(buffer, (int) toRead);
    if (bytesRead <= 0) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",FILE_READ_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    uint32_t chunkCrc = crc32Update(0, buffer, (size_t) bytesRead);
    fullCrc = crc32Update(fullCrc, buffer, (size_t) bytesRead);

    snprintf(
      header, sizeof(header),
      "CHUNK,%s,%lu,%lu,%d,%08lX\n",
      transferId.c_str(),
      chunkIndex,
      offset,
      bytesRead,
      (unsigned long) chunkCrc
    );
    if (client.print(header) <= 0) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_HEADER_WRITE_FAILED," + String(offset), replyIp, replyPort);
      return;
    }

    size_t written = 0;
    unsigned long writeStart = millis();
    while (written < (size_t) bytesRead) {
      if (!client.connected()) break;
      int n = client.write(buffer + written, (size_t) bytesRead - written);
      if (n > 0) {
        written += (size_t) n;
        writeStart = millis();
      } else {
        if ((millis() - writeStart) > 5000UL) break;
        delay(1);
      }
    }
    if (written != (size_t) bytesRead) {
      client.stop();
      file.close();
      sendUdpMessage("ERROR," + transferId + ",TCP_WRITE_TIMEOUT," + String(offset), replyIp, replyPort);
      return;
    }

    offset += (unsigned long) bytesRead;
    chunkIndex++;

    if (offset >= nextProgressOffset || offset >= fileSize) {
      Serial.print(F("XFER "));
      Serial.print(offset);
      Serial.print(F("/"));
      Serial.println(fileSize);
      nextProgressOffset += 262144UL;
    }
  }

  String eof = "EOF,";
  eof += transferId;
  eof += ",";
  eof += String(fileSize);
  eof += ",";
  eof += crc32Hex(fullCrc);
  eof += "\n";
  client.print(eof);
  client.stop();
  file.close();
  sendUdpMessage("FILE_SENT," + transferId + "," + String(fileSize) + "," + crc32Hex(fullCrc), replyIp, replyPort);
  uploadCompletedThisWindow = true;
  readyBeaconAcked = true;
  Serial.print(F("UPLOAD COMPLETE: "));
  Serial.print(filename);
  Serial.print(F(" ("));
  Serial.print(fileSize);
  Serial.println(F(" bytes)"));
  setLcdStatusLine1("Xfer: done");
  delay(2000);
  setLcdStatusLine1("WiFi: waiting");
}

/***********************
 * Handles inbound UDP control commands while in WiFi mode.
 * Commands include polling, list files, start file transfer, and RTC sync.
 ***********************/
void serviceWifiCommands() {
  wifiCommandHandled = false;
  if (!wifiInitialized) return;
#if defined(WIFI_PROFILE_AIRLIFT)
  // WiFiNINA on AirLift can miss packets if parsePacket() is called too rapidly.
  delay(10);
#endif
  int packetSize = udp.parsePacket();
  if (packetSize <= 0) return;

  char incoming[192];
  int n = udp.read(incoming, sizeof(incoming) - 1);
  if (n < 0) return;
  incoming[n] = '\0';
  while (n > 0 && (incoming[n - 1] == '\n' || incoming[n - 1] == '\r' || incoming[n - 1] == ' ')) {
    incoming[n - 1] = '\0';
    n--;
  }
  wifiCommandHandled = true;

  IPAddress remoteIp = udp.remoteIP();
  uint16_t remotePort = udp.remotePort();
  Serial.print(F("UDP CMD from "));
  Serial.print(remoteIp);
  Serial.print(F(":"));
  Serial.print(remotePort);
  Serial.print(F(" -> "));
  Serial.println(incoming);

  if (strcmp(incoming, POLL_MESSAGE) == 0) {
    String response = "ID,";
    response += deviceId;
    response += ",";
    response += WiFi.localIP().toString();
    response += ",";
    response += UDP_TARGET_IP;
    response += ",";
    response += String(UDP_TARGET_PORT);
    response += ",";
    response += getNetworkUid();
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_NET_UID_MESSAGE) == 0) {
    String netUid = getNetworkUid();
    String response = "NET_UID,";
    response += netUid;
    response += ",HOST=";
    response += networkHostname;
    response += ",MAC=";
    response += getWifiMacHex();
    Serial.print(F("GET_NET_UID -> "));
    Serial.print(netUid);
    Serial.print(F(" (HOST="));
    Serial.print(networkHostname);
    Serial.println(F(")"));
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_VERSION_MESSAGE) == 0) {
    Serial.println(F("GET_VERSION received"));
    String response = "VERSION,";
    response += getFirmwareVersion();
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_WIFI_POLICY_MESSAGE) == 0) {
    String response = String("WIFI_POLICY,POLICY=") + wifiPolicyName()
      + ",GRACE_MIN=" + String(wifiPolicyGraceMinutes)
      + ",WAKE_HOUR=" + String(wifiPolicyWakeHour);
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  if (strcmp(incoming, GET_TIME_MESSAGE) == 0) {
    DateTime nowRtc((uint32_t)0);
    bool ok = false;
    // Retry stable RTC reads first.
    for (uint8_t i = 0; i < 3 && !ok; ++i) {
      ok = readRtcStable(nowRtc);
      if (!ok) delay(40);
    }
    // If still failing, do a lightweight I2C/RTC recovery and try once more.
    if (!ok) {
      recoverI2CBus();
      Wire.end();
      delay(10);
      Wire.begin();
      Wire.setClock(50000);
      delay(50);
      if (beginRtcWithRetry(2)) {
        ok = readRtcStable(nowRtc);
      }
    }
    if (ok) {
      String response = "TIME,";
      response += String((unsigned long) nowRtc.unixtime());
      response += ",";
      response += nowRtc.timestamp(DateTime::TIMESTAMP_FULL);
      sendUdpMessage(response, remoteIp, remotePort);
    } else {
      Serial.println(F("GET_TIME failed: RTC read/recovery unsuccessful."));
      sendUdpMessage("ERR_TIME,READ_FAILED", remoteIp, remotePort);
    }
    return;
  }

  char parseBuf[192];
  strncpy(parseBuf, incoming, sizeof(parseBuf) - 1);
  parseBuf[sizeof(parseBuf) - 1] = '\0';
  char *fields[8] = {nullptr};
  int fieldCount = splitCsv(parseBuf, fields, 8);

  if (fieldCount >= 3 && strcmp(fields[0], ACK_READY_MESSAGE) == 0) {
    String ackUid = String(fields[1]);
    String ackFile = String(fields[2]);
    if (ackUid == deviceId) {
      readyBeaconAcked = true;
      if (ackFile.length() > 0) readyUploadFilename = ackFile;
      Serial.print(F("READY handshake complete after "));
      Serial.print(readyBeaconSendCount);
      Serial.println(F(" ready signal(s)."));
      Serial.print(F("READY ACK received for "));
      Serial.println(readyUploadFilename);
    }
    return;
  }

  if (fieldCount >= 2 && strcmp(fields[0], SET_WIFI_POLICY_MESSAGE) == 0) {
    uint8_t newPolicy = parseWifiPolicyName(fields[1]);
    uint16_t newGraceMin = wifiPolicyGraceMinutes;
    uint8_t newWakeHour = wifiPolicyWakeHour;
    for (int i = 2; i < fieldCount; ++i) {
      char *param = fields[i];
      char *eq = strchr(param, '=');
      if (!eq) continue;
      *eq = '\0';
      char *key = param;
      char *value = eq + 1;
      if (strcmp(key, "GRACE_MIN") == 0) {
        int val = atoi(value);
        if (val >= 0 && val <= 360) newGraceMin = (uint16_t)val;
      } else if (strcmp(key, "WAKE_HOUR") == 0) {
        int val = atoi(value);
        if (val >= 0 && val < 24) newWakeHour = (uint8_t)val;
      }
    }
    wifiPolicy = newPolicy;
    wifiPolicyGraceMinutes = newGraceMin;
    wifiPolicyWakeHour = newWakeHour;
    wifiLastActivityTs = Get_TimeStamp();
    String response = String("ACK_WIFI_POLICY,POLICY=") + wifiPolicyName()
      + ",GRACE_MIN=" + String(wifiPolicyGraceMinutes)
      + ",WAKE_HOUR=" + String(wifiPolicyWakeHour);
    sendUdpMessage(response, remoteIp, remotePort);
    Serial.print(F("WiFi policy set: "));
    Serial.print(wifiPolicyName());
    Serial.print(F(" grace_min="));
    Serial.print(wifiPolicyGraceMinutes);
    Serial.print(F(" wake_hour="));
    Serial.println(wifiPolicyWakeHour);
    return;
  }

  if (fieldCount >= 1 && strcmp(fields[0], LIST_FILES_MESSAGE) == 0) {
    String transferId = (fieldCount >= 2) ? String(fields[1]) : String("T0");
    Serial.print(F("LIST_FILES received transferId="));
    Serial.println(transferId);
    sendFileList(transferId, remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 3 && strcmp(fields[0], DELETE_FILE_MESSAGE) == 0) {
    String transferId = String(fields[1]);
    String filename = String(fields[2]);
    deleteFileFromSd(transferId, filename, remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 4 && strcmp(fields[0], START_FILE_MESSAGE) == 0) {
    String transferId = String(fields[1]);
    String filename = String(fields[2]);
    uint16_t tcpPort = (uint16_t) atoi(fields[3]);
    unsigned long offset = 0;
    if (fieldCount >= 5) {
      offset = strtoul(fields[4], nullptr, 10);
    }
    sendFileOverTcp(transferId, filename, remoteIp, tcpPort, offset, remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 3 && strcmp(fields[0], RESUME_MESSAGE) == 0) {
    String transferId = String(fields[1]);
    sendUdpMessage("ACK_RESUME_HINT," + transferId + ",USE_START_FILE_WITH_OFFSET", remoteIp, remotePort);
    return;
  }

  if (fieldCount >= 2 && strcmp(fields[0], SET_TIME_MESSAGE) == 0) {
    unsigned long epoch = strtoul(fields[1], nullptr, 10);
    if (epoch > 0) {
      RTC.adjust(DateTime((uint32_t) epoch));
      Update_TimeStamp_Cache_From_Epoch((uint32_t)epoch, "gateway-set-time");
      delay(50);
      DateTime verified((uint32_t)0);
      bool ok = readRtcStable(verified);
      if (ok) {
        uint32_t r = verified.unixtime();
        uint32_t diff = (r >= epoch) ? (r - epoch) : (epoch - r);
        if (diff <= 5UL) {
          sendUdpMessage("ACK_TIME," + String(epoch), remoteIp, remotePort);
          Serial.print(F("RTC set from controller epoch: "));
          Serial.println(epoch);
          Serial.print(F("RTC now: "));
          Serial.println(verified.timestamp(DateTime::TIMESTAMP_FULL));
        } else {
          sendUdpMessage("ERR_TIME,VERIFY_MISMATCH", remoteIp, remotePort);
          Serial.print(F("RTC verify mismatch. epoch="));
          Serial.print(epoch);
          Serial.print(F(" rtc="));
          Serial.println((unsigned long) r);
        }
      } else {
        sendUdpMessage("ERR_TIME,VERIFY_FAILED", remoteIp, remotePort);
        Serial.println(F("RTC verify failed after SET_TIME."));
      }
    } else {
      sendUdpMessage("ERR_TIME,BAD_EPOCH", remoteIp, remotePort);
    }
    return;
  }

  // PING: Simple connectivity check
  if (strcmp(incoming, PING_MESSAGE) == 0) {
    sendUdpMessage("PONG," + deviceId, remoteIp, remotePort);
    return;
  }

  // GET_STATUS: Report device status (uptime, mode, SD space, last data timestamp)
  if (strcmp(incoming, GET_STATUS_MESSAGE) == 0) {
    uint32_t uptime = millis() / 1000;
    String mode = wifiModeActive ? "WIFI" : (wifiLowPowerStandby ? "WIFI_STBY" : "DATA");
    uint32_t sdFree = 0; // TODO: SD.totalBytes() may not be available in all libraries
    uint32_t lastTs = Get_TimeStamp();
    String response = String("STATUS,UPTIME=") + String(uptime)
      + ",MODE=" + mode
      + ",WIFI_POLICY=" + wifiPolicyName()
      + ",WIFI_SLEEPING=" + String(wifiLowPowerStandby ? 1 : 0)
      + ",SD_FREE_KB=" + String(sdFree)
      + ",LAST_DATA_TS=" + String(lastTs)
      + ",BATTERY=N/A";
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // GET_CONFIG: Report current configuration (hours, device ID)
  if (strcmp(incoming, GET_CONFIG_MESSAGE) == 0) {
    String response = String("CONFIG,START_HOUR=") + String(START_HOUR)
      + ",END_HOUR=" + String(END_HOUR)
      + ",WIFI_POLICY=" + wifiPolicyName()
      + ",GRACE_MIN=" + String(wifiPolicyGraceMinutes)
      + ",WAKE_HOUR=" + String(wifiPolicyWakeHour)
      + ",DEVICE_ID=" + deviceId;
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // GET_DIAGNOSTICS: Report error counts and health status
  if (strcmp(incoming, GET_DIAGNOSTICS_MESSAGE) == 0) {
    DateTime now((uint32_t)0);
    bool rtcOk = readRtcStable(now);
    uint32_t cacheTs = Get_TimeStamp();
    uint32_t cacheAge = (rtcBaseUnixTs != 0) ? ((millis() - rtcBaseMs) / 1000UL) : 0UL;
    String response = "DIAG,RTC_OK=" + String(rtcOk ? 1 : 0)
      + ",RTC_TIME=" + String(rtcOk ? (unsigned long)now.unixtime() : 0UL)
      + ",CACHE_TIME=" + String((unsigned long)cacheTs)
      + ",CACHE_SOURCE=" + rtcCacheSource
      + ",CACHE_AGE_SEC=" + String((unsigned long)cacheAge);
    if (rtcOk) {
      long delta = (long)cacheTs - (long)now.unixtime();
      response += ",CACHE_RTC_DELTA_SEC=" + String(delta);
    } else {
      response += ",CACHE_RTC_DELTA_SEC=NA";
    }
    response += ",WIFI_POLICY=" + wifiPolicyName()
      + ",WIFI_LAST_ACTIVITY=" + String((unsigned long)wifiLastActivityTs)
      + ",WIFI_NEXT_PROBE=" + String((unsigned long)wifiNextStandbyProbeTs)
      + ",RTC_ERRORS=" + String(rtcErrorCount)
      + ",I2C_ERRORS=" + String(i2cErrorCount)
      + ",SD_ERRORS=" + String(sdErrorCount);
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // GET_LAST_DATA: Report current sensor reading and timestamp
  if (strcmp(incoming, GET_LAST_DATA_MESSAGE) == 0) {
    long data = 0; // TODO: Get_Data() may hang if ADC not ready
    uint32_t ts = Get_TimeStamp();
    String response = "LAST_DATA,VALUE=" + String(data) + ",TS=" + String(ts);
    sendUdpMessage(response, remoteIp, remotePort);
    return;
  }

  // SET_CONFIG: Set configuration parameters (e.g., SET_CONFIG,START_HOUR=8,END_HOUR=16)
  if (fieldCount >= 2 && strcmp(fields[0], SET_CONFIG_MESSAGE) == 0) {
    bool updated = false;
    for (int i = 1; i < fieldCount; ++i) {
      char *param = fields[i];
      char *eq = strchr(param, '=');
      if (eq) {
        *eq = '\0';
        char *key = param;
        char *value = eq + 1;
        if (strcmp(key, "START_HOUR") == 0) {
          uint8_t val = (uint8_t)atoi(value);
          if (val < 24) {
            START_HOUR = val;
            updated = true;
          }
        } else if (strcmp(key, "END_HOUR") == 0) {
          uint8_t val = (uint8_t)atoi(value);
          if (val < 24) {
            END_HOUR = val;
            updated = true;
          }
        }
      }
    }
    if (updated) {
      sendUdpMessage("ACK_CONFIG", remoteIp, remotePort);
      Serial.println(F("Config updated via SET_CONFIG"));
    } else {
      sendUdpMessage("ERR_CONFIG,INVALID_PARAMS", remoteIp, remotePort);
    }
    return;
  }

  // REBOOT: Reboot the device
  if (strcmp(incoming, REBOOT_MESSAGE) == 0) {
    sendUdpMessage("ACK_REBOOT", remoteIp, remotePort);
    Serial.println(F("Rebooting via REBOOT command"));
    closeAcqDataFile();
    delay(1000);
    NVIC_SystemReset();
    return;
  }

  // ENTER_DATA_MODE: Exit WiFi mode and return to data collection
  if (strcmp(incoming, ENTER_DATA_MODE_MESSAGE) == 0) {
    exitWifiMode();
    sendUdpMessage("ACK_ENTER_DATA_MODE", remoteIp, remotePort);
    return;
  }

  // GET_LOGS: Retrieve recent logs (not implemented, respond with not supported)
  if (strcmp(incoming, GET_LOGS_MESSAGE) == 0) {
    sendUdpMessage("LOGS,NOT_SUPPORTED", remoteIp, remotePort);
    return;
  }

  // CLEAR_ERRORS: Reset error counters
  if (strcmp(incoming, CLEAR_ERRORS_MESSAGE) == 0) {
    i2cErrorCount = 0;
    rtcErrorCount = 0;
    sdErrorCount = 0;
    sendUdpMessage("ACK_CLEAR_ERRORS", remoteIp, remotePort);
    Serial.println(F("Error counters cleared"));
    return;
  }
}

/***********************
 * Performs one acquisition batch and appends it to current data file.
 * @param unixTs Timestamp value recorded for this acquisition batch.
 ***********************/
void runAcquisitionCycle(uint32_t unixTs, bool inCalibrationPhase) {
  unsigned long sampleValue = 0;
  uint16_t linesInChunk = 0;
  if (!ensureAcqDataFileOpen(myFilename)) {
    Serial.println("Error opening file ");
    lcd.setCursor(0, 0);
    lcd.print("Can't open file!!");
    delay(2000);
    lcd.setCursor(0, 1);
    lcd.print("Figure it out!");
    delay(10000);
    return;
  }

  for (int i = 0; i < 300; i++) {
    sampleValue = Get_Data();
    if (!appendAcqLineToBuffer((long) sampleValue, unixTs)) {
      Serial.println(F("Error buffering data line."));
      closeAcqDataFile();
      return;
    }
    linesInChunk++;
    if (linesInChunk >= ACQ_LINES_PER_CHUNK) {
      if (!flushAcqBuffer(false)) {
        Serial.println(F("Error writing buffered chunk."));
        closeAcqDataFile();
        return;
      }
      linesInChunk = 0;
    }

    if (debug) {
      Serial.print(sampleValue);
      Serial.print(", ");
      Serial.println(unixTs);
    }
  }
  haveLastAcqSample = true;
  lastAcqSampleValue = (long) sampleValue;

  if (acqWriteLen > 0 && !flushAcqBuffer(false)) {
    Serial.println(F("Error writing trailing buffered chunk."));
    closeAcqDataFile();
    return;
  }
  uint32_t nowMs = millis();
  if ((uint32_t)(nowMs - lastAcqFlushMs) >= ACQ_FLUSH_INTERVAL_MS) {
    if (!flushAcqBuffer(true)) {
      Serial.println(F("Error flushing acquisition data."));
      closeAcqDataFile();
      return;
    }
  }

  if (shouldUpdateAcqLcd(inCalibrationPhase)) {
    setLcdAcqLine1WithValue(inCalibrationPhase, (long) sampleValue);
  }
}



////////////////////
//  Get_Data - encapsulate data access to test different ideas - for now, very simple - would it be faster if we didn't use it at all?
////
/***********************
 * Reads averaged ADC sample from load cell front-end.
 * @return Averaged ADC reading.
 ***********************/
long int Get_Data() {
  // return (scale.read()); // different mode of the amplifier? From chris Lange
  return scale.continuousReadAverage(myAVG);
}

////////////////////
//  Get_TimeStamp - encapsulate data in case we change libraries
////
/***********************
 * Reads current RTC unix timestamp.
 * @return Unix time in seconds.
 ***********************/
long int OLD_Get_TimeStamp() {
  /* GET CURRENT TIME FROM RTC */
  currenttime = RTC.now();
  // convert to raw unix value for return - could give options
  return (currenttime.unixtime());
}

/***********************
 * Get_TimeStamp - cached RTC read
 * Uses trusted boot/Gateway time plus elapsed millis.
 * Does not read RTC during data collection.
 * Returns Unix time in seconds.
 ***********************/
uint32_t Get_TimeStamp() {
  return Estimated_TimeStamp_From_Cache(millis());
}

////////////////////
//  Get_TimeStampString - encapsulate data in case we change the way we record time
////
/***********************
 * Reads current RTC time string.
 * @return RTC time string in HH:MM:SS style.
 ***********************/
String Get_TimeStampString() {
  currenttime = DateTime(Get_TimeStamp());
  return(currenttime.timestamp(DateTime::TIMESTAMP_TIME));
}

////////////////////
//  Get_TimeStampString - encapsulate data in case we change the way we record time
////
/***********************
 * Checks whether current unix timestamp is inside configured WiFi window.
 * @param unixTs Unix timestamp to test.
 * @param startHour Window start hour [0..23].
 * @param endHour Window end hour [0..23], exclusive when start<end.
 * @return True when timestamp falls within window semantics.
 ***********************/
bool IsBetweenHours(uint32_t unixTs, uint8_t startHour = START_HOUR, uint8_t endHour = END_HOUR) {
  uint32_t secOfDay = unixTs % 86400UL;
  uint32_t start = (uint32_t)startHour * 3600UL;
  uint32_t end   = (uint32_t)endHour   * 3600UL;
  return (secOfDay >= start && secOfDay < end);
}


///////////////////
// File name function - based on cached time - so that we have a new filename for every day "DL_MM_DD.txt"
/////////
/***********************
 * Builds daily data filename from cached date.
 * @return Filename in DLYYMMDD.TXT format.
 ***********************/
String rtnFilename() {
  DateTime nowRtc(Get_TimeStamp());
  int yy = nowRtc.year() % 100;
  int mm = nowRtc.month();
  int dd = nowRtc.day();

  char name[13]; // "DLYYMMDD.TXT" + null
  snprintf(name, sizeof(name), "DL%02d%02d%02d.TXT", yy, mm, dd);
  return String(name);
}

/***********************
 * Arduino initialization entrypoint.
 * Sets up peripherals, RTC, SD, diagnostics, and initial display state.
 ***********************/
void setup() {

  ////////// 
  // Set CS pins high as soon as we can - from TACUNA
  ////////
  pinMode(SRAM_CS, OUTPUT); 
  digitalWrite(SRAM_CS, HIGH);

  pinMode(SD_CS, OUTPUT); 
  digitalWrite(SD_CS, HIGH);

  pinMode(AD7193_CS, OUTPUT); 
  digitalWrite(AD7193_CS, HIGH);

#if defined(WIFI_PROFILE_AIRLIFT)
  pinMode(AIRLIFT_CS, OUTPUT);
  digitalWrite(AIRLIFT_CS, HIGH);
#endif

  // Communication settings
  Serial.begin(115200);
  delay(500); // give time for serial to start up

#if defined(WIFI_PROFILE_AIRLIFT)
  // AirLift Shield pin configuration. Must be set before any WiFi.* call.
  WiFi.setPins(AIRLIFT_CS, AIRLIFT_BUSY, AIRLIFT_RESET, AIRLIFT_GPIO0);
#endif

  Serial.println("setup lcd");
  deviceId = getChipIdHex();
  deviceID_6 = shortUidFromHash(deviceId, 6);
  networkHostname = buildNetworkHostname();
 

  Serial.print("Device ID: ");
  Serial.println(deviceId);

  ///////////////////////////
  // setup LCD
  ///////////////////////////
    // set up the LCD's number of columns and rows:
  lcd.begin(16, 2);

  lcd.setCursor(0,0);     // user feedback in the field
  lcd.print("Checking...");  
  delay(1000);
  lcd.setCursor(0,0);
  lcd.print("                "); 

  ///////////////////////////
  // setup ADC AD7193 on PCB from Tacuna code
  ///////////////////////////
  scale.setSPI(SPI);
    if(!scale.begin(AD7193_CS, PIN_SPI_MISO)) {
      Serial.println(F("AD7193 initialization failed!"));

    } else {
      scale.printAllRegisters();
      scale.setClockMode(AD7193_CLK_INT);
      scale.setRate(0x001);
      scale.setFilter(AD7193_MODE_SINC4);
      scale.enableNotchFilter(false);     // learn what this will do
      scale.enableChop(false);
      scale.enableBuffer(true);
      scale.rangeSetup(0, AD7193_CONF_GAIN_128);
      scale.channelSelect(AD7193_CH_0);
      Serial.println(F("AD7193 Initialized!"));
    }


  ///////////////////////////
  // RTC - Setup - turn on and off with flag
  //.    May 7, 2024 - disable 
  ///////////////////////////

  Serial.println("RTC setup");
  recoverI2CBus();
  Wire.end();
  delay(10);
  Wire.begin();
  Wire.setClock(50000);
  delay(200);
  if (!beginRtcWithRetry(3)) {
    Serial.println("RTC failed after retries.");
    lcd.print("                ");
    lcd.setCursor(0, 0);
    lcd.print("RTC begin fail! ");
    lcd.setCursor(0, 1);
    lcd.print("Check wiring    ");
    while (1) { delay(1000); }
  }

  DateTime rtcNow((uint32_t)0);
  bool rtcStable = readRtcStable(rtcNow, "boot-initial");
  bool rtcRunning = RTC.isrunning();

  if (!rtcRunning || !rtcStable) {
    Serial.println("RTC invalid/unset at boot. Trying recovery passes.");
    bool recovered = false;
    for (uint8_t pass = 0; pass < RTC_BOOT_RECOVERY_PASSES; ++pass) {
      Serial.print(F("RTC recovery pass "));
      Serial.print((unsigned int)(pass + 1));
      Serial.println(F("..."));

      recoverI2CBus();
      Wire.end();
      delay(10);
      Wire.begin();
      Wire.setClock(50000);
      delay(120);

      if (!beginRtcWithRetry(3)) {
        Serial.println(F("RTC begin failed in recovery pass."));
        continue;
      }

      DateTime passNow((uint32_t)0);
      bool passStable = readRtcStable(passNow, "boot-recover");
      bool passRunning = RTC.isrunning();
      if (passRunning && passStable) {
        rtcNow = passNow;
        recovered = true;
        Serial.print(F("RTC recovery pass succeeded. TIMESTAMP:\t"));
        Serial.println(rtcNow.timestamp(DateTime::TIMESTAMP_FULL));
        break;
      }
    }

    if (!recovered) {
      Serial.println("RTC still invalid after retries. Applying compile time (last resort).");
      RTC.adjust(DateTime(F(__DATE__), F(__TIME__)));
      delay(50);

      DateTime verify((uint32_t)0);
      if (!readRtcStable(verify, "boot-compile-fallback")) {
        Serial.println("RTC verify failed after compile-time adjust.");
        lcd.print("                ");
        lcd.setCursor(0, 0);
        lcd.print("RTC verify fail ");
        lcd.setCursor(0, 1);
        lcd.print("Can't proceed!     ");
        while (1) { delay(1000); } // Halt if we can't get a stable RTC read even after setting compile time, as this is critical for operation.
      }
      rtcNow = verify;
      Serial.print("RTC set. TIMESTAMP:\t");
      Serial.println(rtcNow.timestamp(DateTime::TIMESTAMP_FULL));
    } else {
      Serial.print("RTC running. TIMESTAMP:\t");
      Serial.println(rtcNow.timestamp(DateTime::TIMESTAMP_FULL));
    }
  } else {
    Serial.print("RTC running. TIMESTAMP:\t");
    Serial.println(rtcNow.timestamp(DateTime::TIMESTAMP_FULL));
  }

  // Prime cached timestamp base from the already-validated startup RTC value.
  Update_TimeStamp_Cache_From_Epoch(rtcNow.unixtime(), "boot-rtc");

  lcd.print("                ");
  lcd.setCursor(0, 0);
  lcd.print("RTC Running!    ");
  lcd.setCursor(0, 1);
  lcd.print(String(rtcNow.timestamp(DateTime::TIMESTAMP_FULL)).substring(0, 16));
  delay(1500);


  ///////////////////////////
  // setup SD Card - turn on and off with flag
  ///////////////////////////


  if (debug) {
    // myFilename = "Test_tm.txt";  // use this if you want a custom name
    myFilename = rtnFilename();  // use this if debugging and want to have the autonamed file - NEED RTC running to make it work
  } else {
    myFilename = rtnFilename();
  }

  Serial.print("Saving to: ");
  Serial.println(myFilename);

  // setup lcd for user feedback
  lcd.setCursor(0, 0);

  // initialize the SD card process with user feedback
  Serial.print("Initializing SD card...");

  delay(1000);

  // see if the card is present and can be initialized:
  sdReady = SD.begin(chipSelect);
  if (!sdReady) {
    Serial.println("Card failed, or not present");  // don't do anything more:
    lcd.print("                ");
    lcd.setCursor(0, 0);
    lcd.print("Card failed!");
    lcd.setCursor(1, 1);
    lcd.print("Disconnect!     ");
    delay(10000);

    while (1)
      ;
  } else {
    Serial.println("card initialized.");  // confirm that it is good to go
  }

#if defined(WIFI_PROFILE_AIRLIFT)
  // AirLift self-heal: if nina-fw is dead and the SD card has NINAFW.BIN,
  // burn the efuse strap (if needed) and reflash, then reset. No retry cap;
  // if the chip is truly unrecoverable the unit boot-loops until the operator
  // pulls power. AirLift-only; the R4 onboard WiFi (WIFI_PROFILE_R4_WIFI) has
  // its own firmware update path via the Arduino IDE / fwuploader.
  checkAndMaybeFlashWiFi();
#endif



  if (verbose) {  // give full feedback on status to the user

    float wt_Check01;  // track current weight
    float wt_Check02;  // track the raw values from load cell
    float wt_Check03;  // track the raw values from load cell

    Serial.println("Before setting up the scale:");
    Serial.print("read: \t\t\t");
    Serial.println(scale.singleConversion());  // print a raw reading from the ADC

    Serial.print("read average: \t\t");
    Serial.println(Get_Data());  // print the average of normal sample of readings from the ADC

    wt_Check01 = scale.singleConversion();
    Serial.print("Check 01: \t\t");
    Serial.println(wt_Check01);
    delay(200);
    wt_Check02 = scale.singleConversion();
    Serial.print("Check 02: \t\t");
    Serial.println(wt_Check02);
    delay(200);
    wt_Check03 = scale.singleConversion();
    Serial.print("Check 03: \t\t");
    Serial.println(wt_Check03);
    delay(200);

    if ((wt_Check01 == wt_Check02) & (wt_Check02 == wt_Check03)) {
      Serial.print("We have a problem; load cell always reads: ");
      Serial.println(wt_Check01);
      lcd.setCursor(1, 0);
      lcd.print("Load cell Problem!");
      lcd.setCursor(1, 1);
      lcd.print("Disconnnect!    ");
      delay(10000);
    } else {
      lcd.setCursor(0, 0);
      lcd.print("Load cell works");
      Serial.println("Load cell working properly.");
      delay(750);
    }

  }

  // turn off the lcd?
  if (!printLCD) {
    // lcd.noBacklight();
    // lcd.noDisplay();
  } else {
    lcd.setCursor(0, 0);
    lcd.print("Data:           ");
    setLcdUidLine(false);
    lcd.setCursor(6, 0);
    lcd.print(Get_Data());  // do this while we are messing with closing the datafile
  }

  myFilename = rtnFilename();
  Serial.print(F("Saving to: "));
  Serial.println(myFilename);

  if (countdown && printLCD) {
    lcd.setCursor(0, 0);
    lcd.print("Data:         ");
    int N = 10;
    for (int i = 1; i < N; i++) {
      lcd.setCursor(0, 1);
      lcd.print("Start in: ");
      lcd.print(N - i);
      lcd.print(" secs");
      delay(800);
    }
  }

  if (printLCD) {
    lcd.setCursor(0, 0);
    lcd.print("Data:           ");
    setLcdUidLine(false);
    lcd.setCursor(6, 0);
    lcd.print(Get_Data());  // do this while we are messing with closing the datafile
  }

  Serial.print(F("Setup complete. Version: "));
  Serial.println(VERSION);
}


/***********************
 * Main runtime state machine.
 * - startup calibration capture window
 * - no-acquisition WiFi window behavior
 * - optional trim + WiFi command mode
 * - normal acquisition outside WiFi window
 ***********************/
void loop() {
  tCounter = tCounter + 1;
  uint32_t unixTs = Get_TimeStamp();
  bool inWifiWindow = IsBetweenHours(unixTs);
  bool enteredWifiWindow = (inWifiWindow && !wasInWifiWindow);
  bool exitedWifiWindow = (!inWifiWindow && wasInWifiWindow);
  wasInWifiWindow = inWifiWindow;

  // Always capture 10 minutes of raw data after each reboot before any WiFi workflow.
  if (!startupCalWindowInitialized) {
    startupCalWindowInitialized = true;
    startupCalWindowComplete = false;
    startupCalWindowEndTs = unixTs + STARTUP_CAL_CAPTURE_SECONDS;
    bootedInWifiWindow = inWifiWindow;
    thisBootSatisfiedWifiRebootGate = bootedInWifiWindow;
    // If boot occurs inside WiFi window, this boot satisfies the reboot gate.
    // Otherwise, the gate is armed later on first window entry transition.
    wifiSessionArmed = thisBootSatisfiedWifiRebootGate;
    wifiWindowCycleInitialized = inWifiWindow;
    Serial.print(F("Startup capture begin. bootedInWifiWindow="));
    Serial.println(bootedInWifiWindow ? F("YES") : F("NO"));
    if (printLCD) {
      if (haveLastAcqSample) {
        setLcdAcqLine1WithValue(true, lastAcqSampleValue);
      }
      setLcdUidLine(true);
    }
  }

  if (!startupCalWindowComplete) {
    runAcquisitionCycle(unixTs, true);
    if (unixTs >= startupCalWindowEndTs) {
      startupCalWindowComplete = true;
      Serial.println(F("Startup capture complete."));
      if (printLCD) {
        if (haveLastAcqSample) {
          setLcdAcqLine1WithValue(false, lastAcqSampleValue);
        }
        setLcdUidLine(false);
      }
    }
    return;
  }

  if (enteredWifiWindow) {
    // New WiFi window cycle: require one reboot-calibration cycle unless this
    // very boot occurred inside the window and has already been calibrated.
    if (!wifiWindowCycleInitialized) {
      wifiSessionArmed = false;
      thisBootSatisfiedWifiRebootGate = false;
      wifiLowPowerStandby = false;
      wifiModeActive = false;
      wifiInitialized = false;
      wifiOutWindowSinceTs = 0;
      wifiNextStandbyProbeTs = 0;
      wifiIdleAnnounced = false;
      readyBeaconAcked = false;
      uploadCompletedThisWindow = false;
      readyUploadFilename = "";
      nextReadyBeaconMs = 0;
      readyBeaconSendCount = 0;
      resetWifiConnectRetryState();
      Write_Estimated_Time_To_RTC("wifi-window-entry");
      Serial.println(F("WiFi window entered: reboot/calibration required for this window."));
    }
    wifiWindowCycleInitialized = true;
  }

  if (exitedWifiWindow) {
    // Reset cycle state so next day's WiFi window requires a fresh reboot.
    wifiWindowCycleInitialized = false;
    wifiSessionArmed = false;
    thisBootSatisfiedWifiRebootGate = false;
    wifiLowPowerStandby = false;
    wifiNextStandbyProbeTs = 0;
    wifiIdleAnnounced = false;
    readyBeaconAcked = false;
    uploadCompletedThisWindow = false;
    readyUploadFilename = "";
    nextReadyBeaconMs = 0;
    readyBeaconSendCount = 0;
    resetWifiConnectRetryState();
    Serial.println(F("WiFi window exited: reboot gate reset for next window."));
  }

  // If already in live WiFi mode, keep servicing commands.
  if (wifiModeActive) {
    if (!inWifiWindow) {
      if (wifiOutWindowSinceTs == 0) {
        wifiOutWindowSinceTs = unixTs;
        Serial.println(F("WiFi out-of-window detected; debounce started."));
      }
      bool exitNow = (unixTs >= wifiOutWindowSinceTs + WIFI_EXIT_DEBOUNCE_SECONDS);
      if (exitNow) {
        exitWifiMode();
        wifiModeActive = false;
        wifiIdleAnnounced = false;
        wifiOutWindowSinceTs = 0;
        wifiLowPowerStandby = false;
        wifiNextStandbyProbeTs = 0;
      } else {
        // Keep processing commands during debounce window to tolerate transient RTC/window glitches.
        serviceWifiCommands();
        if (wifiCommandHandled) wifiLastActivityTs = unixTs;
      }
    } else {
      wifiOutWindowSinceTs = 0;
      serviceWifiCommands();
      if (wifiCommandHandled) wifiLastActivityTs = unixTs;
      maybeSendReadyToUploadBeacon();
      if (wifiPolicyMaySleep() && wifiLastActivityTs > 0 && unixTs >= (wifiLastActivityTs + wifiPolicyGraceSeconds()) && !wifiPolicyWakeTimeReached(unixTs)) {
        Serial.println(F("WiFi policy idle timeout -> standby"));
        exitWifiMode();
        wifiModeActive = false;
        wifiLowPowerStandby = true;
        wifiNextStandbyProbeTs = ((unixTs / 86400UL) * 86400UL) + ((uint32_t)wifiPolicyWakeHour * 3600UL);
        if (printLCD) setLcdStatusLine1("WiFi: standby");
      }
    }
    return;
  }

  // In WiFi window: acquisition must remain stopped.
  if (inWifiWindow) {
    closeAcqDataFile();
    if (!thisBootSatisfiedWifiRebootGate) {
      wifiSessionArmed = false;
      wifiLowPowerStandby = false;
      if (!wifiIdleAnnounced) {
        Serial.println(F("WiFi window active. Reboot required before TRIM/WiFi."));
        wifiIdleAnnounced = true;
      }
      if (printLCD) setLcdStatusLine1("WiFi: reboot req");
      delay(250);
      return;
    }
    if (!wifiSessionArmed) {
      if (!wifiIdleAnnounced) {
        Serial.println(F("WiFi window active. Waiting for reboot-armed session."));
        wifiIdleAnnounced = true;
      }
      if (printLCD) setLcdStatusLine1("WiFi: reboot req");
      delay(250);
      return;
    }

    if (wifiLowPowerStandby && wifiPolicy == WIFI_POLICY_MORNING_ONLY) {
      if (!wifiPolicyWakeTimeReached(unixTs)) {
        delay(250);
        return;
      }
      if (!canAttemptWifiConnect(unixTs)) {
        delay(250);
        return;
      }
      Serial.println(F("WiFi policy wake hour reached"));
      enterWifiMode();
      wifiModeActive = wifiInitialized;
      if (!wifiModeActive) {
        noteWifiConnectFailure(unixTs);
        wifiNextStandbyProbeTs = unixTs + WIFI_CONNECT_RETRY_INTERVAL_SECONDS;
        delay(250);
        return;
      }
      resetWifiConnectRetryState();
      Serial.println(F("WiFi standby wake -> active"));
      wifiLowPowerStandby = false;
      wifiOutWindowSinceTs = 0;
      wifiLastActivityTs = unixTs;
      return;
    }

    if (!canAttemptWifiConnect(unixTs)) {
      delay(250);
      return;
    }

    // Reboot-armed path: trim first, then open WiFi listener.
    if (!ensureTrimmedFileReadyForWifi()) {
      delay(1000);
      return;
    }
    enterWifiMode();
    wifiModeActive = wifiInitialized;
    if (wifiModeActive) {
      resetWifiConnectRetryState();
      // Keep session armed for the rest of the current WiFi window so
      // additional WiFi activity can continue without another reboot.
      wifiIdleAnnounced = false;
      wifiOutWindowSinceTs = 0;
      wifiLowPowerStandby = false;
      wifiLastActivityTs = unixTs;
      readyBeaconAcked = false;
      uploadCompletedThisWindow = false;
      readyUploadFilename = determineReadyUploadFilename();
      nextReadyBeaconMs = 0;
      readyBeaconSendCount = 0;
    } else {
      noteWifiConnectFailure(unixTs);
    }
    return;
  }

  // Outside WiFi window: normal acquisition.
  wifiIdleAnnounced = false;
  wifiLowPowerStandby = false;
  wifiNextStandbyProbeTs = 0;
  runAcquisitionCycle(unixTs, false);
}


// -- END OF FILE --
