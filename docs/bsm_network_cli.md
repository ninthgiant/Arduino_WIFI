# bsm_network.py CLI Reference

This document explains how to run `bsm_network.py` and what each optional parameter does.

## Basic Usage

```bash
python3 bsm_network.py [options]
```

Show all runtime help:

```bash
python3 bsm_network.py --help
```

## Shared Network Profile

Both `bsm_network.py` and `bsm_web.py` now load shared network defaults from:

- `config/network_profile.json`

Switch environments by changing `active_profile` (`field` or `home`) in that file, then restart both apps.

You can also override the profile file path with:

```bash
BSM_NETWORK_PROFILE=/path/to/network_profile.json python3 bsm_network.py
```

## Common Modes

Listener mode (no discovery):

```bash
python3 bsm_network.py --no-discover
```

One-shot discovery + transfer:

```bash
python3 bsm_network.py --discover
```

Scheduled operation loop:

```bash
python3 bsm_network.py --scheduled --discover
```

Cloud upload once:

```bash
python3 bsm_network.py --cloud-once
```

## Option Reference

### General / Listener

- `-h, --help`: Show help and exit.
- `--bind BIND`: Local interface/IP to bind. Default: `192.168.10.1`.
- `--port PORT`: UDP listen port for packet listener/discovery socket. Default: `5005`.
- `--buffer-size BUFFER_SIZE`: Max UDP datagram size in bytes. Default: `2048`.
- `--csv-log CSV_LOG`: Append parsed listener payloads to CSV file.
- `--ack`: Send ACK back to packet sender in listener mode.
- `--host-label HOST_LABEL`: Host display label in startup logs. Default: `NORTH_END_WIFI`.

### Discovery Control

- `--discover`: Enable discovery workflow.
- `--no-discover`: Disable discovery workflow.
- `--discover-ip DISCOVER_IP`: Broadcast IP used for discovery poll. Default: `192.168.10.255`.
- `--discover-port DISCOVER_PORT`: Arduino UDP control port. Default: `8888`.
- `--discover-timeout DISCOVER_TIMEOUT`: Seconds to wait for discovery replies or READY beacons. Default: `70.0`, long enough to overlap the Arduino `READY_TO_UPLOAD` beacon interval plus jitter.
- `--discover-attempts DISCOVER_ATTEMPTS`: Number of poll broadcasts. Default: `15`.
- `--discover-interval DISCOVER_INTERVAL`: Seconds between poll broadcasts. Default: `0.6`.
- `--discover-csv DISCOVER_CSV`: Output CSV path for discovered device table.
- `--network-map NETWORK_MAP`: Optional JSON with AP routing defaults (`device_to_ap`, `device_to_burrow`, `ap_limits`, `default_ap`).
- `--runtime-ap-map RUNTIME_AP_MAP`: Optional JSON with live AP overrides (checked before `--network-map`).
- `--default-ap-id DEFAULT_AP_ID`: Fallback AP bucket when no mapping is found. Default: `DEFAULT`.
- `--default-ap-limit DEFAULT_AP_LIMIT`: Per-AP cap for fallback bucket. Default: `2`.
- `--max-concurrent-transfers MAX_CONCURRENT_TRANSFERS`: Global transfer cap; `0` auto-uses sum of AP limits.
- `--db-path DB_PATH`: SQLite path for network/device activity logs. Default: `data/bsm_network.db`.
- `--db-log` / `--no-db-log`: Enable/disable SQLite logging (enabled by default).

### Time Sync

- `--sync-time`: Send RTC sync (`SET_TIME`) before retrieval.
- `--no-sync-time`: Disable RTC sync.
- `--sync-time-only`: Discover + sync time only; skip data/file retrieval.
- `--time-offset-hours TIME_OFFSET_HOURS`: Offset applied to controller epoch before `SET_TIME`. Default: `-3`.

### Line Download Mode (non-file transfer path)

- `--download-command DOWNLOAD_COMMAND`: UDP command to trigger payload lines. Default: `DOWNLOAD_DATA`.
- `--download-lines DOWNLOAD_LINES`: Number of lines to capture per Arduino. Default: `4`.
- `--download-timeout DOWNLOAD_TIMEOUT`: Per-device wait timeout in seconds. Default: `120.0`.
- `--post-poll-wait POST_POLL_WAIT`: Delay after polling before retrieval starts. Default: `10.0`.
- `--download-dir DOWNLOAD_DIR`: Output directory for line-download CSV files. Default: `data`.

### File Transfer Mode

- `--transfer-latest-file`: After discovery, list remote files and transfer latest unsaved file.
- `--no-transfer-latest-file`: Disable file transfer step.
- `--prefer-file-prefix {TR,DL,ANY}`: Preferred file prefix selection. Default: `TR`.
- `--tr-only`: If prefix is `TR`, do not fall back to `DL`. Default: enabled.
- `--no-tr-only`: Allow fallback to `DL` when no eligible `TR` exists.
- `--transfer-latest-even-if-seen`: Fetch latest file even if already logged as received.
- `--file-day {yesterday,today,latest}`: Day targeting policy for file selection. Default: `yesterday`.
- `--file-list-timeout FILE_LIST_TIMEOUT`: Seconds to wait for `LIST_FILES` response. Default: `10.0`.
- `--file-output-dir FILE_OUTPUT_DIR`: Directory where transferred files are saved. Default: `data/files`.
- `--file-log-dir FILE_LOG_DIR`: Per-device transfer log directory. Default: `data/file_logs`.
- `--transfer-tolerant`: Allow partial file saves when EOF integrity checks fail.
- `--mark-partial-received`: When tolerant mode is enabled, mark partial transfers as received.

