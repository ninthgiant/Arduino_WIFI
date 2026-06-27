from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

# Network defaults used by CLI flags below.
# Edit these in one place when moving to a different local network.
# DEFAULT_BIND_IP = "192.168.1.8"
DEFAULT_BIND_IP = "192.168.10.1" # will connect to all interfaces, but discovery will only work if the default route is on the same subnet as the Arduinos (e.g. if connected to a VPN that routes all traffic through it, discovery will fail since the broadcast will go out the VPN interface instead of the local subnet interface)
DEFAULT_DISCOVER_BROADCAST_IP = "192.168.10.255"
DEFAULT_LISTEN_PORT = 5005
DEFAULT_DISCOVER_PORT = 8888
DEFAULT_LOCAL_OFFSET = -3 # hrs offset from UTC, -4 is EDT in summer, -5 in winter, for ADT use -3, for AST use -4 year-round
DEFAULT_HOST_LABEL = "NORTH_END_IOT"
DEFAULT_START_HOUR = 7
DEFAULT_END_HOUR = 20
DEFAULT_CLOUD_START = "0100"
DEFAULT_CLOUD_END = "0400"
DEFAULT_PREFER_FILE_PREFIX = "TR"
DEFAULT_FILE_DAY = "yesterday"
DEFAULT_DISCOVER_CSV = "data/discovered_devices.csv"
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 5001
DEFAULT_AP_ID = "DEFAULT"
DEFAULT_AP_LIMIT = 2
DEFAULT_MAX_CONCURRENT_TRANSFERS = 0
DEFAULT_DISCOVER_TIMEOUT = 70.0
DEFAULT_DB_PATH = "data/bsm_network.db"
DEFAULT_NETWORK_MAP = ""
DEFAULT_RUNTIME_AP_MAP = ""
DEFAULT_PROFILE_PATH = "config/network_profile.json"
ACTIVE_NETWORK_PROFILE = "builtin"
ACTIVE_NETWORK_PROFILE_SOURCE = "builtin defaults"


