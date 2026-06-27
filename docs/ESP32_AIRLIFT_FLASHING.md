# ESP32 AirLift Flashing

This procedure provisions the ESP32 AirLift WiFi board before loading the normal
`Arduino_WIFI_All` sketch. Use the standalone flasher sketch only for WiFi board
provisioning.

## Files Needed

The repo contains the standalone flasher here:

```text
sketches/Arduino_WIFI_Flasher/Arduino_WIFI_Flasher.ino
```

The validated NINA firmware file is here:

```text
sketches/Arduino_WIFI_Flasher/NINAFW.BIN
```

The flasher sketch also needs the bundled support source files:

```text
sketches/Arduino_WIFI_Flasher/src/ESPSerialFlasher/
```

Keep that `src/ESPSerialFlasher` folder with the flasher sketch. Do not copy
only `ESPSerialFlasher.h`; the sketch needs the full set of support files.

## SD Card Setup

Copy this file to the root/top level of the SD card:

```text
NINAFW.BIN
```

The SD card should contain:

```text
/NINAFW.BIN
```

The flasher sketch reads `NINAFW.BIN` from the Arduino's SD card, not from the
computer's sketch folder. The copy in the sketch folder is the source file to
put on the SD card.

## Flashing Procedure

1. Put `NINAFW.BIN` on the SD card root.
2. Insert the SD card into the Arduino/Tacuna board.
3. Open `sketches/Arduino_WIFI_Flasher/Arduino_WIFI_Flasher.ino` in the Arduino IDE.
4. Compile and upload the flasher sketch.
5. Open Serial Monitor at `115200`.
6. Let the flasher run without interrupting power.
7. After successful flashing, the board resets.
8. Confirm the AirLift firmware reports as OK.
9. Upload the normal `Arduino_WIFI_All.ino` sketch for data collection.

## Expected Output

For a board that needs provisioning, expect output like:

```text
[efuse] reading chip state
Connected to target
[efuse] RDATA4=...
[efuse] burning XPD_SDIO bits
[efuse] burn verified
```

After reset, expect the NINA firmware flash:

```text
Connected to target
Erasing flash (this may take a while)...
Start programming
Progress: 0,1,2,...,100,
Finished programming
Flash verified
[wifi] reflash done; rebooting in 2s
```

On the next boot, success should look like:

```text
[wifi] firmware OK: 3.3.0
```

## Notes

- An antenna is not required for flashing, because flashing uses wired
  communication to the ESP32. An antenna is needed for reliable WiFi connection
  testing after flashing.
- The efuse burn step is permanent. Do not interrupt power while the LCD or
  Serial Monitor says `Do NOT power off`.
- Once the AirLift board is provisioned, `Arduino_WIFI_All.ino` does not need
  the AirLift recovery/provisioning code merged into it.
- The flasher sketch is a provisioning tool, not the field data-collection
  sketch.
