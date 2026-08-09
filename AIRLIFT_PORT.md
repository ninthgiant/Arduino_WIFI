# AirLift Shield Port (`airlift-port` branch)

Ports the full `Arduino_WIFI.ino` (AD7193 acquisition + SD logging + UDP/TCP command protocol) from the Uno R4 WiFi's onboard ESP32-S3 (`WiFiS3`) to an external Adafruit AirLift Shield #4285 driven over SPI by `WiFiNINA`. The R4 Minima will work too — only the FQBN changes.

The whole application protocol stays identical, so the existing `bsm_network.py` and `bsm_web.py` Python tools continue to work unchanged.

## Hardware stack tested

```
[Arduino UNO R4 WiFi]                — USB-C, powers everything
[Adafruit AirLift Shield #4285]       — mounted on the R4 (with mods below)
[Custom AD7193 / mauck shield V3]     — on top of AirLift: LCD, AD7193 ADC, SRAM
[HiLetgo MicroSD breakout]            — on top of mauck shield, CS=D10
```

## Required hardware modifications

Two AirLift Shield pins collide with the mauck/SD stack. Without these mods, WiFi or SD logging will fail.

### Mod 1 — AirLift RESET: D5 → A0 (D14)

The mauck shield uses **D5 as LCD data line 4**. Every LCD update would pulse the ESP32 EN line and silently reset the AirLift.

- Cut the AirLift's `RST_JMP` solder bridge (D5 → ESP32 EN)
- Run a jumper wire from Arduino **A0** to the AirLift's **ESP32 EN pad**

Verify with a multimeter: continuity from R4's D5 pin to ESP32 EN pad should be **broken** after the cut.

### Mod 2 — AirLift CS: D10 → A1 (D15)