def _coerce_int(value: object, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return fallback


def _coerce_float(value: object, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return fallback


def _apply_runtime_profile() -> None:
    global DEFAULT_BIND_IP, DEFAULT_DISCOVER_BROADCAST_IP, DEFAULT_DISCOVER_PORT
    global DEFAULT_HOST_LABEL, DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_LOCAL_OFFSET
    global DEFAULT_DISCOVER_CSV, DEFAULT_DB_PATH, DEFAULT_NETWORK_MAP, DEFAULT_RUNTIME_AP_MAP
    global ACTIVE_NETWORK_PROFILE, ACTIVE_NETWORK_PROFILE_SOURCE

    profile_path_raw = os.environ.get("BSM_NETWORK_PROFILE", DEFAULT_PROFILE_PATH)
    profile_path = Path(profile_path_raw).expanduser()
    if not profile_path.exists():
        return

    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: failed to read network profile '{profile_path}': {exc}")
        return

    if not isinstance(data, dict):
        print(f"Warning: network profile '{profile_path}' must be a JSON object.")
        return

    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        print(f"Warning: network profile '{profile_path}' missing 'profiles' object.")
        return

    active_name = str(data.get("active_profile") or data.get("active") or "").strip()
    if not active_name:
        print(f"Warning: network profile '{profile_path}' missing active_profile/active.")
        return

    profile = raw_profiles.get(active_name, {})
    if not isinstance(profile, dict):
        print(f"Warning: network profile '{profile_path}' has invalid profile '{active_name}'.")
        return

    DEFAULT_BIND_IP = str(profile.get("bind", DEFAULT_BIND_IP)).strip() or DEFAULT_BIND_IP
    DEFAULT_DISCOVER_BROADCAST_IP = str(profile.get("discover_ip", DEFAULT_DISCOVER_BROADCAST_IP)).strip() or DEFAULT_DISCOVER_BROADCAST_IP
    DEFAULT_DISCOVER_PORT = _coerce_int(profile.get("discover_port", DEFAULT_DISCOVER_PORT), DEFAULT_DISCOVER_PORT)
    DEFAULT_HOST_LABEL = str(profile.get("host_label", DEFAULT_HOST_LABEL)).strip() or DEFAULT_HOST_LABEL
    DEFAULT_WEB_HOST = str(profile.get("web_host", DEFAULT_WEB_HOST)).strip() or DEFAULT_WEB_HOST
    DEFAULT_WEB_PORT = _coerce_int(profile.get("web_port", DEFAULT_WEB_PORT), DEFAULT_WEB_PORT)
    DEFAULT_LOCAL_OFFSET = _coerce_float(profile.get("time_offset_hours", DEFAULT_LOCAL_OFFSET), DEFAULT_LOCAL_OFFSET)
    DEFAULT_DISCOVER_CSV = str(profile.get("discover_csv", DEFAULT_DISCOVER_CSV)).strip() or DEFAULT_DISCOVER_CSV
    DEFAULT_DB_PATH = str(profile.get("db_path", DEFAULT_DB_PATH)).strip() or DEFAULT_DB_PATH
    DEFAULT_NETWORK_MAP = str(profile.get("network_map", DEFAULT_NETWORK_MAP)).strip()
    DEFAULT_RUNTIME_AP_MAP = str(profile.get("runtime_ap_map", DEFAULT_RUNTIME_AP_MAP)).strip()
    ACTIVE_NETWORK_PROFILE = active_name
    ACTIVE_NETWORK_PROFILE_SOURCE = str(profile_path)


_apply_runtime_profile()


def build_normal_ops_argv(discover_csv: str = DEFAULT_DISCOVER_CSV) -> list[str]:
    return [
        "--scheduled",
        "--discover",
        "--ready-driven",
        "--skip-if-uploaded-today",
        "--discover-timeout",
        "50",
        "--discover-csv",
        discover_csv,
        "--transfer-latest-file",
        "--prefer-file-prefix",
        "TR",
        "--file-day",
        "yesterday",
        "--sync-time",
        "--transfer-tolerant",
    ]


def build_poll_now_argv(discover_csv: str = DEFAULT_DISCOVER_CSV) -> list[str]:
    return [
        "--discover",
        "--no-ready-driven",
        "--discover-attempts",
        "70",
        "--discover-interval",
        "0.6",
        "--discover-timeout",
        "50",
        "--discover-csv",
        discover_csv,
        "--no-transfer-latest-file",
        "--no-sync-time",
        "--no-cloud-enabled",
        "--post-poll-wait",
        "0",
    ]


def build_force_upload_argv(device_ip: str, discover_csv: str = DEFAULT_DISCOVER_CSV) -> list[str]:
    return [
        "--discover",
        "--discover-attempts",
        "3",
        "--discover-timeout",
        "8",
        "--discover-interval",
        "0.3",
        "--post-poll-wait",
        "0",
        "--discover-csv",
        discover_csv,
        "--transfer-latest-file",
        "--prefer-file-prefix",
        "TR",
        "--file-day",
        "latest",
        "--transfer-latest-even-if-seen",
        "--no-sync-time",
        "--transfer-tolerant",
        "--mark-partial-received",
        "--no-cloud-enabled",
        "--discover-ip",
        device_ip,
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UDP listener/controller for Arduino BSM payloads"
    )
    parser.set_defaults(
        discover=True,
        ready_driven=True,
        transfer_latest_file=True,
        skip_if_uploaded_today=True,
        sync_time=True,
        cloud_enabled=False,
        scheduled=True,
        db_log=True,
    )
    parser.add_argument("--bind", default=DEFAULT_BIND_IP, help=f"Local interface/IP to bind (default: {DEFAULT_BIND_IP})")
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT, help=f"UDP port to listen on (default: {DEFAULT_LISTEN_PORT})")
    parser.add_argument("--buffer-size", type=int, default=2048, help="Max UDP datagram size in bytes (default: 2048)")
    parser.add_argument("--csv-log", default="", help="Optional CSV file path to append parsed packets")
    parser.add_argument("--ack", action="store_true", help="Reply to sender with a simple ACK message")
    parser.add_argument("--host-label", default=DEFAULT_HOST_LABEL, help=f"Name shown in startup output for the host machine (default: {DEFAULT_HOST_LABEL})")
    parser.add_argument("--discover", action="store_true", help="Broadcast poll request and print discovered Arduino unique IDs")
    parser.add_argument("--no-discover", action="store_false", dest="discover", help="Disable discovery mode")
    parser.add_argument(
        "--ready-driven",
        action="store_true",
        help="Listen for READY_TO_UPLOAD beacons and ACK them instead of driving broadcast POLL discovery.",
    )
    parser.add_argument(
        "--no-ready-driven",
        action="store_false",
        dest="ready_driven",
        help="Disable READY-driven discovery and use broadcast POLL behavior.",
    )
    parser.add_argument("--discover-ip", default=DEFAULT_DISCOVER_BROADCAST_IP, help=f"Broadcast IP for discovery polls (default: {DEFAULT_DISCOVER_BROADCAST_IP})")
    parser.add_argument("--discover-port", type=int, default=DEFAULT_DISCOVER_PORT, help=f"UDP port Arduino listens on for discovery polls (default: {DEFAULT_DISCOVER_PORT})")
    parser.add_argument("--discover-timeout", type=float, default=DEFAULT_DISCOVER_TIMEOUT, help=f"Seconds to wait for discovery replies (default: {DEFAULT_DISCOVER_TIMEOUT})")
    parser.add_argument("--discover-attempts", type=int, default=15, help="How many poll broadcasts to send (default: 15)")
    parser.add_argument("--discover-interval", type=float, default=0.6, help="Seconds between poll broadcasts (default: 0.6)")
    parser.add_argument("--discover-csv", default="", help="Optional CSV path to write discovered device table")
    parser.add_argument(
        "--network-map",
        default=DEFAULT_NETWORK_MAP,
        help="Optional JSON config for AP mapping (supports keys: device_to_ap, ap_limits, default_ap)",
    )
    parser.add_argument(
        "--runtime-ap-map",
        default=DEFAULT_RUNTIME_AP_MAP,
        help="Optional JSON runtime AP overrides (same schema as --network-map, checked first)",
    )
    parser.add_argument(
        "--default-ap-id",
        default=DEFAULT_AP_ID,
        help=f"Fallback AP bucket when no mapping exists (default: {DEFAULT_AP_ID})",
    )
    parser.add_argument(
        "--default-ap-limit",
        type=int,
        default=DEFAULT_AP_LIMIT,
        help=f"Per-AP transfer cap for fallback AP bucket (default: {DEFAULT_AP_LIMIT})",
    )
    parser.add_argument(
        "--max-concurrent-transfers",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_TRANSFERS,
        help=(
            "Global transfer cap across all AP buckets. "
            "0 means auto (sum of AP limits, minimum 1)."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path for network events/device state (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument("--db-log", action="store_true", dest="db_log", help="Enable SQLite logging (default: enabled)")
    parser.add_argument("--no-db-log", action="store_false", dest="db_log", help="Disable SQLite logging")
    parser.add_argument("--sync-time", action="store_true", help="Sync Arduino RTC from controller time before retrieval")
    parser.add_argument("--no-sync-time", action="store_false", dest="sync_time", help="Disable RTC sync command")
    parser.add_argument(
        "--sync-time-only",
        action="store_true",
        help="Discover devices and run RTC sync only (no data/file retrieval)",
    )
    parser.add_argument(
        "--time-offset-hours",
        type=float,
        default=DEFAULT_LOCAL_OFFSET,
        help=f"Hours offset applied to controller epoch before SET_TIME (default: {DEFAULT_LOCAL_OFFSET})",
    )
    parser.add_argument("--download-command", default="DOWNLOAD_DATA", help="UDP command sent to each Arduino to trigger data burst (default: DOWNLOAD_DATA)")
    parser.add_argument("--download-lines", type=int, default=4, help="How many CSV payload lines to capture per Arduino (default: 4)")
    parser.add_argument("--download-timeout", type=float, default=120.0, help="Seconds to wait per Arduino when capturing payload lines (default: 120.0)")
    parser.add_argument("--post-poll-wait", type=float, default=10.0, help="Seconds to wait after polling before starting downloads (default: 10.0)")
    parser.add_argument("--download-dir", default="data", help="Directory for per-device downloaded CSV files (default: data)")
    parser.add_argument("--transfer-latest-file", action="store_true", help="After discovery, list remote files and transfer the latest unsaved file")
    parser.add_argument("--no-transfer-latest-file", action="store_false", dest="transfer_latest_file", help="Disable latest-file transfer after discovery")
    parser.add_argument(
        "--prefer-file-prefix",
        choices=["TR", "DL", "ANY"],
        default=DEFAULT_PREFER_FILE_PREFIX,
        help=f"Preferred remote file prefix when selecting latest file (default: {DEFAULT_PREFER_FILE_PREFIX})",
    )
    parser.add_argument(
        "--tr-only",
        action="store_true",
        default=True,
        help="When prefix preference is TR, do not fall back to DL files (default: enabled)",
    )
    parser.add_argument(
        "--no-tr-only",
        action="store_false",
        dest="tr_only",
        help="Allow fallback to DL when no eligible TR file exists",
    )
    parser.add_argument(
        "--transfer-latest-even-if-seen",
        action="store_true",
        help="Download the most recent remote file even if it was already received before",
    )
    parser.add_argument(
        "--skip-if-uploaded-today",
        action="store_true",
        help=(
            "Skip transfer work for devices that already have a successful upload today "
            "for target day files (TR/RF + YYMMDD + .TXT)."
        ),
    )
    parser.add_argument(
        "--no-skip-if-uploaded-today",
        action="store_false",
        dest="skip_if_uploaded_today",
        help="Disable same-day transfer skip filter.",
    )
    parser.add_argument(
        "--file-day",
        choices=["yesterday", "today", "latest"],
        default=DEFAULT_FILE_DAY,
        help=f"Which remote file day to target during transfer (default: {DEFAULT_FILE_DAY})",
    )
    parser.add_argument("--file-list-timeout", type=float, default=10.0, help="Seconds to wait for LIST_FILES response per device (default: 10.0)")
    parser.add_argument("--file-output-dir", default="data/files", help="Directory for transferred files (default: data/files)")
    parser.add_argument("--file-log-dir", default="data/file_logs", help="Directory for per-device file transfer logs (default: data/file_logs)")
    parser.add_argument(
        "--transfer-tolerant",
        action="store_true",
        help="Allow partial file saves when EOF integrity checks fail (default: strict off)",
    )
    parser.add_argument(
        "--mark-partial-received",
        action="store_true",
        help="Treat partial transfers as received so they are not retried (only used with --transfer-tolerant)",
    )
    parser.add_argument("--scheduled", action="store_true", help="Run discover/transfer in a repeating time-window loop (opt-in)")
    parser.add_argument("--no-scheduled", action="store_false", dest="scheduled", help="Disable scheduled mode")
    parser.add_argument("--start-hour", type=int, default=DEFAULT_START_HOUR, help=f"Scheduled mode start hour, 0-23 local time (default: {DEFAULT_START_HOUR})")
    parser.add_argument("--end-hour", type=int, default=DEFAULT_END_HOUR, help=f"Scheduled mode end hour, 0-23 local time (default: {DEFAULT_END_HOUR})")
    parser.add_argument("--cycle-interval-sec", type=float, default=180.0, help="Seconds between cycles while inside schedule window (default: 180)")
    parser.add_argument("--out-window-sleep-sec", type=float, default=45.0, help="Seconds to sleep between time checks outside schedule window (default: 45)")
    parser.add_argument("--cloud-enabled", action="store_true", dest="cloud_enabled", help="Enable cloud upload window processing")
    parser.add_argument("--no-cloud-enabled", action="store_false", dest="cloud_enabled", help="Disable cloud upload window processing")
    parser.add_argument("--cloud-start", default=DEFAULT_CLOUD_START, help=f"Cloud upload window start in HHMM local time (default: {DEFAULT_CLOUD_START})")
    parser.add_argument("--cloud-end", default=DEFAULT_CLOUD_END, help=f"Cloud upload window end in HHMM local time (default: {DEFAULT_CLOUD_END})")
    parser.add_argument("--cloud-cycle-interval-sec", type=float, default=300.0, help="Seconds between cloud upload cycles in cloud window (default: 300)")
    parser.add_argument("--cloud-source-dir", default="data/files", help="Directory containing downloaded files to upload (default: data/files)")
    parser.add_argument("--cloud-sent-log", default="data/cloud_sent_files.csv", help="CSV ledger of files already sent to cloud (default: data/cloud_sent_files.csv)")
    parser.add_argument("--cloud-rclone-remote", default="", help="rclone remote name (required for actual upload), e.g. gdrive:")
    parser.add_argument("--cloud-rclone-base", default="BSM_Uploads", help="Remote base path/folder under rclone remote (default: BSM_Uploads)")
    parser.add_argument(
        "--cloud-local-dir",
        default="/Users/bobmauck/Library/CloudStorage/GoogleDrive-mauckr@kenyon.edu/.shortcut-targets-by-id/1paNSXGkj41CwPOn-VE1BFRt7k3oTzm51/PETREL NSF GRANT/2025 DATA and ANALYSIS/Bob Automation Information",
        help="Local destination directory for cloud sync clients (alternative to rclone remote)",
    )
    parser.add_argument("--cloud-once", action="store_true", help="Run one immediate cloud upload cycle and exit")
    return parser.parse_args(argv)