### Scheduled Window Mode

- `--scheduled`: Run repeating schedule loop.
- `--start-hour START_HOUR`: Discovery window start hour (0-23). Default: `7`.
- `--end-hour END_HOUR`: Discovery window end hour (0-23). Default: `19`.
- `--cycle-interval-sec CYCLE_INTERVAL_SEC`: Delay between in-window discovery cycles. Default: `180`.
- `--out-window-sleep-sec OUT_WINDOW_SLEEP_SEC`: Sleep between checks outside window. Default: `45`.

### Cloud Upload

- `--cloud-enabled`: Enable cloud upload window processing.
- `--no-cloud-enabled`: Disable cloud upload window processing.
- `--cloud-start CLOUD_START`: Cloud window start in `HHMM`. Default: `0100`.
- `--cloud-end CLOUD_END`: Cloud window end in `HHMM`. Default: `0400`.
- `--cloud-cycle-interval-sec CLOUD_CYCLE_INTERVAL_SEC`: Seconds between cloud cycles in window. Default: `300`.
- `--cloud-source-dir CLOUD_SOURCE_DIR`: Local directory to upload from. Default: `data/files`.
- `--cloud-sent-log CLOUD_SENT_LOG`: CSV ledger of uploaded files. Default: `data/cloud_sent_files.csv`.
- `--cloud-rclone-remote CLOUD_RCLONE_REMOTE`: rclone remote target (for remote uploads), e.g. `gdrive:`.
- `--cloud-rclone-base CLOUD_RCLONE_BASE`: Base remote folder/path. Default: `BSM_Uploads`.
- `--cloud-local-dir CLOUD_LOCAL_DIR`: Local sync destination path (alternative to rclone remote).
- `--cloud-once`: Run one cloud upload cycle immediately and exit.

## Practical Examples

Discover only (no file transfer, no cloud):

```bash
python3 bsm_network.py \
  --discover \
  --discover-csv data/discovered_devices.csv \
  --no-transfer-latest-file \
  --no-cloud-enabled \
  --post-poll-wait 0
```

Discover + transfer latest TR file for today:

```bash
python3 bsm_network.py \
  --discover \
  --transfer-latest-file \
  --prefer-file-prefix TR \
  --file-day today
```

Single-AP home test (2 concurrent transfers max via AP bucket):

```bash
python3 bsm_network.py \
  --discover \
  --network-map docs/network_map.example.json \
  --default-ap-id AP_HOME \
  --default-ap-limit 2 \
  --max-concurrent-transfers 2
```

Network map JSON keys (including field label mapping):

```json
{
  "default_ap": "AP_HOME",
  "ap_limits": { "AP_HOME": 2, "DEFAULT": 1 },
  "device_to_ap": {
    "31011F0B383136326B7F33354B573355": "AP_HOME"
  },
  "device_to_burrow": {
    "31011F0B383136326B7F33354B573355": "BURROW_01"
  }
}
```

Scheduled discover/transfer during daytime, cloud overnight:

```bash
python3 bsm_network.py \
  --scheduled \
  --discover \
  --start-hour 10 \
  --end-hour 18 \
  --cloud-enabled \
  --cloud-start 0100 \
  --cloud-end 0400
```

## Notes

- `--discover` and `--no-discover` are toggles; whichever appears last wins.
- Same behavior for `--sync-time`/`--no-sync-time`, `--cloud-enabled`/`--no-cloud-enabled`, and similar toggles.
- In scheduled mode, use `Ctrl+C` to stop.

## other notes - discovery parameter

--discover-attempts applies every time a discovery cycle runs.

In one-shot discovery (--discover without --scheduled): it controls how many poll broadcasts are sent in that single run.
In scheduled mode (--scheduled --discover): it applies inside each scheduled discovery cycle, every cycle.
So in scheduled mode:

cycle timing = --cycle-interval-sec and window (--start-hour/--end-hour)
per-cycle poll burst size = --discover-attempts (with spacing --discover-interval)

## other notes - add parser functionality
-it responds if a parameter is added to the 'python3 bsm_network.py'

`add_argument` defines which parameters `python3 bsm_network.py ...` accepts and how they’re interpreted.  
If you pass a defined parameter, `argparse` sets the corresponding `args` value.  
If you pass an undefined one, you get an “unrecognized arguments” error.

## data stored in csv-log
--csv-log stores parsed UDP payload packets from listener mode as CSV rows.

Columns written are:

received_at
src_ip
src_port
device_id
unix_time
sample
So each row is one parsed payload (device_id,unix_time,sample) plus receive metadata.