The HiLetgo SD breakout uses **D10 as CS** (Bob's original wiring). Two SPI peripherals can't share the same select line.

- Cut the AirLift's `CS_JMP` solder bridge (D10 → ESP32 GPIO5)
- Run a jumper wire from Arduino **A1** to the AirLift's **ESP32 CS pad** (GPIO5)

### Do NOT use A4 or A5 for these jumpers

On the R4 (just like Uno R3), **A4 = SDA and A5 = SCL**. As soon as `Wire.begin()` runs for the RTC, A4/A5 become I2C peripheral pins and stop responding to `pinMode`/`digitalWrite`. A probe sketch without `Wire` will mislead you into thinking A4/A5 are free; they are not.

### Other AirLift jumpers

Make sure the AirLift Shield's normally-optional jumpers are fully soldered:

- **MISO, MOSI, SCK** (SPI pads) — ship open by default on the AirLift; required for any SPI communication.
- **TX, RX** — only needed for direct ESP32 firmware re-flashing via the host MCU, not for normal operation.
- **G0** — leave open. ESP32 boots from flash via its on-shield pull-up.

## Pin map (R4 + AirLift + AD7193 mauck + HiLetgo SD)

| Arduino pin | Function | Notes |
|---|---|---|
| D0  | AD7193 CS (mauck) | Set HIGH at boot to deselect |
| D1  | SRAM CS (mauck) | Set HIGH at boot |
| D2-D5 | LCD data lines 7-4 (mauck) | D5 also was AirLift RESET (moved) |
| D6  | unused | HX711 PD_SCK pad on mauck, HX711 not populated |
| D7  | AirLift BUSY | also HX711 D_OUT pad (not populated) |
| D8  | LCD enable |  |
| D9  | LCD RS |  |
| D10 | **SD CS** (HiLetgo) | Was AirLift CS, now moved to A1 |
| D11 | SPI MOSI | shared SPI bus |
| D12 | SPI MISO | shared SPI bus |
| D13 | SPI SCK | shared SPI bus |
| **A0 / D14** | **AirLift RESET** | moved from D5 |
| **A1 / D15** | **AirLift CS** | moved from D10 |
| A2, A3 | available |  |
| A4 / D18 | I2C SDA | RTC — do not repurpose |
| A5 / D19 | I2C SCL | RTC — do not repurpose |

## Code changes vs Bob's original `Arduino_WIFI.ino`

1. `#include <WiFiS3.h>` → `#include <WiFiNINA.h>`
2. New AirLift pin defines (`AIRLIFT_CS=15`, `AIRLIFT_BUSY=7`, `AIRLIFT_RESET=14`, `AIRLIFT_GPIO0=-1`)
3. `WiFi.setPins(...)` called early in `setup()` before any other WiFi call
4. `pinMode(AIRLIFT_CS, OUTPUT); digitalWrite(AIRLIFT_CS, HIGH);` added next to the existing CS deselects (SRAM, SD, AD7193)
5. `delay(10)` before `udp.parsePacket()` in `serviceWifiCommands()` — works around a known WiFiNINA quirk on AirLift where rapid `parsePacket()` calls miss packets
6. `START_HOUR` / `END_HOUR` widened from `7/19` to `0/23` for bench testing — **restore to `7/19` for field deployment**

Nothing else in the application code changes: AD7193 acquisition, SD buffering / file rotation, UDP commands, and TCP file transfer are byte-for-byte identical to Bob's `Vers_01`.

## Building

```
arduino-cli core install arduino:renesas_uno
arduino-cli lib install LiquidCrystal RTClib SD Time WiFiNINA
arduino-cli compile --fqbn arduino:renesas_uno:unor4wifi .
arduino-cli upload --fqbn arduino:renesas_uno:unor4wifi --port COMxx .
```

`WiFiNINA` must be the **Adafruit fork** (the upstream Arduino fork lacks `setPins()` and doesn't compile on the Renesas `sam` arch).

## secrets.h

Copy `secrets.example.h` to `secrets.h` and fill in WiFi + UDP target. `secrets.h` is gitignored.

## Operational quirks

### 1. Cold boot → UDP-ready takes 3–4 minutes

The sketch's state machine has a multi-phase startup:

1. **Power up → setup()**: hardware init, SD mount, RTC sync from WiFi NTP
2. **Startup capture window**: 60 seconds of forced AD7193 acquisition into today's file (regardless of WiFi window) so we always have fresh calibration data
3. **TRIM yesterday's file**: post-processes `DL<yesterday>.TXT` into `TR<yesterday>.TXT` based on amplitude thresholds. Can take a couple minutes on large files.
4. **enterWifiMode()**: WiFi.begin → DHCP → udp.begin

UDP commands are only serviced once step 4 completes. If you `POLL_UID` too early, you get no reply — keep trying for a few minutes after a reset.

### 2. Sketch only services UDP inside the WiFi window

`START_HOUR`/`END_HOUR` (default 7-19 in Bob's original; widened to 0-23 in this branch for bench tests) gate WiFi mode. Outside the window the sketch is in pure AD7193 acquisition mode and does not service UDP at all.

There's a chicken-and-egg with `SET_TIME`: if you SET_TIME to a value outside the current window, the sketch transitions to data mode and you can no longer SET_TIME back. Only escape is a power cycle. For field deployment the window should match expected operator interaction time; for bench testing keep `0/23`.

### 3. WiFi mode is reboot-gated for each new window

When the sketch first **enters** a new WiFi window (transition from outside → inside), it requires a reboot before servicing commands. If you booted already inside the window (`bootedInWifiWindow=YES` in the boot log), this gate is satisfied automatically. If you booted outside and crossed into the window without rebooting, the sketch will print `WiFi window active. Waiting for reboot-armed session.` and idle until you cycle power.

### 4. WiFi idle timeout

After `WIFI_MAINTENANCE_IDLE_SECONDS` of inactivity inside the window, the sketch drops into a low-power standby that periodically wakes to probe for new commands (`WIFI_STANDBY_CHECK_INTERVAL_SECONDS`). First packet after standby may have higher latency. Subsequent packets are fast again.

### 5. `parsePacket()` needs idle time

WiFiNINA on AirLift drops packets if `parsePacket()` is called too rapidly. The 10ms delay we added in `serviceWifiCommands()` is the workaround. Don't remove it.

### 6. UDP replies go to the sender's source port

`POLL_UID`, `GET_*`, `ACK_*` all reply to `(remoteIp, remotePort)` — i.e. the source address of the incoming UDP packet, not to the configured `UDP_TARGET_IP:UDP_TARGET_PORT`. A test client that sends from socket A and listens on socket B will miss the reply. Use `udp_query.ps1` for ad-hoc probes — it sends and receives on the same socket.

(File-list messages and async notifications DO go to `UDP_TARGET_IP:UDP_TARGET_PORT`. See the `sendUdpMessage` call sites.)

### 7. Windows Firewall silently drops `python.exe` UDP

If you write Python test scripts that send UDP to the Arduino and they appear to do nothing — check whether Windows Firewall has blocked `python.exe`. The Python `socket.sendto()` returns success but no packet hits the wire. PowerShell's `System.Net.Sockets.UdpClient` is signed by Microsoft and goes through fine. All the test helpers in this repo use PowerShell for that reason.

### 8. Pre-existing stubs in Bob's code (not regressions from the port)

- `GET_LAST_DATA` returns `LAST_DATA,VALUE=0,TS=0` (TODO comment in source: "Get_TimeStamp() may hang if RTC bad")
- `GET_LOGS` returns `LOGS,NOT_SUPPORTED`
- `SD_FREE_KB` in `GET_STATUS` is always `0` (TODO: "SD.totalBytes() may not be available in all libraries")

These were stubs in Bob's `Vers_01` before the port and remain stubs.

## Validated commands

End-to-end exercised on the live R4 + AirLift + mauck + SD stack:

| Command | Status |
|---|---|
| `POLL_UID` | ✅ |
| `GET_NET_UID` | ✅ |
| `GET_VERSION` | ✅ returns `VERSION,2.21` |
| `GET_STATUS` | ✅ (SD_FREE_KB is stub) |
| `GET_CONFIG` | ✅ |
| `GET_DIAGNOSTICS` | ✅ |
| `GET_TIME` | ✅ |
| `PING` | ✅ |
| `LIST_FILES,<tid>` | ✅ multi-packet, returns FILE_LIST_BEGIN / FILE_ITEM × N / FILE_LIST_END |
| `START_FILE,<tid>,<file>,<tcpPort>` | ✅ TCP file transfer with CHUNK headers + EOF + FILE_SENT ack |
| `START_FILE,<tid>,<file>,<tcpPort>,<offset>` | ✅ resume from byte offset works |
| `DELETE_FILE,<tid>,<file>` (real) | ✅ returns ACK_DELETE |
| `DELETE_FILE` (nonexistent) | ✅ returns ERROR,FILE_NOT_FOUND |
| `DELETE_FILE` (active file) | ✅ returns ERROR,ACTIVE_FILE (safety guard) |
| `DELETE_FILE` (path traversal) | ✅ returns ERROR,BAD_FILENAME |
| `SET_TIME,<epoch>` | ✅ ACK_TIME, time updated |
| `SET_CONFIG,KEY=VAL[,...]` | ✅ ACK_CONFIG |
| `CLEAR_ERRORS` | ✅ ACK_CLEAR_ERRORS |
| `RESUME,<tid>,<file>` | ✅ returns ACK_RESUME_HINT,USE_START_FILE_WITH_OFFSET |
| `ENTER_DATA_MODE` | ✅ ACK sent then exits WiFi mode |
| `GET_LAST_DATA` | ⚠️ Stub (returns zeros) |
| `GET_LOGS` | ⚠️ Stub (returns NOT_SUPPORTED) |
| `REBOOT` | ⏸️ Not exercised (would disconnect mid-test) |
| Unknown command | ✅ Silently ignored |

## Bench test helpers (in this branch's root)

All PowerShell — see "Windows Firewall" quirk above for why.

- **`udp_query.ps1 -Ip <ip> -Port <port> -Msg <cmd> [-WaitMs <ms>]`** — send a UDP command, wait for reply on the same socket. Use this for single-shot commands.
- **`file_pull.ps1 -Filename <name> -Out <path> [-Ip <ip>] [-UdpPort <port>] [-TcpPort <port>] [-TransferId <id>]`** — full file transfer: opens TCP listener, sends `START_FILE`, parses CHUNK headers + raw bytes, writes to local file. Verifies `EOF` and `FILE_SENT`.
- **`file_pull_offset.ps1 -Offset <bytes> [...other params]`** — resume-style transfer from a byte offset.

Example session:

```powershell
# Identify device
.\udp_query.ps1 -Msg POLL_UID

# Look at file list
$c = New-Object System.Net.Sockets.UdpClient
$c.Client.ReceiveTimeout = 3000
$b = [System.Text.Encoding]::ASCII.GetBytes('LIST_FILES,L1')
$c.Send($b, $b.Length, '192.168.1.32', 2390) | Out-Null
$ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
$end = (Get-Date).AddSeconds(4)
while ((Get-Date) -lt $end) { try { Write-Host ([System.Text.Encoding]::ASCII.GetString($c.Receive([ref]$ep))) } catch {} }
$c.Close()

# Pull a file
.\file_pull.ps1 -Filename DL260517.TXT -Out today.csv

# Set the RTC
.\udp_query.ps1 -Msg "SET_TIME,$([DateTimeOffset]::Now.ToUnixTimeSeconds())"
```

## Differences vs the controller-side python tools

`bsm_network.py` and `bsm_web.py` use the same UDP/TCP protocol but with proper transferId tracking, retry/backoff, and scheduled polling. The PowerShell helpers above are bench tools, not production replacements. For actual operation, use the python tools against the same `Ip:UdpPort` (default `192.168.1.32:2390` in our test secrets).
