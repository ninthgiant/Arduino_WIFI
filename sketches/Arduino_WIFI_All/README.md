# Arduino_WIFI_Combined

Combined sketch intended to support both current WiFi hardware paths with a compile-time profile switch.

Open this folder in Arduino IDE:

```text
sketches/Arduino_WIFI_Combined/Arduino_WIFI_Combined.ino
```

## WiFi Profile Selection

At the top of `Arduino_WIFI_Combined.ino`, select exactly one profile:

```cpp
#define WIFI_PROFILE_R4_WIFI 1
// #define WIFI_PROFILE_AIRLIFT 1
```

Use `WIFI_PROFILE_R4_WIFI` for the current Uno R4 WiFi onboard ESP32-S3 radio using `WiFiS3`.

Use `WIFI_PROFILE_AIRLIFT` for the Tacuna/Adafruit AirLift Shield path using `WiFiNINA`. The AirLift profile also enables:

- AirLift pin definitions
- `WiFi.setPins(...)`
- a short delay before `udp.parsePacket()` for the WiFiNINA/AirLift packet timing quirk

Only one profile may be enabled at a time.

## Secrets

Copy `secrets.example.h` to `secrets.h` in this folder before compiling.

## AirLift auto-recovery (AirLift profile only)

When `WIFI_PROFILE_AIRLIFT` is selected, the sketch self-heals a dead AirLift
on boot. After `SD.begin` succeeds, `checkAndMaybeFlashWiFi()` runs:

1. Probes `WiFi.firmwareVersion()` over SPI.
2. If the AirLift returns a sane string -> LCD shows `WiFi fw: 3.3.0`,
   sketch continues normally. A persistent retry counter (see below) is
   cleared on every healthy boot.
3. If the AirLift is unresponsive AND the SD card has `NINAFW.BIN` in the
   root, the R4 reflashes the ESP32 over its UART directly (MD5-verified),
   then `NVIC_SystemReset`s so the new firmware boots cleanly. ~150 s at
   921600 baud for a 1.33 MB nina-fw image.
4. A counter file `RECOVCNT.TXT` on the SD caps consecutive failed
   recoveries at 3 attempts. After the cap, recovery refuses to try again
   and shows `Recovery limit / hit - manual fix` on the LCD. Reset by
   deleting `RECOVCNT.TXT` from the SD card.

Set `WIFI_AUTO_RECOVERY` to `0` near the top of the sketch to disable.

### Fleet provisioning workflow

The whole fleet can be provisioned with one Arduino + one SD card by
swapping AirLift shields between boards.

1. **Prep one SD card:** copy `NINAFW.BIN` to the SD root — and nothing
   else. Use the copy shipped with this PR at
   `libraries/ESPSerialFlasher/extras/NINAFW.BIN` (1,333,248 bytes,
   MD5 `7b50dfbc97f488fce09e49a3bd8c779c`). This is the modernized
   `nina-fw 3.3.0` fork validated end-to-end with the auto-recovery
   flasher. **Do not substitute a different build** — older Adafruit
   stock images flash and verify but won't speak the WiFiNINA SPI
   protocol the way this sketch expects, so recovery will appear to
   succeed (flash OK, MD5 verified) but post-reboot `firmwareVersion()`
   keeps returning garbage and the unit boot-loops.
2. **For each board:** swap in the next AirLift, power on. The
   auto-recovery flow:
   - probes nina-fw → fails (factory-blank AirLift)
   - reads chip efuse state live via ESPFlasher
   - if `XPD_SDIO_{REG,FORCE,TIEH}` not all set → burns them (forces
     VDD_SDIO=3.3V regardless of IO12 strap, equivalent to Espressif's
     factory burn) → `NVIC_SystemReset` to re-latch the strap → next
     boot reads the now-burned state and skips the burn
   - if already burned (Adafruit factory boards or a previously-burned
     chip) → no-op on the burn → continues immediately
   - flashes `NINAFW.BIN`, MD5-verifies, `NVIC_SystemReset`
   - next boot: nina-fw runs correctly, sketch enters normal operation
3. **Move on to the next board.** No SD prep between boards — the
   sketch reads the chip's efuse state live every boot, so the same
   card works across the whole fleet. Operator watches the LCD or
   serial; if a board boot-loops on flash, power it down and pull it
   for manual debug.

The sketch does not maintain any SD-side recovery state. If you see
old `RECOVCNT.TXT` or `EFUSE.DN` files on a card from a previous
version of this sketch, the recovery path now removes them
automatically on first run. Set `WIFI_AUTO_EFUSE_BURN` to `0` at the
top of the sketch to disable the burn path entirely.

## Notes

This sketch starts from the production root `Arduino_WIFI.ino` and adds the Tacuna/AirLift WiFi communication differences behind compile-time guards. The root production sketch is unchanged.
