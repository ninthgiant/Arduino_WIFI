#!/usr/bin/env python3
"""North End WIFI web control UI.

This module provides dashboard, file-transfer, and maintenance pages for
monitoring discovered Arduinos and running network/maintenance actions.
"""

from __future__ import annotations

import html
import csv
import json
import mimetypes
import sqlite3
import socket
import re
import subprocess
import sys
import threading
import datetime as dt
import time
import tempfile
import zipfile
from tempfile import TemporaryDirectory
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bsm_network.config import (
    ACTIVE_NETWORK_PROFILE,
    ACTIVE_NETWORK_PROFILE_SOURCE,
    DEFAULT_BIND_IP,
    DEFAULT_DB_PATH,
    DEFAULT_DISCOVER_CSV,
    DEFAULT_DISCOVER_PORT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    build_force_upload_argv,
    build_normal_ops_argv,
    build_poll_now_argv,
    parse_args,
)
from bsm_network.db import (
    clear_transfer_active,
    get_db_schema_info,
    get_daily_ops_state,
    init_db,
    is_daily_ops_complete,
    is_transfer_active,
    list_active_transfers,
    log_web_event,
    log_transfer_event,
    read_devices_snapshot,
    self_test_db_writes,
    set_transfer_active,
    set_burrow_id_by_short_uid,
    set_burrow_id_by_unique_id,
)
from bsm_network.discovery import run_discovery
from bsm_network.protocol import (
    clear_device_errors as protocol_clear_device_errors,
    enter_data_mode as protocol_enter_data_mode,
    get_device_config as protocol_get_device_config,
    get_device_diagnostics as protocol_get_device_diagnostics,
    get_device_status as protocol_get_device_status,
    get_last_data as protocol_get_last_data,
    ping_device as protocol_ping_device,
    reboot_device as protocol_reboot_device,
    set_device_config as protocol_set_device_config,
    set_wifi_policy as protocol_set_wifi_policy,
    transfer_file_protocol,
)
from bsm_network.records import build_local_filename, ensure_unique_filename
from bsm_network.wifi_policy import (
    DEFAULT_WIFI_POLICY_PATH,
    POLICY_MORNING_ONLY,
    POLICY_STAY_ACTIVE,
    build_wifi_policy_command,
    load_wifi_policy,
    normalize_wifi_policy,
    save_wifi_policy,
)
from bsm_web_ui.components import (
    PageContext,
    _render_action_form,
    _render_controls_row,
    _render_known_arduinos_selector,
    _render_scrollbox,
    _render_section_title,
    _render_shared_page,
    _render_titled_scroll_panel,
)

DISCOVER_CONTROL_PORT = DEFAULT_DISCOVER_PORT
WEB_SET_TIME_OFFSET_HOURS = -4.0
TZ_PRESET_OFFSETS: dict[str, float] = {
    "ast": -4.0,
    "adt": -3.0,
    "est": -5.0,
    "edt": -4.0,
}
UI_STATE_LOCK = threading.Lock()
LAST_SET_TIME_OFFSET_HOURS = WEB_SET_TIME_OFFSET_HOURS
LAST_SET_TIME_PRESET = "ast"
WEB_APP_NAME = "NORTH_END_IOT"
WEB_APP_VERSION = "4.5"
WEB_APP_HEADER = f"{WEB_APP_NAME} (version {WEB_APP_VERSION})"
UI_POLL_UPLOADS_MS = 5000
UI_POLL_DEVICES_MS = 5000
UI_POLL_ACTIVITY_MS = 5000
UI_POLL_PYTHON_LOG_MS = 5000
UI_POLL_UPLOAD_PROGRESS_MS = 500
UI_POLL_HEALTH_MS = 5000
ENDPOINT_CACHE_TTL_S = 1.5
CMD_RETRY_ATTEMPTS = 3
CMD_RETRY_BACKOFF_S = 0.25
WEB_POLL_NOW_DISCOVER_TIMEOUT_S = "8"
WEB_POLL_NOW_DISCOVER_ATTEMPTS = "4"
WEB_POLL_NOW_DISCOVER_INTERVAL_S = "0.25"
WEB_POLL_NOW_DOWNLOAD_LINES = "0"
WEB_POLL_NOW_DOWNLOAD_TIMEOUT_S = "0"
ENDPOINT_CACHE_LOCK = threading.Lock()
ENDPOINT_CACHE: dict[str, tuple[float, str]] = {}
DEVICE_RTC_CACHE_LOCK = threading.Lock()
DEVICE_RTC_CACHE: dict[str, tuple[float, str, str]] = {}
DEVICE_RTC_CACHE_TTL_S = 20.0


def new_correlation_id(prefix: str = "CMD") -> str:
    """Create correlation id."""
    return f"{prefix}{int(time.time() * 1000)}{(time.time_ns() & 0xFFF):03X}"


def categorize_command_error(detail: str) -> str:
    """Classify command error text into a retry/diagnostic category."""
    txt = (detail or "").strip().lower()
    if not txt:
        return "unknown"
    if "active_file" in txt or "busy" in txt:
        return "busy"
    if "timeout" in txt:
        return "timeout"
    if "unsupported" in txt or "not supported" in txt:
        return "unsupported"
    if "missing" in txt or "no ip" in txt or "invalid" in txt:
        return "invalid_input"
    if "unexpected source" in txt:
        return "unexpected_source"
    if "error" in txt:
        return "device_error"
    return "unknown"


def is_success_message(msg: str) -> bool:
    """Return whether success message."""
    return " OK " in f" {msg} "


def should_retry_message(msg: str) -> bool:
    """Return whether a command result message should be retried."""
    category = categorize_command_error(msg)
    return category in {"timeout", "unexpected_source"}


def get_last_set_time_state() -> tuple[float, str]:
    """Get last set time state."""
    with UI_STATE_LOCK:
        return LAST_SET_TIME_OFFSET_HOURS, LAST_SET_TIME_PRESET


def set_last_set_time_state(offset_hours: float, preset: str) -> None:
    """Set last set time state."""
    global LAST_SET_TIME_OFFSET_HOURS, LAST_SET_TIME_PRESET
    with UI_STATE_LOCK:
        LAST_SET_TIME_OFFSET_HOURS = offset_hours
        LAST_SET_TIME_PRESET = preset


NORMAL_OPS_CMD = [
    sys.executable,
    "-u",
    "bsm_network.py",
    *build_normal_ops_argv(DEFAULT_DISCOVER_CSV),
]


def _normal_ops_discovery_mode() -> str:
    """Return configured discovery mode label for Normal Ops command."""
    cmd = [str(x) for x in NORMAL_OPS_CMD]
    if "--ready-driven" in cmd and "--no-ready-driven" not in cmd:
        return "READY-driven"
    return "Poll-driven"

POLL_NOW_ARGS = build_poll_now_argv(DEFAULT_DISCOVER_CSV)


def _set_or_append_flag(args_list: list[str], flag: str, value: str) -> list[str]:
    """Set or append flag."""
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(args_list):
        tok = args_list[i]
        if tok == flag:
            if not replaced:
                out.extend([flag, value])
                replaced = True
            i += 2
            continue
        out.append(tok)
        i += 1
    if not replaced:
        out.extend([flag, value])
    return out


def _infer_bind_ip_for_prefix(prefix3: str) -> str:
    """Pick a local interface IP that matches the discovered /24 prefix."""
    try:
        out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return "0.0.0.0"
    inet_re = re.compile(r"\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+")
    for line in out.splitlines():
        m = inet_re.match(line)
        if not m:
            continue
        ip = m.group(1)
        if ip.startswith("127."):
            continue
        if ip.startswith(prefix3 + "."):
            return ip
    return "0.0.0.0"


def build_dynamic_poll_now_args() -> tuple[list[str], str]:
    """Build dynamic poll now args."""
    args_list = build_poll_now_argv(DEFAULT_DISCOVER_CSV)
    args_list = _set_or_append_flag(args_list, "--discover-timeout", WEB_POLL_NOW_DISCOVER_TIMEOUT_S)
    args_list = _set_or_append_flag(args_list, "--discover-attempts", WEB_POLL_NOW_DISCOVER_ATTEMPTS)
    args_list = _set_or_append_flag(args_list, "--discover-interval", WEB_POLL_NOW_DISCOVER_INTERVAL_S)
    args_list = _set_or_append_flag(args_list, "--download-lines", WEB_POLL_NOW_DOWNLOAD_LINES)
    args_list = _set_or_append_flag(args_list, "--download-timeout", WEB_POLL_NOW_DOWNLOAD_TIMEOUT_S)

    rows = read_devices_rows(Path("data/discovered_devices.csv"))
    prefixes: dict[str, int] = {}
    for row in rows:
        ip = (row.get("device_ip", "") or row.get("recv_ip", "")).strip()
        parts = ip.split(".")
        if len(parts) != 4:
            continue
        if not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            continue
        prefix3 = ".".join(parts[:3])
        prefixes[prefix3] = prefixes.get(prefix3, 0) + 1

    if not prefixes:
        return (
            args_list,
            "poll-now auto network: no known device subnet; using profile defaults "
            f"(fast mode: timeout={WEB_POLL_NOW_DISCOVER_TIMEOUT_S}s attempts={WEB_POLL_NOW_DISCOVER_ATTEMPTS} "
            f"interval={WEB_POLL_NOW_DISCOVER_INTERVAL_S}s download_lines={WEB_POLL_NOW_DOWNLOAD_LINES})",
        )

    chosen_prefix = sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    discover_ip = f"{chosen_prefix}.255"
    bind_ip = _infer_bind_ip_for_prefix(chosen_prefix)
    args_list = _set_or_append_flag(args_list, "--discover-ip", discover_ip)
    args_list = _set_or_append_flag(args_list, "--bind", bind_ip)
    note = (
        f"poll-now auto network: subnet={chosen_prefix}.0/24 discover-ip={discover_ip} bind={bind_ip} "
        f"(fast mode: timeout={WEB_POLL_NOW_DISCOVER_TIMEOUT_S}s attempts={WEB_POLL_NOW_DISCOVER_ATTEMPTS} "
        f"interval={WEB_POLL_NOW_DISCOVER_INTERVAL_S}s download_lines={WEB_POLL_NOW_DOWNLOAD_LINES})"
    )
    return args_list, note


class ProcessManager:
    """Class ProcessManager container."""
    def __init__(self) -> None:
        """Initialize manager state and runtime paths."""
        self._lock = threading.Lock()
        self._log_path = Path("data/web_normal_ops.log")
        self._force_thread: threading.Thread | None = None
        self._force_task_id: int = 0
        self._force_active_id: int | None = None
        self._force_log_path = Path("data/web_force_upload.log")
        self._poll_thread: threading.Thread | None = None
        self._poll_task_id: int = 0
        self._poll_active_id: int | None = None
        self._poll_log_path = Path("data/web_poll_now.log")

    def status(self) -> tuple[bool, int | None]:
        """Return whether the sibling bsm_network process appears to be running."""
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", r"python.*bsm_network\.py|bsm_network\.py"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return False, None
        for line in out.splitlines():
            token = line.strip()
            if not token:
                continue
            try:
                return True, int(token)
            except ValueError:
                continue
        return False, None

    def force_status(self) -> tuple[bool, int | None]:
        """Return whether a manual force-upload task is running."""
        with self._lock:
            if self._force_thread is None:
                return False, None
            if not self._force_thread.is_alive():
                self._force_thread = None
                self._force_active_id = None
                return False, None
            return True, self._force_active_id

    def start(self) -> str:
        """Report sibling-service ownership for Normal Ops."""
        running, pid = self.status()
        if running:
            return f"Normal Ops already running as sibling bsm_network process (PID {pid})."
        return "Normal Ops is managed outside bsm_web. Start bsm_network.service from systemd or run bsm_network.py directly."

    def stop(self) -> str:
        """Report sibling-service ownership for Normal Ops stop requests."""
        running, pid = self.status()
        if running:
            return f"Normal Ops is managed outside bsm_web (PID {pid}). Stop bsm_network.service from systemd if needed."
        return "Normal Ops is not running."

    def start_force_upload(self, uid: str, device_ip: str) -> str:
        """Start a background force-upload task for one Arduino."""
        with self._lock:
            if self._force_thread is not None and self._force_thread.is_alive():
                active_id = self._force_active_id if self._force_active_id is not None else 0
                return f"Force upload already running (task {active_id})."
            self._force_task_id += 1
            task_id = self._force_task_id
            self._force_active_id = task_id
            args_list = build_force_upload_argv(device_ip=device_ip, discover_csv=DEFAULT_DISCOVER_CSV)

        def _run_force_upload() -> None:
            """Execute force-upload discovery/transfer in a worker thread."""
            self._force_log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            with self._force_log_path.open("a", encoding="utf-8") as logf:
                logf.write(f"\n=== Force upload start {stamp} task={task_id} uid={uid} ip={device_ip} ===\n")
                logf.flush()
                rc = 1
                try:
                    args = parse_args(args_list)
                    with redirect_stdout(logf), redirect_stderr(logf):
                        rc = run_discovery(args)
                except Exception as exc:  # noqa: BLE001
                    logf.write(f"Force upload failed: {exc}\n")
                end_stamp = dt.datetime.now().isoformat(timespec="seconds")
                logf.write(f"=== Force upload end {end_stamp} task={task_id} rc={rc} ===\n")
            with self._lock:
                if self._force_active_id == task_id:
                    self._force_active_id = None

        thread = threading.Thread(target=_run_force_upload, name=f"force-upload-{task_id}", daemon=True)
        with self._lock:
            self._force_thread = thread
        thread.start()
        return f"Force upload started for {uid} ({device_ip}) (task {task_id})."

    def poll_now(self) -> str:
        """Start a guarded, discovery-only poll of Arduino clients."""
        with self._lock:
            if self._force_thread is not None and self._force_thread.is_alive():
                return "Wait for force upload to finish before manual poll (port/bind conflict)."
            if self._poll_thread is not None and self._poll_thread.is_alive():
                active_id = self._poll_active_id if self._poll_active_id is not None else 0
                return f"Poll Now already running (task {active_id})."

        try:
            active_rows = list_active_transfers(Path(DEFAULT_DB_PATH))
        except Exception as exc:  # noqa: BLE001
            return f"Poll Now skipped: active-transfer check failed ({exc})."
        if active_rows:
            labels = []
            for row in active_rows[:3]:
                short_uid = str(row.get("short_uid", "") or "").strip()
                uid = str(row.get("unique_id", "") or "").strip()
                source = str(row.get("source_filename", "") or "").strip()
                labels.append(short_uid or uid or source or "unknown")
            more = "" if len(active_rows) <= 3 else f", +{len(active_rows) - 3} more"
            return f"Poll Now skipped: {len(active_rows)} file transfer(s) active ({', '.join(labels)}{more})."

        args_list, note = build_dynamic_poll_now_args()
        with self._lock:
            self._poll_task_id += 1
            task_id = self._poll_task_id
            self._poll_active_id = task_id

        def _run_poll_now() -> None:
            """Execute discovery-only poll in a worker thread."""
            self._poll_log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            with self._poll_log_path.open("a", encoding="utf-8") as logf:
                logf.write(f"\n=== Poll Now start {stamp} task={task_id} ===\n")
                logf.write(f"{note}\n")
                logf.flush()
                rc = 1
                try:
                    args = parse_args(args_list)
                    with redirect_stdout(logf), redirect_stderr(logf):
                        rc = run_discovery(args)
                    invalidate_endpoint_cache()
                except Exception as exc:  # noqa: BLE001
                    logf.write(f"Poll Now failed: {exc}\n")
                end_stamp = dt.datetime.now().isoformat(timespec="seconds")
                logf.write(f"=== Poll Now end {end_stamp} task={task_id} rc={rc} ===\n")
            with self._lock:
                if self._poll_active_id == task_id:
                    self._poll_active_id = None

        thread = threading.Thread(target=_run_poll_now, name=f"poll-now-{task_id}", daemon=True)
        with self._lock:
            self._poll_thread = thread
        thread.start()
        return f"Poll Now started (task {task_id}). Discovery only; no transfers or time sync. Refresh in a few seconds."

    def shutdown(self) -> None:
        """Stop managed background processes before server shutdown."""
        return


MANAGER = ProcessManager()
ACTION_LOG_PATH = Path("data/web_actions.log")
WEB_FILE_OUTPUT_ROOT = Path("data/files")
WEB_FILE_LOG_ROOT = Path("data/file_logs")
UPLOAD_PROGRESS_LOCK = threading.Lock()
UPLOAD_PROGRESS: dict[str, dict[str, str | int | bool | float]] = {}


def set_upload_progress(op_id: str, pct: int, message: str, done: bool = False, error: bool = False) -> None:
    """Set upload progress."""
    token = (op_id or "").strip()
    if not token:
        return
    now = time.time()
    with UPLOAD_PROGRESS_LOCK:
        UPLOAD_PROGRESS[token] = {
            "pct": max(0, min(100, int(pct))),
            "message": str(message),
            "done": bool(done),
            "error": bool(error),
            "updated_at": now,
        }
        # Best-effort cleanup for stale entries.
        stale_before = now - 1800.0
        stale_keys = [k for k, v in UPLOAD_PROGRESS.items() if float(v.get("updated_at", 0.0) or 0.0) < stale_before]
        for k in stale_keys:
            UPLOAD_PROGRESS.pop(k, None)


def get_upload_progress(op_id: str) -> dict[str, str | int | bool]:
    """Get upload progress."""
    token = (op_id or "").strip()
    if not token:
        return {"ok": False, "pct": 0, "message": "missing op id", "done": False, "error": True}
    with UPLOAD_PROGRESS_LOCK:
        entry = UPLOAD_PROGRESS.get(token)
    if not entry:
        # Allow client polling to continue while upload handler initializes.
        return {"ok": False, "pct": 0, "message": "upload operation pending", "done": False, "error": False}
    return {
        "ok": True,
        "pct": int(entry.get("pct", 0) or 0),
        "message": str(entry.get("message", "")),
        "done": bool(entry.get("done", False)),
        "error": bool(entry.get("error", False)),
    }


def read_log_tail(path: Path, max_bytes: int = 120_000) -> str:
    """Read log tail."""
    if not path.exists():
        return ""
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def invalidate_endpoint_cache(keys: list[str] | None = None) -> None:
    """Invalidate endpoint cache."""
    with ENDPOINT_CACHE_LOCK:
        if keys is None:
            ENDPOINT_CACHE.clear()
            return
        for key in keys:
            ENDPOINT_CACHE.pop(str(key), None)


def get_cached_text(key: str, ttl_s: float, producer) -> str:
    """Get cached text."""
    now = time.monotonic()
    cache_key = str(key)
    with ENDPOINT_CACHE_LOCK:
        cached = ENDPOINT_CACHE.get(cache_key)
        if cached is not None:
            ts, body = cached
            if (now - ts) <= max(0.0, float(ttl_s)):
                return body
    body = str(producer())
    with ENDPOINT_CACHE_LOCK:
        ENDPOINT_CACHE[cache_key] = (time.monotonic(), body)
    return body


def append_action_log(action: str, message: str) -> None:
    """Append action log."""
    ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with ACTION_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {action}: {message}\n")
    try:
        log_web_event(
            db_path=Path(DEFAULT_DB_PATH).expanduser(),
            action=action,
            message=message,
            severity="error" if "failed" in message.lower() or "error" in message.lower() else "info",
        )
    except Exception:
        pass
    invalidate_endpoint_cache(["activity", "python-log"])


def read_activity_status() -> str:
    """Read activity status."""
    action_txt = read_log_tail(ACTION_LOG_PATH, max_bytes=80_000)
    normal_txt = read_log_tail(MANAGER._log_path, max_bytes=80_000)

    sections = []
    sections.append("=== WEB ACTIONS ===")
    sections.append(action_txt.strip() or "(No web actions yet)")
    sections.append("")
    sections.append("=== NORMAL OPS OUTPUT ===")
    sections.append(normal_txt.strip() or "(No normal-ops output yet)")
    return "\n".join(sections)


def read_python_log_status() -> str:
    """Read python log status."""
    txt = read_log_tail(ACTION_LOG_PATH, max_bytes=120_000)
    return txt.strip() or "(No python web actions logged yet)"


def _latest_discovery_timestamp(db_path: Path) -> str:
    """Read the most recent successful discovery timestamp from SQLite."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT COALESCE(MAX(event_ts), '')
                FROM discovery_events
                WHERE status = 'discovered'
                """
            )
            row = cur.fetchone()
            return str((row[0] if row else "") or "")
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return ""


def _latest_transfer_skip_summary(db_path: Path) -> str:
    """Build one-line summary for latest run's completed-upload skip filter."""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT run_id
                FROM transfer_events
                WHERE run_id LIKE 'DISC_%'
                ORDER BY event_ts DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            run_id = str((row[0] if row else "") or "").strip()
            if not run_id:
                return ""

            cur = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status = 'skip' AND message LIKE '%already uploaded today%' THEN 1 ELSE 0 END) AS skip_done,
                  SUM(CASE WHEN NOT (status = 'skip' AND message LIKE '%already uploaded today%') THEN 1 ELSE 0 END) AS eligible
                FROM transfer_events
                WHERE run_id = ?
                """,
                (run_id,),
            )
            sums = cur.fetchone()
            skip_done = int((sums[0] if sums and sums[0] is not None else 0) or 0)
            eligible = int((sums[1] if sums and sums[1] is not None else 0) or 0)
            if skip_done <= 0:
                return ""
            return (
                f"Transfer skip filter: {skip_done} device(s) already uploaded today "
                f"(TR/RF target date), {eligible} device(s) still eligible."
            )
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return ""


def get_health_payload() -> dict[str, object]:
    """Get health payload."""
    db_path = Path(DEFAULT_DB_PATH).expanduser()
    running, pid = MANAGER.status()
    payload: dict[str, object] = {
        "ok": True,
        "web_alive": True,
        "normal_ops_running": bool(running),
        "normal_ops_pid": int(pid) if pid is not None else None,
        "db_path": str(db_path),
        "db_exists": bool(db_path.exists()),
        "db_writable": False,
        "active_transfer_count": 0,
        "last_discovery_ts": "",
        "last_discovery_age_s": None,
        "device_ip_recv_ip_mismatch_count": 0,
        "transfer_skip_summary": "",
        "discovery_mode": _normal_ops_discovery_mode(),
        "status": "ok",
    }

    try:
        ok, _err = self_test_db_writes(db_path)
        payload["db_writable"] = bool(ok)
    except Exception:
        payload["db_writable"] = False

    try:
        payload["active_transfer_count"] = len(list_active_transfers(db_path))
    except Exception:
        payload["active_transfer_count"] = 0

    last_ts = _latest_discovery_timestamp(db_path)
    payload["last_discovery_ts"] = last_ts
    if last_ts:
        dt_val = _iso_to_dt(last_ts)
        if dt_val is not None:
            payload["last_discovery_age_s"] = max(0.0, (dt.datetime.now() - dt_val).total_seconds())

    mismatch_count = 0
    try:
        for row in read_devices_rows(Path("data/discovered_devices.csv")):
            dev_ip = (row.get("device_ip", "") or "").strip()
            recv_ip = (row.get("recv_ip", "") or "").strip()
            if dev_ip and recv_ip and dev_ip != recv_ip:
                mismatch_count += 1
    except Exception:
        mismatch_count = 0
    payload["device_ip_recv_ip_mismatch_count"] = mismatch_count
    payload["transfer_skip_summary"] = _latest_transfer_skip_summary(db_path)

    if not payload["db_writable"]:
        payload["ok"] = False
        payload["status"] = "degraded"
    elif mismatch_count > 0:
        payload["status"] = "warning"
    else:
        payload["status"] = "ok"
    return payload


def format_health_status_text(payload: dict[str, object]) -> str:
    """Format health status text."""
    status = str(payload.get("status", "unknown")).upper()
    running = "YES" if bool(payload.get("normal_ops_running", False)) else "NO"
    pid = payload.get("normal_ops_pid")
    pid_text = str(pid) if pid is not None else "-"
    db_exists = "YES" if bool(payload.get("db_exists", False)) else "NO"
    db_writable = "YES" if bool(payload.get("db_writable", False)) else "NO"
    active = int(payload.get("active_transfer_count", 0) or 0)
    mismatch = int(payload.get("device_ip_recv_ip_mismatch_count", 0) or 0)
    discovery_mode = str(payload.get("discovery_mode", "") or "")
    last_ts = str(payload.get("last_discovery_ts", "") or "")
    age = payload.get("last_discovery_age_s")
    age_min_text = f"{(float(age) / 60.0):.1f}" if isinstance(age, (int, float)) else ""
    col_defs = [
        ("status", 8),
        ("normal_ops", 10),
        ("pid", 7),
        ("db_exists", 9),
        ("db_writable", 11),
        ("active_xfers", 12),
        ("ip_mismatch", 11),
        ("discovery_mode", 14),
        ("last_discovery", 19),
        ("age_min", 7),
    ]
    data_vals = [
        status,
        running,
        pid_text,
        db_exists,
        db_writable,
        str(active),
        str(mismatch),
        (discovery_mode or "-"),
        (last_ts or "-"),
        (age_min_text or "-"),
    ]
    header_line = " ".join(f"{name:<{width}}" for name, width in col_defs)
    sep_line = " ".join("-" * width for _name, width in col_defs)
    data_line = " ".join(f"{val:<{col_defs[idx][1]}}" for idx, val in enumerate(data_vals))
    summary = str(payload.get("transfer_skip_summary", "") or "").strip()
    if summary:
        return f"{header_line}\n{sep_line}\n{data_line}\n{summary}"
    return f"{header_line}\n{sep_line}\n{data_line}"


def _iso_to_dt(value: str) -> dt.datetime | None:
    """Parse an ISO timestamp string into a datetime object."""
    try:
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def read_devices_status(path: Path, online_seconds: int = 600) -> str:
    """Read devices status."""
    rows = read_devices_rows(path, online_seconds=online_seconds)
    if not rows:
        return "(No devices discovered yet)"

    lines = []
    mismatches = []
    for row in rows:
        dev_ip = (row.get("device_ip", "") or "").strip()
        recv_ip = (row.get("recv_ip", "") or "").strip()
        if dev_ip and recv_ip and dev_ip != recv_ip:
            short_uid = (row.get("short_uid", "") or "").strip()
            mismatches.append(short_uid if short_uid else (row.get("unique_id", "") or "").strip())
    if mismatches:
        lines.append(f"WARNING: device_ip != recv_ip for {len(mismatches)} device(s): {', '.join(mismatches)}")
        lines.append("")
    lines.append("status   burrow_id      short_uid  fw_ver   ap_id       network_uid       device_ip      recv_ip        last_seen             unique_id")
    lines.append("------   ------------   --------   ------   ---------   ---------------   -----------   -----------    -------------------   ------------------------------------")
    for row in rows:
        status = row.get("status", "UNKNOWN")
        burrow_id = row.get("burrow_id", "")
        short_uid = row.get("short_uid", "")
        if str(row.get("short_uid_collision", "0")) in {"1", "true", "True"}:
            short_uid = f"{short_uid}*"
        uid = row.get("unique_id", "")
        ap_id = row.get("ap_id", "")
        net_uid = row.get("network_uid", "")
        fw_ver = row.get("firmware_version", "")
        dev_ip = row.get("device_ip", "")
        recv_ip = row.get("recv_ip", "")
        last_seen_raw = row.get("last_seen", "")
        lines.append(f"{status:<6}   {burrow_id:<12}   {short_uid:<8}   {fw_ver:<6}   {ap_id:<9}   {net_uid:<15}   {dev_ip:<11}   {recv_ip:<11}    {last_seen_raw:<19}   {uid:<36}")
    return "\n".join(lines)


def read_devices_rows(path: Path, online_seconds: int = 600) -> list[dict[str, str]]:
    """Read devices rows."""
    db_rows = read_devices_snapshot(Path(DEFAULT_DB_PATH))
    if db_rows:
        rows = db_rows
    else:
        if not path.exists():
            return []

        rows = []
        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_clean = {k: (v or "").strip() for k, v in row.items()}
                rows.append(row_clean)

    active_uids: set[str] = set()
    try:
        active_rows = list_active_transfers(Path(DEFAULT_DB_PATH))
        active_uids = {str(r.get("unique_id", "")).strip() for r in active_rows if str(r.get("unique_id", "")).strip()}
    except Exception:
        active_uids = set()

    now = dt.datetime.now()
    for row in rows:
        uid = (row.get("unique_id", "") or "").strip()
        status = "Stale"
        if uid and uid in active_uids:
            row["status"] = "Upload"
            continue
        last_seen = _iso_to_dt(row.get("last_seen", ""))
        if last_seen is not None:
            age_s = (now - last_seen).total_seconds()
            status = "Online" if age_s <= online_seconds else "Stale"
        row["status"] = status
    return rows


def query_device_time(device_ip: str, timeout_s: float = 2.0) -> str:
    """Query device time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(b"GET_TIME", (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            line = data.decode("utf-8", errors="replace").strip()
            if src_ip != device_ip:
                return f"RTC query got reply from unexpected source: {src_ip} ({line})"
            if line.startswith("TIME,"):
                parts = line.split(",", 2)
                epoch = parts[1] if len(parts) > 1 else "?"
                ts = parts[2] if len(parts) > 2 else "?"
                if attempt == 1 and first_line.startswith("ERR_TIME"):
                    return (
                        f"RTC query recovered for {device_ip}: initial error ({first_line}), "
                        f"then OK after reset/retry: {ts} (epoch={epoch})"
                    )
                return f"RTC query OK from {device_ip}: {ts} (epoch={epoch})"
            if attempt == 0:
                first_line = line
                continue
            return f"RTC query error from {device_ip}: {line} (after initial: {first_line})"
        return f"RTC query error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"RTC query timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"RTC query failed for {device_ip}: {exc}"
    finally:
        sock.close()


def set_device_time(device_ip: str, offset_hours: float, timeout_s: float = 2.0) -> str:
    """Set device time."""
    epoch = int(time.time() + (offset_hours * 3600.0))
    msg = f"SET_TIME,{epoch}".encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(msg, (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            line = data.decode("utf-8", errors="replace").strip()
            if src_ip != device_ip:
                return f"SET_TIME got reply from unexpected source: {src_ip} ({line})"
            if line.startswith("ACK_TIME,"):
                verify_msg = query_device_time(device_ip=device_ip, timeout_s=timeout_s)
                if attempt == 1 and first_line:
                    return (
                        f"SET_TIME recovered for {device_ip}: initial error ({first_line}), then ACK ({line}). "
                        f"Verify: {verify_msg}"
                    )
                return (
                    f"SET_TIME OK for {device_ip}: {line} "
                    f"(offset={offset_hours:+g}h). Verify: {verify_msg}"
                )
            if attempt == 0:
                first_line = line
                continue
            return f"SET_TIME error from {device_ip}: {line} (after initial: {first_line})"
        return f"SET_TIME error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"SET_TIME timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"SET_TIME failed for {device_ip}: {exc}"
    finally:
        sock.close()


def ping_device(device_ip: str, timeout_s: float = 2.0) -> str:
    """Send PING and return one-line status text."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        line = protocol_ping_device(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        return f"PING OK from {device_ip}: {line}"
    except TimeoutError:
        return f"PING timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"PING failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_status(device_ip: str, timeout_s: float = 2.0) -> str:
    """Query device status."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        status = protocol_get_device_status(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in status.items())
        return f"GET_STATUS OK from {device_ip}: STATUS,{payload}"
    except TimeoutError:
        return f"GET_STATUS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_STATUS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_config(device_ip: str, timeout_s: float = 2.0) -> str:
    """Query device config."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        config = protocol_get_device_config(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in config.items())
        return f"GET_CONFIG OK from {device_ip}: CONFIG,{payload}"
    except TimeoutError:
        return f"GET_CONFIG timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_CONFIG failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_diagnostics(device_ip: str, timeout_s: float = 2.0) -> str:
    """Query device diagnostics."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        diag = protocol_get_device_diagnostics(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in diag.items())
        return f"GET_DIAGNOSTICS OK from {device_ip}: DIAG,{payload}"
    except TimeoutError:
        return f"GET_DIAGNOSTICS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_DIAGNOSTICS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_last_data(device_ip: str, timeout_s: float = 2.0) -> str:
    """Query last data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        data_dict = protocol_get_last_data(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in data_dict.items())
        return f"GET_LAST_DATA OK from {device_ip}: LAST_DATA,{payload}"
    except TimeoutError:
        return f"GET_LAST_DATA timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_LAST_DATA failed for {device_ip}: {exc}"
    finally:
        sock.close()


def set_device_config(device_ip: str, config_updates: dict[str, str], timeout_s: float = 3.0) -> str:
    """Set device config."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_set_device_config(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            config_updates=config_updates,
            timeout_s=timeout_s,
        )
        if ok:
            return f"SET_CONFIG OK for {device_ip}"
        return f"SET_CONFIG failed/timeout from {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"SET_CONFIG failed for {device_ip}: {exc}"
    finally:
        sock.close()


def set_device_wifi_policy(device_ip: str, policy: dict[str, object], timeout_s: float = 3.0) -> str:
    """Send current Gateway WiFi policy to one Arduino."""
    clean = normalize_wifi_policy(policy)
    command = build_wifi_policy_command(clean)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_set_wifi_policy(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            command=command,
            timeout_s=timeout_s,
        )
        if ok:
            return f"SET_WIFI_POLICY OK for {device_ip}: {command}"
        return f"SET_WIFI_POLICY failed/timeout from {device_ip}: {command}"
    except Exception as exc:  # noqa: BLE001
        return f"SET_WIFI_POLICY failed for {device_ip}: {exc}"
    finally:
        sock.close()


def reboot_device(device_ip: str, timeout_s: float = 3.0) -> str:
    """Send REBOOT command and return status text."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_reboot_device(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if ok:
            return f"REBOOT OK for {device_ip}"
        return f"REBOOT failed/timeout for {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"REBOOT failed for {device_ip}: {exc}"
    finally:
        sock.close()


def enter_data_mode(device_ip: str, timeout_s: float = 3.0) -> str:
    """Send ENTER_DATA_MODE and return status text."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_enter_data_mode(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if ok:
            return f"ENTER_DATA_MODE OK for {device_ip}"
        return f"ENTER_DATA_MODE failed/timeout for {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"ENTER_DATA_MODE failed for {device_ip}: {exc}"
    finally:
        sock.close()


def can_enter_data_mode(uid: str) -> tuple[bool, str]:
    """Return whether enter data mode."""
    return can_web_contact_arduino(uid, "ENTER_DATA_MODE")


def can_web_contact_arduino(uid: str, action_label: str) -> tuple[bool, str]:
    """Return whether web UI may send a direct command to an Arduino."""
    token = (uid or "").strip()
    label = (action_label or "Web command").strip() or "Web command"
    if not token:
        return False, f"{label} blocked: missing Arduino UID."
    db_path = Path(DEFAULT_DB_PATH)
    try:
        if is_transfer_active(db_path, token):
            return False, f"{label} blocked for {token}: bsm_network transfer is active."
        if not is_daily_ops_complete(db_path, token):
            state = get_daily_ops_state(db_path, token)
            current = state.get("state", "none") if state else "none"
            updated = state.get("updated_at", "") if state else ""
            suffix = f" Last daily ops state={current}"
            if updated:
                suffix += f" updated_at={updated}"
            return False, (
                f"{label} blocked for {token}: daily Normal Ops are not complete for today."
                f"{suffix}."
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"{label} blocked for {token}: cannot verify Gateway state ({exc})."
    return True, ""


def can_web_access_file_transfers(uid: str, action_label: str) -> tuple[bool, str]:
    """Return whether web UI may access Arduino SD file-transfer commands."""
    token = (uid or "").strip()
    label = (action_label or "File transfer").strip() or "File transfer"
    if not token:
        return False, f"{label} blocked: missing Arduino UID."
    db_path = Path(DEFAULT_DB_PATH)
    try:
        if is_transfer_active(db_path, token):
            return False, f"{label} blocked for {token}: bsm_network transfer is active."
        state = get_daily_ops_state(db_path, token)
        current = state.get("state", "none") if state else "none"
        if current in {"completed_uploaded", "completed_no_data", "completed_unverified", "failed"}:
            return True, ""
        updated = state.get("updated_at", "") if state else ""
        suffix = f" Last daily ops state={current}"
        if updated:
            suffix += f" updated_at={updated}"
        return False, (
            f"{label} blocked for {token}: daily Normal Ops have not reached a terminal state today."
            f"{suffix}."
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{label} blocked for {token}: cannot verify Gateway state ({exc})."


def assign_burrow_id(short_uid: str, burrow_id: str) -> str:
    """Assign burrow id."""
    db_path = Path(DEFAULT_DB_PATH)
    ok, msg = set_burrow_id_by_short_uid(db_path=db_path, short_uid=short_uid, burrow_id=burrow_id)
    return msg if ok else f"Assign burrow_id failed: {msg}"


def assign_burrow_id_for_uid(unique_id: str, burrow_id: str) -> str:
    """Assign burrow id for uid."""
    db_path = Path(DEFAULT_DB_PATH)
    ok, msg = set_burrow_id_by_unique_id(db_path=db_path, unique_id=unique_id, burrow_id=burrow_id)
    return msg if ok else f"Assign burrow_id failed: {msg}"


def _current_burrow_for_uid(unique_id: str) -> str:
    """Return current burrow_id assignment for a unique device UID."""
    uid = (unique_id or "").strip()
    if not uid:
        return ""
    rows = read_devices_snapshot(Path(DEFAULT_DB_PATH))
    for row in rows:
        if (row.get("unique_id", "") or "").strip() == uid:
            return (row.get("burrow_id", "") or "").strip()
    return ""


def clear_device_errors(device_ip: str, timeout_s: float = 3.0) -> str:
    """Clear device errors."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_clear_device_errors(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if ok:
            return f"CLEAR_ERRORS OK for {device_ip}"
        return f"CLEAR_ERRORS failed/timeout for {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"CLEAR_ERRORS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def run_maintenance_action_with_retry(action: str, fn, timeout_s: float) -> str:
    """Run maintenance action with retry."""
    cid = new_correlation_id("MNT")
    last_msg = ""
    for attempt in range(1, CMD_RETRY_ATTEMPTS + 1):
        last_msg = str(fn())
        if is_success_message(last_msg):
            return f"{last_msg} [cid={cid} category=ok attempts={attempt}]"
        if attempt < CMD_RETRY_ATTEMPTS and should_retry_message(last_msg):
            time.sleep(CMD_RETRY_BACKOFF_S * attempt)
            continue
        break
    category = categorize_command_error(last_msg)
    return (
        f"{action} failed after retries: {last_msg} "
        f"[cid={cid} category={category} attempts={CMD_RETRY_ATTEMPTS} timeout_s={timeout_s:.1f}]"
    )


def read_today_uploads_status(db_path: Path) -> str:
    """Read today uploads status."""
    if not db_path.exists():
        return "(No upload DB yet)"

    today = dt.date.today().isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT
                  COALESCE(t.burrow_id, ''),
                  COALESCE(d.short_uid, ''),
                  COALESCE(t.network_uid, ''),
                  COALESCE(t.source_filename, ''),
                  COALESCE(t.saved_path, ''),
                  COALESCE(t.event_ts, ''),
                  COALESCE(t.status, ''),
                  COALESCE(t.message, ''),
                  CASE
                    WHEN t.duration_s IS NULL THEN ''
                    ELSE CAST(ROUND(t.duration_s / 60.0, 1) AS TEXT)
                  END
                FROM transfer_events t
                LEFT JOIN devices d
                  ON d.unique_id = t.unique_id
                WHERE date(substr(t.event_ts, 1, 10)) = ?
                ORDER BY t.event_ts DESC
                """,
                (today,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return f"(Upload list unavailable: {exc})"

    if not rows:
        return "(No files uploaded today)"

    def _rtc_sync_from_message(msg: str) -> str:
        return "FAILED" if "RTC sync failed (upload proceeded)" in (msg or "") else "OK"

    def _reason_label(status: str, msg: str) -> str:
        txt = (msg or "").lower()
        st = (status or "").lower()
        if "already uploaded today" in txt:
            return "already uploaded today"
        if "no new files" in txt:
            return "no new files"
        if st == "saved":
            return "transferred"
        if st == "skip":
            return "skipped"
        if st == "error":
            return "error"
        return st or "unknown"

    latest_by_device: dict[str, tuple[str, str, str, str, str]] = {}
    for burrow_id, short_uid, network_uid, filename, _saved_path, uploaded_at, status, message, _duration_s in rows:
        key = str(short_uid or network_uid or burrow_id or "").strip()
        if not key:
            key = str(network_uid or "").strip()
        if key and key not in latest_by_device:
            latest_by_device[key] = (
                str(burrow_id),
                str(short_uid),
                _rtc_sync_from_message(str(message)),
                _reason_label(str(status), str(message)),
                str(uploaded_at),
            )

    lines = []
    lines.append("Today Device Summary")
    lines.append("burrow_id      short_uid  rtc_sync  last_result             updated_at")
    lines.append("------------   --------   --------  ----------------------  -------------------")
    for _key, item in sorted(latest_by_device.items(), key=lambda kv: kv[1][4], reverse=True):
        burrow_id, short_uid, rtc_sync, result_label, updated_at = item
        lines.append(f"{burrow_id:<12}   {short_uid:<8}   {rtc_sync:<8}  {result_label:<22}  {updated_at:<19}")
    lines.append("")
    lines.append("Saved Uploads Today")
    lines.append("burrow_id      short_uid  network_uid       filename                file_size_mb  uploaded_at           duration_min")
    lines.append("------------   --------   ---------------   ----------------------  ------------  -------------------   ------------")
    for burrow_id, short_uid, network_uid, filename, saved_path, uploaded_at, status, message, duration_s in rows:
        if str(status).lower() != "saved":
            continue
        size_mb_str = ""
        try:
            p = Path(str(saved_path or "")).expanduser()
            if p.exists() and p.is_file():
                size_mb_str = f"{(p.stat().st_size / (1024.0 * 1024.0)):.3f}"
        except Exception:
            size_mb_str = ""
        rtc_sync = _rtc_sync_from_message(str(message))
        lines.append(
            f"{str(burrow_id):<12}   {str(short_uid):<8}   {str(network_uid):<15}   "
            f"{str(filename):<22}  {size_mb_str:<12}  {str(uploaded_at):<19}   {str(duration_s):<10}  rtc={rtc_sync}"
        )
    return "\n".join(lines)


def _request_remote_file_list_with_sizes(device_ip: str, timeout_s: float = 8.0) -> tuple[list[tuple[str, int]], str]:
    """Request remote SD file list and parse (filename, byte_size) tuples."""
    cid = new_correlation_id("LST")
    last_err = ""
    ip = (device_ip or "").strip()
    if not ip:
        return [], f"LIST_FILES failed: missing device IP [cid={cid} category=invalid_input]"

    for attempt in range(1, CMD_RETRY_ATTEMPTS + 1):
        transfer_id = f"LWEB{int(time.time() * 1000)}{attempt}"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.4)
        try:
            msg = f"LIST_FILES,{transfer_id}".encode("utf-8")
            sock.sendto(msg, (ip, DISCOVER_CONTROL_PORT))

            items: list[tuple[str, int]] = []
            seen: set[str] = set()
            got_end = False
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    data, (src_ip, _src_port) = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if src_ip != ip:
                    continue
                line = data.decode("utf-8", errors="replace").strip()
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2 or parts[1] != transfer_id:
                    continue
                msg_type = parts[0]
                if msg_type == "ERROR":
                    last_err = f"LIST_FILES Arduino error: {line}"
                    break
                if msg_type == "FILE_ITEM" and len(parts) >= 4:
                    name = parts[2]
                    try:
                        size = int(parts[3])
                    except ValueError:
                        size = 0
                    if name and name not in seen:
                        seen.add(name)
                        items.append((name, size))
                    continue
                if msg_type == "FILE_LIST_END":
                    got_end = True
                    break
            if got_end:
                return items, ""
            if not last_err:
                last_err = f"LIST_FILES timeout for {ip}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"LIST_FILES exception: {exc}"
        finally:
            sock.close()

        if attempt < CMD_RETRY_ATTEMPTS and should_retry_message(last_err):
            time.sleep(CMD_RETRY_BACKOFF_S * attempt)

    category = categorize_command_error(last_err)
    return [], f"{last_err} [cid={cid} category={category} attempts={CMD_RETRY_ATTEMPTS}]"


def delete_remote_file(device_ip: str, remote_filename: str, timeout_s: float = 8.0) -> tuple[bool, str]:
    """Delete remote file."""
    ip = (device_ip or "").strip()
    name = (remote_filename or "").strip()
    cid = new_correlation_id("DEL")
    if not ip or not name:
        return False, f"Missing device IP or filename. [cid={cid} category=invalid_input]"
    if "," in name:
        return False, f"Filename contains unsupported comma. [cid={cid} category=invalid_input]"

    last_err = ""
    for attempt in range(1, CMD_RETRY_ATTEMPTS + 1):
        transfer_id = f"DWEB{int(time.time() * 1000)}{attempt}"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.4)
        try:
            msg = f"DELETE_FILE,{transfer_id},{name}".encode("utf-8")
            sock.sendto(msg, (ip, DISCOVER_CONTROL_PORT))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    data, (src_ip, _src_port) = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if src_ip != ip:
                    continue
                line = data.decode("utf-8", errors="replace").strip()
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2 or parts[1] != transfer_id:
                    continue
                if parts[0] == "ACK_DELETE":
                    return True, f"Deleted SD file '{name}' on {ip}. [cid={cid} category=ok attempts={attempt}]"
                if parts[0] == "ERROR":
                    last_err = f"DELETE_FILE Arduino error: {line}"
                    break
            if not last_err:
                last_err = (
                    f"DELETE_FILE timeout for {ip} "
                    "(device may not be running firmware with DELETE_FILE support yet)"
                )
        except Exception as exc:  # noqa: BLE001
            last_err = f"DELETE_FILE exception: {exc}"
        finally:
            sock.close()

        if attempt < CMD_RETRY_ATTEMPTS and should_retry_message(last_err):
            time.sleep(CMD_RETRY_BACKOFF_S * attempt)

    category = categorize_command_error(last_err)
    return False, f"{last_err} [cid={cid} category={category} attempts={CMD_RETRY_ATTEMPTS}]"


def _read_uploaded_files_for_device(short_uid: str) -> tuple[list[tuple[str, str, str, float]], str]:
    """Read uploaded files for device."""
    sid = (short_uid or "").strip().upper()
    if not sid:
        return [], "(No short UID available)"

    folder = WEB_FILE_OUTPUT_ROOT / sid
    if not folder.exists():
        return [], "(No local upload folder yet)"
    if not folder.is_dir():
        return [], f"(Upload path is not a folder: {folder})"

    rows: list[tuple[str, str, str, float]] = []
    try:
        for p in folder.iterdir():
            if not p.is_file():
                continue
            st = p.stat()
            size_mb = float(st.st_size) / (1024.0 * 1024.0)
            # Use file modified time as uploaded-at for filesystem-first truth.
            ts = dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
            rows.append((p.name, ts, str(p), size_mb))
    except Exception as exc:  # noqa: BLE001
        return [], f"(Could not read upload folder: {exc})"

    rows.sort(key=lambda x: x[1], reverse=True)
    return rows, ""


def _build_uploaded_rows_html(rows: list[tuple[str, str, str, float]]) -> str:
    """Build uploaded rows html."""
    if not rows:
        return '<div style="font-style:italic;">(No Gateway-saved files found for this Arduino)</div>'
    out = []
    out.append('<div class="upload-head">filename                          size_mb   uploaded_at</div>')
    out.append('<div class="upload-sep">--------------------------------  -------   -------------------</div>')
    for name, ts, saved_path, size_mb in rows:
        line = f"{name:<32}  {size_mb:>7.3f}   {ts}"
        out.append(
            f'<div class="upload-row" data-name="{html.escape(name, quote=True)}" '
            f'data-ts="{html.escape(ts, quote=True)}" '
            f'data-path="{html.escape(saved_path, quote=True)}">{html.escape(line)}</div>'
        )
    return "".join(out)


def delete_local_uploaded_file(saved_path: str) -> tuple[bool, str]:
    """Delete local uploaded file."""
    raw = (saved_path or "").strip()
    if not raw:
        return False, "No saved_path provided."
    try:
        target = Path(raw).expanduser().resolve()
        data_root = Path("data").resolve()
    except Exception as exc:  # noqa: BLE001
        return False, f"Invalid path: {exc}"
    if data_root not in target.parents and target != data_root:
        return False, f"Refusing to delete outside data folder: {target}"
    if not target.exists():
        return False, f"File not found: {target}"
    if not target.is_file():
        return False, f"Not a file: {target}"
    try:
        target.unlink()
        return True, f"Deleted local file: {target}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Delete failed: {exc}"


def resolve_local_uploaded_file_for_download(saved_path: str, selected_uid: str = "") -> tuple[Path | None, str]:
    """Validate and resolve a Gateway-uploaded file path for browser download."""
    raw = (saved_path or "").strip()
    if not raw:
        return None, "No saved_path provided."
    try:
        target = Path(raw).expanduser().resolve()
        output_root = WEB_FILE_OUTPUT_ROOT.resolve()
    except Exception as exc:  # noqa: BLE001
        return None, f"Invalid path: {exc}"
    if output_root not in target.parents:
        return None, f"Refusing download outside upload folder: {target}"
    if selected_uid:
        _device, short_uid, _ip = _resolve_device_context_for_uid(selected_uid)
        if short_uid:
            expected_dir = (WEB_FILE_OUTPUT_ROOT / short_uid.upper()).resolve()
            if target.parent != expected_dir:
                return None, f"File does not belong to selected device folder ({short_uid.upper()})."
    if not target.exists():
        return None, f"File not found: {target}"
    if not target.is_file():
        return None, f"Not a file: {target}"
    return target, ""


def _parse_bundle_date(date_raw: str) -> tuple[dt.date | None, str]:
    """Parse YYYY-MM-DD date for bundle export; default to yesterday when blank."""
    txt = (date_raw or "").strip()
    if not txt:
        return dt.date.today() - dt.timedelta(days=1), ""
    try:
        return dt.datetime.strptime(txt, "%Y-%m-%d").date(), ""
    except ValueError:
        return None, f"Invalid date '{txt}'. Use YYYY-MM-DD."


def _file_matches_kind(name: str, kind: str) -> bool:
    """Return whether filename matches requested prefix kind."""
    u = (name or "").strip().upper()
    k = (kind or "TRRF").strip().upper()
    if k == "TR":
        return u.startswith("TR")
    if k == "RF":
        return u.startswith("RF")
    if k == "TRRF":
        return u.startswith("TR") or u.startswith("RF")
    if k == "DL":
        return u.startswith("DL")
    if k == "ALL":
        return u.startswith(("TR", "RF", "DL"))
    return False


def _file_matches_date(p: Path, target_date: dt.date) -> bool:
    """Match file date using filename token YYMMDD first; fallback to mtime date."""
    name_u = p.name.upper()
    m = re.search(r"(TR|RF|DL)(\d{6})", name_u)
    if m:
        yymmdd = m.group(2)
        try:
            file_date = dt.datetime.strptime(yymmdd, "%y%m%d").date()
            return file_date == target_date
        except ValueError:
            pass
    try:
        mtime_date = dt.date.fromtimestamp(p.stat().st_mtime)
        return mtime_date == target_date
    except Exception:
        return False


def _iter_bundle_source_files(selected_uid: str, kind: str, target_date: dt.date) -> tuple[list[tuple[Path, str]], str]:
    """Collect matching Gateway files for bundle download."""
    files: list[tuple[Path, str]] = []
    uid = (selected_uid or "").strip()
    if uid:
        _device, short_uid, _ip = _resolve_device_context_for_uid(uid)
        if not short_uid:
            return [], "No short UID available for selected Arduino."
        scope_dirs = [WEB_FILE_OUTPUT_ROOT / short_uid.upper()]
    else:
        scope_dirs = []
        if WEB_FILE_OUTPUT_ROOT.exists():
            for p in WEB_FILE_OUTPUT_ROOT.iterdir():
                if p.is_dir():
                    scope_dirs.append(p)
    if not scope_dirs:
        return [], "No Gateway upload folders found."
    for folder in scope_dirs:
        if not folder.exists() or not folder.is_dir():
            continue
        folder_name = folder.name
        try:
            for p in folder.iterdir():
                if not p.is_file():
                    continue
                if not _file_matches_kind(p.name, kind):
                    continue
                if not _file_matches_date(p, target_date):
                    continue
                arcname = f"{folder_name}/{p.name}"
                files.append((p, arcname))
        except Exception as exc:  # noqa: BLE001
            return [], f"Could not read upload folder '{folder}': {exc}"
    files.sort(key=lambda x: x[1])
    return files, ""


def _list_batch_download_folders() -> list[str]:
    """List Arduino upload folders under data/files."""
    out: list[str] = []
    root = WEB_FILE_OUTPUT_ROOT
    if not root.exists() or not root.is_dir():
        return out
    for p in sorted(root.iterdir(), key=lambda x: x.name.upper()):
        if p.is_dir():
            out.append(p.name)
    return out


def _collect_available_batch_dates(max_items: int = 60) -> list[str]:
    """Collect distinct YYYY-MM-DD dates present in Gateway upload files."""
    dates: set[str] = set()
    for folder in _list_batch_download_folders():
        d = WEB_FILE_OUTPUT_ROOT / folder
        try:
            for p in d.iterdir():
                if not p.is_file():
                    continue
                name_u = p.name.upper()
                m = re.search(r"(TR|RF|DL)(\d{6})", name_u)
                file_date: dt.date | None = None
                if m:
                    try:
                        file_date = dt.datetime.strptime(m.group(2), "%y%m%d").date()
                    except ValueError:
                        file_date = None
                if file_date is None:
                    try:
                        file_date = dt.date.fromtimestamp(p.stat().st_mtime)
                    except Exception:
                        continue
                dates.add(file_date.isoformat())
        except Exception:
            continue
    return sorted(dates, reverse=True)[:max_items]


def render_batch_downloads_page(message: str = "") -> bytes:
    """Render batch downloads page."""
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    ctx = PageContext(page_title=f"{WEB_APP_NAME} - Batch Downloads", state=state, message=message, subtitle="Batch Downloads")

    folders = _list_batch_download_folders()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    type_options_html = "\n".join(
        [
            '<option value="">Choose file type...</option>',
            '<option value="TR">TR files</option>',
            '<option value="RF">RF files</option>',
            '<option value="TRRF">TR+RF</option>',
            '<option value="DL">DL files</option>',
            '<option value="ALL">ALL</option>',
        ]
    )

    folders_text = "\n".join(folders) if folders else "(No Arduino folders found under data/files)"

    extra_css = """
    .batch-controls { margin-top: 0.8rem; display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; }
    .batch-controls select { padding: 0.45rem; border: 1px solid #9cb2c9; border-radius: 4px; background: #fff; color: #1f2937; }
    .batch-controls input[type="date"] { padding: 0.45rem; border: 1px solid #9cb2c9; border-radius: 4px; background: #fff; color: #1f2937; }
    .batch-note { margin-top: 0.4rem; color: #4b5563; font-size: 0.88rem; }
    .folder-box { white-space: pre; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .progress-overlay {
      position: fixed;
      inset: 0;
      background: rgba(13, 29, 47, 0.35);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9999;
    }
    .progress-card {
      background: #ffffff;
      border: 1px solid #9cb2c9;
      border-radius: 8px;
      min-width: 320px;
      padding: 0.8rem 1rem;
      box-shadow: 0 12px 24px rgba(0, 0, 0, 0.18);
      color: #1f2937;
    }
    .progress-title { margin: 0 0 0.45rem 0; font-weight: 700; }
    .progress-meta { margin: 0.45rem 0 0 0; font-size: 0.88rem; color: #475569; }
    .progress-bar-wrap {
      width: 100%;
      height: 10px;
      border: 1px solid #9cb2c9;
      border-radius: 999px;
      overflow: hidden;
      background: #e5edf6;
    }
    .progress-bar {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #2f75bb 0%, #0b5ea8 100%);
      transition: width 0.15s linear;
    }
    """
    body_html = f"""
      {_render_controls_row([
          _render_action_form(action="/", label="Dashboard", method="get"),
          _render_action_form(action="/file-transfers", label="File Transfers", method="get"),
          _render_action_form(action="/maintenance", label="Maintenance", method="get"),
      ])}

      <form method="get" action="/batch-downloads-download" id="batch-download-form">
        <div class="batch-controls">
          <label for="batch-date">Date:</label>
          <input id="batch-date" name="date" type="date" value="{html.escape(yesterday)}" />
          <label for="batch-kind">Type:</label>
          <select id="batch-kind" name="kind">{type_options_html}</select>
          <button type="submit" id="batch-download-button" disabled>Download to laptop</button>
        </div>
      </form>
      <div class="batch-note">Search scope: all Arduino folders under <code>data/files</code>.</div>

      {_render_titled_scroll_panel("Arduino Folders (from data/files)", "batch-folders-box", folders_text, extra_classes="folder-box")}
      {_render_titled_scroll_panel("Batch Activity Log", "batch-activity-box", "Batch Downloads page ready.")}

      <div id="batch-progress-overlay" class="progress-overlay">
        <div class="progress-card">
          <div class="progress-title" id="batch-progress-title">Preparing batch download...</div>
          <div class="progress-bar-wrap"><div id="batch-progress-bar" class="progress-bar"></div></div>
          <p class="progress-meta" id="batch-progress-meta">0%</p>
        </div>
      </div>
    """
    script_js = """
  (function() {
    const form = document.getElementById("batch-download-form");
    const dateSel = document.getElementById("batch-date");
    const kindSel = document.getElementById("batch-kind");
    const dlBtn = document.getElementById("batch-download-button");
    const activityBox = document.getElementById("batch-activity-box");
    const overlay = document.getElementById("batch-progress-overlay");
    const progTitle = document.getElementById("batch-progress-title");
    const progBar = document.getElementById("batch-progress-bar");
    const progMeta = document.getElementById("batch-progress-meta");
    if (!form || !dateSel || !kindSel || !dlBtn || !activityBox || !overlay || !progTitle || !progBar || !progMeta) return;

    function appendLog(line) {
      const ts = new Date().toLocaleTimeString();
      const cur = activityBox.textContent || "";
      const next = (cur ? (cur + "\\n") : "") + "[" + ts + "] " + line;
      activityBox.textContent = next;
      activityBox.scrollTop = activityBox.scrollHeight;
    }
    function setProgress(visible, title, pct, meta) {
      overlay.style.display = visible ? "flex" : "none";
      progTitle.textContent = title || "";
      const safePct = Math.max(0, Math.min(100, Number.isFinite(pct) ? pct : 0));
      progBar.style.width = safePct.toFixed(1) + "%";
      progMeta.textContent = meta || (safePct.toFixed(1) + "%");
    }
    function parseFilenameFromDisposition(contentDisposition) {
      if (!contentDisposition) return "batch_download.zip";
      const m = /filename=\"([^\"]+)\"/i.exec(contentDisposition);
      if (m && m[1]) return m[1];
      return "batch_download.zip";
    }

    function refreshButtonState() {
      const kind = (kindSel.value || "").trim();
      dlBtn.disabled = (kind.length < 1);
    }
    kindSel.addEventListener("change", refreshButtonState);
    refreshButtonState();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const date = (dateSel.value || "").trim();
      const kind = (kindSel.value || "").trim().toUpperCase();
      if (!date) {
        window.alert("Choose a date first.");
        appendLog("Missing date selection.");
        return;
      }
      if (!kind) {
        window.alert("Choose a file type first.");
        appendLog("Missing file-type selection.");
        return;
      }
      const previewUrl = "/api/batch-downloads/preview?date=" + encodeURIComponent(date) + "&kind=" + encodeURIComponent(kind);
      let payload = null;
      appendLog("Building preview for date=" + date + " type=" + kind + "...");
      setProgress(true, "Preparing batch preview...", 5, "Preparing...");
      try {
        const resp = await fetch(previewUrl, { cache: "no-store" });
        payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {
          setProgress(false, "", 0, "");
          appendLog("Preview failed: " + ((payload && payload.message) ? payload.message : "unknown error"));
          window.alert((payload && payload.message) ? payload.message : "Could not build batch preview.");
          return;
        }
      } catch (_err) {
        setProgress(false, "", 0, "");
        appendLog("Preview failed: request error.");
        window.alert("Could not build batch preview.");
        return;
      }
      const count = Number(payload.file_count || 0);
      const totalMb = Number(payload.total_mb || 0);
      if (count < 1) {
        setProgress(false, "", 0, "");
        appendLog("No matching files found.");
        window.alert("No matching files found for that date/type.");
        return;
      }
      appendLog("Preview ready: files=" + count + " total_mb=" + totalMb.toFixed(3));
      const msg = "Batch download summary:\\n"
        + "Date: " + payload.date + "\\n"
        + "Type: " + payload.kind + "\\n"
        + "Files: " + count + "\\n"
        + "Total MB: " + totalMb.toFixed(3) + "\\n\\n"
        + "Confirm download?";
      const ok = window.confirm(msg);
      if (!ok) {
        setProgress(false, "", 0, "");
        appendLog("User canceled batch download.");
        return;
      }
      const downloadUrl = "/batch-downloads-download?date=" + encodeURIComponent(payload.date) + "&kind=" + encodeURIComponent(payload.kind);
      appendLog("Starting download...");
      setProgress(true, "Downloading batch zip...", 8, "Starting transfer...");
      dlBtn.disabled = true;
      try {
        const resp = await fetch(downloadUrl, { cache: "no-store" });
        if (!resp.ok || !resp.body) {
          setProgress(false, "", 0, "");
          appendLog("Download failed: HTTP " + resp.status);
          window.alert("Download failed (HTTP " + resp.status + ").");
          return;
        }
        const ctype = (resp.headers.get("Content-Type") || "").toLowerCase();
        if (ctype.indexOf("application/zip") < 0) {
          const errText = await resp.text();
          setProgress(false, "", 0, "");
          appendLog("Download failed: expected ZIP response, got '" + ctype + "'.");
          window.alert("Download failed: server did not return a ZIP file.");
          if (errText && errText.length > 0) {
            appendLog("Server response snippet: " + errText.slice(0, 120).replace(/\\s+/g, " "));
          }
          return;
        }
        const total = Number(resp.headers.get("Content-Length") || "0");
        const filename = parseFilenameFromDisposition(resp.headers.get("Content-Disposition") || "");
        const reader = resp.body.getReader();
        const chunks = [];
        let received = 0;
        while (true) {
          const r = await reader.read();
          if (r.done) break;
          if (r.value) {
            chunks.push(r.value);
            received += r.value.length;
            if (total > 0) {
              const pct = (received * 100.0) / total;
              setProgress(true, "Downloading batch zip...", pct, pct.toFixed(1) + "% (" + received + "/" + total + " bytes)");
            } else {
              setProgress(true, "Downloading batch zip...", 50, "Received " + received + " bytes...");
            }
          }
        }
        setProgress(true, "Finalizing download...", 100, "Saving file to browser...");
        if (received < 1) {
          setProgress(false, "", 0, "");
          appendLog("Download failed: ZIP payload was empty.");
          window.alert("Download failed: empty ZIP payload.");
          return;
        }
        const blob = new Blob(chunks, { type: "application/zip" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = (filename && filename.trim().length > 0) ? filename : "batch_download.zip";
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.setTimeout(() => { URL.revokeObjectURL(url); }, 60000);
        appendLog("Download complete: " + filename + " (" + (received / (1024 * 1024)).toFixed(3) + " MB)");
      } catch (_err) {
        appendLog("Download failed: network/stream error.");
        window.alert("Download failed while transferring data.");
      } finally {
        setProgress(false, "", 0, "");
        refreshButtonState();
      }
    });
  })();
"""
    return _render_shared_page(
        ctx=ctx,
        body_html=body_html,
        web_app_header=WEB_APP_HEADER,
        active_network_profile=ACTIVE_NETWORK_PROFILE,
        active_network_profile_source=ACTIVE_NETWORK_PROFILE_SOURCE,
        extra_css=extra_css,
        script_js=script_js,
    )


def get_batch_download_preview_payload(date_raw: str, kind: str) -> dict[str, object]:
    """Return preview stats for batch download across all Arduino folders."""
    target_date, date_err = _parse_bundle_date(date_raw)
    k = (kind or "").strip().upper()
    if target_date is None:
        return {"ok": False, "message": date_err}
    if k not in {"TR", "RF", "TRRF", "DL", "ALL"}:
        return {"ok": False, "message": "Invalid type. Choose TR, RF, TR+RF, DL, or ALL."}
    files, err = _iter_bundle_source_files(selected_uid="", kind=k, target_date=target_date)
    if err:
        return {"ok": False, "message": err}
    total_bytes = 0
    for p, _arc in files:
        try:
            total_bytes += p.stat().st_size
        except Exception:
            continue
    return {
        "ok": True,
        "date": target_date.isoformat(),
        "kind": k,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "total_mb": float(total_bytes) / (1024.0 * 1024.0),
    }


def _build_remote_rows_html(rows: list[tuple[str, int]]) -> str:
    """Build remote rows html."""
    filtered = []
    for name, size in rows:
        n = (name or "").strip().upper()
        if n.startswith("TR") or n.startswith("DL") or n.startswith("RF"):
            filtered.append((name, size))
    if not filtered:
        return '<div style="font-style:italic;">(No files reported by Arduino)</div>'
    out = []
    out.append('<div class="sd-head">filename                          size_mb</div>')
    out.append('<div class="sd-sep">--------------------------------  -------</div>')
    for name, size in sorted(filtered, key=lambda x: x[0], reverse=True):
        size_mb = float(size) / (1024.0 * 1024.0)
        line = f"{name:<32}  {size_mb:>7.3f}"
        out.append(
            f'<div class="sd-row" data-name="{html.escape(name, quote=True)}">{html.escape(line)}</div>'
        )
    return "".join(out)


def upload_selected_remote_file(
    unique_id: str,
    short_uid: str,
    network_uid: str,
    burrow_id: str,
    ap_id: str,
    device_ip: str,
    remote_filename: str,
    progress_callback=None,
) -> tuple[bool, str, dict[str, str | float]]:
    """Upload one selected SD-card file from the chosen Arduino."""
    uid = (unique_id or "").strip()
    sid = (short_uid or "").strip().upper()
    net_uid = (network_uid or "").strip()
    burrow = (burrow_id or "").strip()
    ap = (ap_id or "").strip()
    ip = (device_ip or "").strip()
    rfn = (remote_filename or "").strip()
    if not uid or not ip or not rfn:
        return (
            False,
            "Missing uid/device_ip/remote filename.",
            {
                "unique_id": uid,
                "network_uid": net_uid,
                "burrow_id": burrow,
                "ap_id": ap,
                "device_ip": ip,
                "source_filename": rfn,
                "saved_path": "",
                "status": "error",
                "message": "Missing uid/device_ip/remote filename.",
                "error_text": "missing-required-input",
                "duration_s": 0.0,
            },
        )
    if not rfn.lower().endswith(".txt"):
        msg = f"Only .txt files can be uploaded (selected: {rfn})."
        return (
            False,
            msg,
            {
                "unique_id": uid,
                "network_uid": net_uid,
                "burrow_id": burrow,
                "ap_id": ap,
                "device_ip": ip,
                "source_filename": rfn,
                "saved_path": "",
                "status": "error",
                "message": msg,
                "error_text": "non-txt-file-blocked",
                "duration_s": 0.0,
            },
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start_ts = time.monotonic()
    run_id = f"WEB_MANUAL_{int(time.time() * 1000)}"
    active_marked = False
    try:
        try:
            set_transfer_active(
                db_path=Path(DEFAULT_DB_PATH),
                unique_id=uid,
                run_id=run_id,
                ap_id=ap,
                device_ip=ip,
                source_filename=rfn,
            )
            active_marked = True
        except Exception:
            active_marked = False
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        requested_bind = (DEFAULT_BIND_IP or "").strip() or "0.0.0.0"
        try:
            sock.bind((requested_bind, 0))
        except OSError:
            if requested_bind != "0.0.0.0":
                sock.bind(("0.0.0.0", 0))
            else:
                raise
        sock.settimeout(0.2)

        display_id = sid if sid else (uid[-6:] if len(uid) >= 6 else uid).upper()
        out_dir = WEB_FILE_OUTPUT_ROOT / display_id
        local_name = build_local_filename(rfn, uid, device_short_uid=sid if sid else None)
        local_name = ensure_unique_filename(local_name, out_dir)

        saved_path = transfer_file_protocol(
            control_sock=sock,
            device_ip=ip,
            control_port=DISCOVER_CONTROL_PORT,
            local_bind_ip=requested_bind if requested_bind else "0.0.0.0",
            requested_filename=rfn,
            output_dir=out_dir,
            device_uid=uid,
            device_short_uid=sid if sid else None,
            log_root=WEB_FILE_LOG_ROOT,
            local_filename=local_name,
            timeout_s=120.0,
            tolerant_integrity=True,
            mark_partial_received=False,
            progress_callback=progress_callback,
        )
        duration_s = time.monotonic() - start_ts
        result = {
            "unique_id": uid,
            "network_uid": net_uid,
            "burrow_id": burrow,
            "ap_id": ap,
            "device_ip": ip,
            "source_filename": rfn,
            "saved_path": str(saved_path),
            "status": "saved",
            "message": f"Uploaded selected file '{rfn}' -> {saved_path}",
            "error_text": "",
            "duration_s": duration_s,
        }
        return True, str(result["message"]), result
    except Exception as exc:  # noqa: BLE001
        duration_s = time.monotonic() - start_ts
        msg = f"Upload failed for '{rfn}': {exc}"
        result = {
            "unique_id": uid,
            "network_uid": net_uid,
            "burrow_id": burrow,
            "ap_id": ap,
            "device_ip": ip,
            "source_filename": rfn,
            "saved_path": "",
            "status": "error",
            "message": msg,
            "error_text": str(exc),
            "duration_s": duration_s,
        }
        return False, msg, result
    finally:
        if active_marked:
            try:
                clear_transfer_active(db_path=Path(DEFAULT_DB_PATH), unique_id=uid)
            except Exception:
                pass
        sock.close()


def _read_full_history_for_device(db_path: Path, unique_id: str) -> tuple[list[tuple[str, str, str]], str]:
    """Read full history for device."""
    if not db_path.exists():
        return [], "(No SQLite DB yet)"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT event_ts, source, detail
                FROM (
                  SELECT
                    COALESCE(event_ts, '') AS event_ts,
                    'DISCOVERY' AS source,
                    ('status=' || COALESCE(status, '') || ' msg=' || COALESCE(message, '')) AS detail
                  FROM discovery_events
                  WHERE unique_id = ?

                  UNION ALL

                  SELECT
                    COALESCE(event_ts, '') AS event_ts,
                    'TRANSFER' AS source,
                    ('status=' || COALESCE(status, '') || ' file=' || COALESCE(source_filename, '') ||
                     ' duration_min=' || COALESCE(CAST(ROUND(duration_s / 60.0, 1) AS TEXT), '') ||
                     ' msg=' || COALESCE(message, '')) AS detail
                  FROM transfer_events
                  WHERE unique_id = ?

                  UNION ALL

                  SELECT
                    COALESCE(event_ts, '') AS event_ts,
                    'SLOT' AS source,
                    ('action=' || COALESCE(action, '') || ' ap=' || COALESCE(ap_id, '') ||
                     ' total=' || COALESCE(CAST(running_total AS TEXT), '0') ||
                     ' on_ap=' || COALESCE(CAST(running_on_ap AS TEXT), '0')) AS detail
                  FROM slot_events
                  WHERE unique_id = ?
                )
                ORDER BY event_ts DESC
                """,
                (unique_id, unique_id, unique_id),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return [], f"(History unavailable: {exc})"

    out = [(str(r[0] or ""), str(r[1] or ""), str(r[2] or "")) for r in rows]
    return out, ""


def _resolve_device_context_for_uid(selected_uid: str) -> tuple[dict[str, str] | None, str, str]:
    """Resolve selected UID into device row plus IP and short UID."""
    uid = (selected_uid or "").strip()
    if not uid:
        return None, "", ""
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    device = _find_device_by_uid(devices, uid)
    if device is None:
        return None, "", ""
    short_uid = (device.get("short_uid", "") or "").strip()
    if not short_uid:
        short_uid = uid[-6:] if len(uid) >= 6 else uid
    device_ip = (device.get("device_ip", "") or device.get("recv_ip", "")).strip()
    return device, short_uid, device_ip


def _history_text_for_device_uid(unique_id: str) -> str:
    """Render full transfer/discovery history text for one device UID."""
    uid = (unique_id or "").strip()
    if not uid:
        return "(Select a known Arduino to view complete DB history)"
    history_rows, history_err = _read_full_history_for_device(Path(DEFAULT_DB_PATH), uid)
    if history_err:
        return history_err
    if not history_rows:
        return "(No DB history for this Arduino)"
    lines = []
    lines.append("event_ts              source      detail")
    lines.append("-------------------  ----------  -----------------------------------------------")
    for ts, source, detail in history_rows:
        lines.append(f"{ts:<19}  {source:<10}  {detail}")
    return "\n".join(lines)


def get_file_transfers_remote_files_payload(selected_uid: str) -> dict[str, object]:
    """Get file transfers remote files payload."""
    uid = (selected_uid or "").strip()
    if not uid:
        return {"ok": False, "message": "missing uid"}
    _device, short_uid, device_ip = _resolve_device_context_for_uid(uid)
    if not device_ip:
        return {"ok": False, "message": "Selected Arduino has no IP address."}
    ok, reason = can_web_access_file_transfers(uid, "LIST_FILES")
    if not ok:
        return {"ok": False, "message": reason}
    remote_items, remote_err = _request_remote_file_list_with_sizes(device_ip, timeout_s=8.0)
    if remote_err:
        return {"ok": True, "short_uid": short_uid, "html": "", "note": f"(Could not fetch files: {remote_err})"}
    if not remote_items:
        return {"ok": True, "short_uid": short_uid, "html": "", "note": "(No files reported by Arduino)"}
    return {"ok": True, "short_uid": short_uid, "html": _build_remote_rows_html(remote_items), "note": ""}


def get_file_transfers_uploaded_files_payload(selected_uid: str) -> dict[str, object]:
    """Get file transfers uploaded files payload."""
    uid = (selected_uid or "").strip()
    if not uid:
        return {"ok": False, "message": "missing uid"}
    _device, short_uid, _device_ip = _resolve_device_context_for_uid(uid)
    if not short_uid:
        return {"ok": False, "message": "No short UID available for selected Arduino."}
    uploaded_rows, uploaded_err = _read_uploaded_files_for_device(short_uid)
    if uploaded_err:
        return {"ok": True, "short_uid": short_uid, "html": "", "note": uploaded_err}
    if not uploaded_rows:
        return {"ok": True, "short_uid": short_uid, "html": "", "note": "(No Gateway-saved files found for this Arduino)"}
    return {"ok": True, "short_uid": short_uid, "html": _build_uploaded_rows_html(uploaded_rows), "note": ""}


def get_file_transfers_history_payload(selected_uid: str) -> dict[str, object]:
    """Get file transfers history payload."""
    uid = (selected_uid or "").strip()
    if not uid:
        return {"ok": False, "message": "missing uid"}
    _device, short_uid, _device_ip = _resolve_device_context_for_uid(uid)
    text = _history_text_for_device_uid(uid)
    return {"ok": True, "short_uid": short_uid, "text": text}


def get_maintenance_panels_payload(selected_uid: str) -> dict[str, object]:
    """Get maintenance panels payload."""
    uid = (selected_uid or "").strip()
    if not uid:
        return {"ok": False, "message": "missing uid"}
    _device, short_uid, device_ip = _resolve_device_context_for_uid(uid)
    if not device_ip:
        return {"ok": False, "message": "Selected Arduino has no IP address."}
    ok, reason = can_web_contact_arduino(uid, "Maintenance panel refresh")
    if not ok:
        return {"ok": False, "message": reason}
    raw_panels = _maintenance_panel_data(device_ip=device_ip)
    payload_panels: dict[str, str] = {}
    for key in ["RTC Time", "Status", "Config", "Diagnostics"]:
        header, sep, values = raw_panels.get(key, ("result", "------", ""))
        payload_panels[key] = _mini_panel_block(header, sep, values)
    return {"ok": True, "short_uid": short_uid, "panels": payload_panels}


def get_devices_table_payload() -> dict[str, object]:
    """Return shared Known Arduinos HTML table payload for dashboard."""
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    html_block = _build_device_select_rows(devices, selected_uid="")
    mismatches: list[str] = []
    for d in devices:
        uid = (d.get("unique_id", "") or "").strip()
        short_uid = (d.get("short_uid", "") or "").strip()
        if not short_uid:
            short_uid = uid[-6:] if len(uid) >= 6 else uid
        dev_ip = (d.get("device_ip", "") or "").strip()
        recv_ip = (d.get("recv_ip", "") or "").strip()
        if dev_ip and recv_ip and dev_ip != recv_ip:
            mismatches.append(short_uid if short_uid else uid)
    mismatch_message = ""
    if mismatches:
        mismatch_message = f"WARNING: device_ip != recv_ip for {len(mismatches)} device(s): {', '.join(mismatches)}"
    return {"ok": True, "html": html_block, "mismatch_message": mismatch_message}


def _read_text_preview(path: Path, max_bytes: int = 256 * 1024) -> str:
    """Read capped text preview from a local file path."""
    raw = path.read_bytes()
    clipped = raw[:max_bytes]
    text = clipped.decode("utf-8", errors="replace")
    if len(raw) > max_bytes:
        text += f"\n\n[preview truncated at {max_bytes} bytes; file is {len(raw)} bytes]"
    return text


def get_rf_data_local_preview_payload(selected_uid: str, saved_path: str) -> dict[str, object]:
    """Preview a Gateway-uploaded file from the selected row."""
    target, err = resolve_local_uploaded_file_for_download(saved_path=saved_path, selected_uid=selected_uid)
    if target is None:
        return {"ok": False, "message": err}
    try:
        text = _read_text_preview(target)
        return {"ok": True, "source": "gateway", "name": target.name, "text": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Could not read local preview: {exc}"}


def get_rf_data_remote_preview_payload(selected_uid: str, remote_filename: str) -> dict[str, object]:
    """Preview an Arduino SD file by transferring it to a temporary local file."""
    uid = (selected_uid or "").strip()
    rfn = (remote_filename or "").strip()
    if not uid:
        return {"ok": False, "message": "missing uid"}
    if not rfn:
        return {"ok": False, "message": "missing remote filename"}
    _device, _short_uid, device_ip = _resolve_device_context_for_uid(uid)
    if not device_ip:
        return {"ok": False, "message": "Selected Arduino has no IP address."}
    ok, reason = can_web_contact_arduino(uid, "RF Data remote preview")
    if not ok:
        return {"ok": False, "message": reason}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        requested_bind = (DEFAULT_BIND_IP or "").strip() or "0.0.0.0"
        try:
            sock.bind((requested_bind, 0))
        except OSError:
            if requested_bind != "0.0.0.0":
                sock.bind(("0.0.0.0", 0))
            else:
                raise
        sock.settimeout(0.2)
        with TemporaryDirectory(prefix="rf_preview_") as td:
            out_dir = Path(td)
            saved_path = transfer_file_protocol(
                control_sock=sock,
                device_ip=device_ip,
                control_port=DISCOVER_CONTROL_PORT,
                local_bind_ip=requested_bind if requested_bind else "0.0.0.0",
                requested_filename=rfn,
                output_dir=out_dir,
                timeout_s=60.0,
                tolerant_integrity=True,
                mark_partial_received=False,
            )
            text = _read_text_preview(saved_path)
            return {"ok": True, "source": "arduino", "name": rfn, "text": text}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Could not preview remote file '{rfn}': {exc}"}
    finally:
        sock.close()


def render_rf_data_page(message: str = "", selected_uid: str = "") -> bytes:
    """Render RF Data page with remote/local lists plus file-content preview panel."""
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    ctx = PageContext(page_title=f"{WEB_APP_NAME} - RF Data", state=state, message=message, subtitle="RF Data")
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    selected_uid = (selected_uid or "").strip()
    selected_device = _find_device_by_uid(devices, selected_uid)
    device_rows_html_block = _build_device_select_rows(devices, selected_uid)

    selected_short = ""
    remote_note = "(Select a known Arduino to view SD files)"
    uploaded_note = "(Select a known Arduino to view upload history)"
    preview_note = "(Select a file from either list to preview contents)"
    if selected_device is not None:
        selected_short = (selected_device.get("short_uid", "") or "").strip()
        if not selected_short:
            selected_short = selected_uid[-6:] if len(selected_uid) >= 6 else selected_uid
        remote_note = "Loading files from Arduino..."
        uploaded_note = "Loading uploaded-file list..."

    files_title_suffix = selected_short if selected_short else "..."
    extra_css = """
    .device-head, .device-sep { white-space: pre; }
    .device-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .device-row:hover { background: #eef5ff; }
    .device-row.selected { background: #d7e9ff; font-weight: 700; }
    .sd-head, .sd-sep { white-space: pre; }
    .sd-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .sd-row:hover { background: #eef5ff; }
    .sd-row.selected { background: #c8f7d1; font-weight: 700; }
    .upload-head, .upload-sep { white-space: pre; }
    .upload-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .upload-row:hover { background: #eef5ff; }
    .upload-row.selected { background: #ffe1ba; font-weight: 700; }
    .grid2 { margin-top: 0.8rem; display: grid; gap: 0.8rem; grid-template-columns: 1fr 1fr; }
    .preview-row { margin-top: 0.8rem; display: grid; gap: 0.8rem; grid-template-columns: 1fr 1fr; align-items: start; }
    .preview-box-narrow { max-width: none; }
    .sort-panel { border: 1px solid var(--line); background: #fbfdff; border-radius: 6px; padding: 0.65rem; }
    .sort-panel label { display: block; margin-bottom: 0.35rem; font-weight: 700; color: #304a64; }
    .sort-panel select { width: 100%; padding: 0.45rem; border: 1px solid #9cb2c9; border-radius: 4px; background: #fff; color: #1f2937; }
    .sort-note { margin-top: 0.55rem; color: #4b5563; font-size: 0.86rem; }
    .loading-indicator { display: none; margin: 0.25rem 0 0.4rem 0; font-size: 0.88rem; color: #1f4f82; font-weight: 700; }
    .loading-bar-wrap { width: 220px; height: 8px; border: 1px solid #9cb2c9; border-radius: 999px; background: #e5edf6; overflow: hidden; margin-top: 0.3rem; }
    .loading-bar { width: 42%; height: 100%; background: linear-gradient(90deg, #2f75bb, #0b5ea8); animation: rfLoad 1s linear infinite; }
    @keyframes rfLoad { 0% { transform: translateX(-120%);} 100% { transform: translateX(280%);} }
    @media (max-width: 1200px) { .grid2 { grid-template-columns: 1fr; } .preview-row { grid-template-columns: 1fr; } }
    """
    body_html = f"""
      {_render_controls_row([
          _render_action_form(action="/", label="Dashboard", method="get"),
          _render_action_form(action="/file-transfers", label="File Transfers", method="get"),
          _render_action_form(action="/batch-downloads", label="Batch Downloads", method="get"),
      ])}

      {_render_known_arduinos_selector(
          action="/rf-data",
          selected_uid=selected_uid,
          device_rows_html_block=device_rows_html_block,
          button_label="Load RF Data",
          show_button=False,
      )}

      <div class="grid2">
        <div>
          <div class="section-title" id="rf-files-on-title">{html.escape(f"Files on {files_title_suffix}")}</div>
          {_render_scrollbox("rf-sd-list-box", remote_note)}
        </div>
        <div>
          <div class="section-title" id="rf-files-uploaded-title">{html.escape(f"Files uploaded from {files_title_suffix}")}</div>
          {_render_scrollbox("rf-uploaded-list-box", uploaded_note)}
        </div>
      </div>
      <div class="preview-row">
        <div>
          <div class="section-title" id="rf-preview-title">Selected File Contents</div>
          <div id="rf-loading-indicator" class="loading-indicator">
            Loading file contents...
            <div class="loading-bar-wrap"><div class="loading-bar"></div></div>
          </div>
          {_render_scrollbox("rf-preview-box", preview_note, "preview-box-narrow")}
        </div>
        <div>
          <div class="sort-panel">
            <label for="rf-sort-order">Sort Contents</label>
            <select id="rf-sort-order">
              <option value="asc">oldest to newest</option>
              <option value="desc">newest to oldest</option>
            </select>
            <div class="sort-note">Sort is applied when loading selected file contents.</div>
          </div>
        </div>
      </div>
"""
    script_js = """
  (function() {
    const rows = Array.from(document.querySelectorAll(".device-row"));
    const uidFields = Array.from(document.querySelectorAll(".selected-uid-field"));
    const sdListBox = document.getElementById("rf-sd-list-box");
    const uploadedListBox = document.getElementById("rf-uploaded-list-box");
    const previewBox = document.getElementById("rf-preview-box");
    const previewTitle = document.getElementById("rf-preview-title");
    const loadingIndicator = document.getElementById("rf-loading-indicator");
    const sortOrder = document.getElementById("rf-sort-order");
    const filesOnTitle = document.getElementById("rf-files-on-title");
    const filesUploadedTitle = document.getElementById("rf-files-uploaded-title");
    let selectedSource = "";
    let selectedRemoteName = "";
    let selectedLocalPath = "";
    let selectedLocalName = "";
    function getSelectedUid() {
      for (const f of uidFields) {
        const v = (f.value || "").trim();
        if (v.length > 0) return v;
      }
      return "";
    }
    function getSdRows() { return Array.from(document.querySelectorAll("#rf-sd-list-box .sd-row")); }
    function getUploadRows() { return Array.from(document.querySelectorAll("#rf-uploaded-list-box .upload-row")); }
    function clearSelections() {
      getSdRows().forEach((r) => r.classList.remove("selected"));
      getUploadRows().forEach((r) => r.classList.remove("selected"));
      selectedSource = "";
      selectedRemoteName = "";
      selectedLocalPath = "";
      selectedLocalName = "";
    }
    function showLoading(show) {
      if (!loadingIndicator) return;
      loadingIndicator.style.display = show ? "block" : "none";
    }
    function currentOrder() {
      if (!sortOrder) return "asc";
      const v = (sortOrder.value || "").trim().toLowerCase();
      return (v === "desc") ? "desc" : "asc";
    }
    function setSelectedUid(uid, triggerLoad = true) {
      let selectedShort = "...";
      uidFields.forEach((f) => { f.value = uid; });
      rows.forEach((r) => {
        if (r.dataset.uid === uid) {
          r.classList.add("selected");
          selectedShort = (r.dataset.short || "").trim() || "...";
        } else r.classList.remove("selected");
      });
      if (filesOnTitle) filesOnTitle.textContent = "Files on " + selectedShort;
      if (filesUploadedTitle) filesUploadedTitle.textContent = "Files uploaded from " + selectedShort;
      if (previewTitle) previewTitle.textContent = "Selected File Contents";
      if (previewBox) previewBox.textContent = "(Select a file from either list to preview contents)";
      showLoading(false);
      if (triggerLoad) loadAll(uid);
    }
    rows.forEach((r) => { r.addEventListener("click", () => setSelectedUid(r.dataset.uid || "", true)); });
    async function loadRemoteFiles(uid) {
      if (!sdListBox) return;
      if (!uid) { sdListBox.textContent = "(Select a known Arduino to view SD files)"; return; }
      sdListBox.textContent = "Loading files from Arduino...";
      try {
        const resp = await fetch("/api/file-transfers/remote-files?uid=" + encodeURIComponent(uid), { cache: "no-store" });
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) { sdListBox.textContent = payload && payload.message ? payload.message : "(Could not fetch files.)"; return; }
        if (payload.html && payload.html.length > 0) sdListBox.innerHTML = payload.html;
        else sdListBox.textContent = payload.note || "(No files reported by Arduino)";
      } catch (_err) {
        sdListBox.textContent = "(Could not fetch files.)";
      }
      bindSdRows();
    }
    async function loadUploadedFiles(uid) {
      if (!uploadedListBox) return;
      if (!uid) { uploadedListBox.textContent = "(Select a known Arduino to view upload history)"; return; }
      uploadedListBox.textContent = "Loading uploaded-file list...";
      try {
        const resp = await fetch("/api/file-transfers/uploaded-files?uid=" + encodeURIComponent(uid), { cache: "no-store" });
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) { uploadedListBox.textContent = payload && payload.message ? payload.message : "(Could not load uploaded files.)"; return; }
        if (payload.html && payload.html.length > 0) uploadedListBox.innerHTML = payload.html;
        else uploadedListBox.textContent = payload.note || "(No uploaded files logged for this Arduino)";
      } catch (_err) {
        uploadedListBox.textContent = "(Could not load uploaded files.)";
      }
      bindUploadedRows();
    }
    function sortKey(row) {
      if (!row || row.length < 1) return [2, ""];
      const first = (row[0] || "").trim();
      if (/^[0-9]+$/.test(first)) return [0, Number.parseInt(first, 10)];
      const asNum = Number.parseFloat(first);
      if (Number.isFinite(asNum)) return [1, asNum];
      return [2, first];
    }
    function renderRows(rows, order) {
      if (!previewBox) return;
      if (!rows || rows.length < 1) {
        previewBox.textContent = "(No rows found in file)";
        return;
      }
      const data = rows.slice();
      const desc = (order || "asc") === "desc";
      data.sort((a, b) => {
        const ka = sortKey(a);
        const kb = sortKey(b);
        if (ka[0] !== kb[0]) return desc ? (kb[0] - ka[0]) : (ka[0] - kb[0]);
        const va = ka[1];
        const vb = kb[1];
        if (typeof va === "number" && typeof vb === "number") {
          return desc ? (vb - va) : (va - vb);
        }
        const cmp = String(va).localeCompare(String(vb));
        return desc ? -cmp : cmp;
      });
      const maxRows = 3000;
      const clipped = data.slice(0, maxRows);
      let cols = 0;
      for (const r of clipped) cols = Math.max(cols, r.length);
      cols = Math.max(cols, 1);
      const widths = [];
      for (let c = 0; c < cols; c++) {
        let w = ("col" + (c + 1)).length;
        for (const r of clipped) {
          const cell = (c < r.length) ? String(r[c] || "") : "";
          if (cell.length > w) w = cell.length;
        }
        widths.push(Math.min(w, 28));
      }
      const lines = [];
      const hdr = [];
      const sep = [];
      for (let c = 0; c < cols; c++) {
        const h = ("col" + (c + 1));
        hdr.push(h.padEnd(widths[c], " "));
        sep.push("-".repeat(widths[c]));
      }
      lines.push(hdr.join("  "));
      lines.push(sep.join("  "));
      for (const r of clipped) {
        const parts = [];
        for (let c = 0; c < cols; c++) {
          let cell = (c < r.length) ? String(r[c] || "") : "";
          if (cell.length > widths[c]) cell = cell.slice(0, Math.max(1, widths[c] - 1)) + "~";
          parts.push(cell.padEnd(widths[c], " "));
        }
        lines.push(parts.join("  "));
      }
      previewBox.textContent = lines.join("\\n");
    }
    function parseRows(text) {
      const rows = [];
      for (const raw of (text || "").split(/\\r?\\n/)) {
        const line = raw.trim();
        if (!line) continue;
        rows.push(line.split(",").map((x) => x.trim()));
      }
      return rows;
    }
    let cachedRows = [];
    let cachedFileKey = "";
    function setCache(fileKey, text) {
      cachedFileKey = fileKey || "";
      cachedRows = parseRows(text);
    }
    function rerenderFromCache() {
      renderRows(cachedRows, currentOrder());
    }
    async function loadPreviewLocal(uid, savedPath, name) {
      if (!previewBox) return;
      previewTitle.textContent = "Selected File Contents - " + (name || "");
      previewBox.textContent = "";
      showLoading(true);
      try {
        const u = "/api/rf-data/preview-local?uid=" + encodeURIComponent(uid)
          + "&saved_path=" + encodeURIComponent(savedPath);
        const resp = await fetch(u, { cache: "no-store" });
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {
          previewBox.textContent = payload && payload.message ? payload.message : "(Could not load preview.)";
          return;
        }
        setCache("local:" + savedPath, payload.text || "");
        rerenderFromCache();
      } catch (_err) {
        previewBox.textContent = "(Could not load preview.)";
      } finally {
        showLoading(false);
      }
    }
    async function loadPreviewRemote(uid, remoteFilename) {
      if (!previewBox) return;
      previewTitle.textContent = "Selected File Contents - " + (remoteFilename || "");
      previewBox.textContent = "";
      showLoading(true);
      try {
        const u = "/api/rf-data/preview-remote?uid=" + encodeURIComponent(uid)
          + "&remote_filename=" + encodeURIComponent(remoteFilename);
        const resp = await fetch(u, { cache: "no-store" });
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {
          previewBox.textContent = payload && payload.message ? payload.message : "(Could not load preview.)";
          return;
        }
        setCache("remote:" + remoteFilename, payload.text || "");
        rerenderFromCache();
      } catch (_err) {
        previewBox.textContent = "(Could not load preview.)";
      } finally {
        showLoading(false);
      }
    }
    function bindSdRows() {
      getSdRows().forEach((r) => {
        r.addEventListener("click", () => {
          const uid = getSelectedUid();
          clearSelections();
          r.classList.add("selected");
          selectedSource = "remote";
          selectedRemoteName = r.dataset.name || "";
          loadPreviewRemote(uid, selectedRemoteName);
        });
      });
    }
    function bindUploadedRows() {
      getUploadRows().forEach((r) => {
        r.addEventListener("click", () => {
          const uid = getSelectedUid();
          clearSelections();
          r.classList.add("selected");
          selectedSource = "local";
          selectedLocalPath = r.dataset.path || "";
          selectedLocalName = r.dataset.name || "";
          loadPreviewLocal(uid, selectedLocalPath, selectedLocalName);
        });
      });
    }
    if (sortOrder) {
      sortOrder.addEventListener("change", () => {
        const uid = getSelectedUid();
        if (!uid) return;
        if (!cachedFileKey || cachedRows.length < 1) return;
        rerenderFromCache();
      });
    }
    async function loadAll(uid) {
      await Promise.allSettled([loadRemoteFiles(uid), loadUploadedFiles(uid)]);
      clearSelections();
    }
    const initialUid = getSelectedUid();
    if (initialUid) loadAll(initialUid);
  })();
"""
    return _render_shared_page(
        ctx=ctx,
        body_html=body_html,
        web_app_header=WEB_APP_HEADER,
        active_network_profile=ACTIVE_NETWORK_PROFILE,
        active_network_profile_source=ACTIVE_NETWORK_PROFILE_SOURCE,
        extra_css=extra_css,
        script_js=script_js,
    )


def render_page(message: str = "") -> bytes:
    """Render page."""
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    ctx = PageContext(page_title=WEB_APP_NAME, state=state, message=message)
    extra_css = """
    .placeholder-btn { background: var(--accent); color: #0b2d4b; border-color: #8db4da; }
    .device-head, .device-sep { white-space: pre; }
    .device-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .device-row:hover { background: #eef5ff; }
    .warn-banner {
      margin: 0.65rem 0 0.5rem 0;
      padding: 0.55rem 0.7rem;
      border: 1px solid #d97706;
      border-radius: 6px;
      background: #fff7ed;
      color: #7c2d12;
      font-size: 0.88rem;
      display: none;
    }
    #healthbox { height: 5.2em; }
    .nav-buttons { margin-top: 0.9rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
    #activitybox { margin-top: 0.8rem; }
    """
    body_html = f"""
      {_render_controls_row([
          _render_action_form(action="/start", label="Normal Ops", method="post"),
          _render_action_form(action="/poll-now", label="Poll Now", method="post"),
      ])}
      <div class="warn-banner" style="display:block;">
        Poll Now sends repeated POLL_UID broadcasts (poll-driven discovery) and increases Arduino WiFi activity.
        Use it only for manual diagnostics.
      </div>

      <div class="nav-buttons">
        <form method="get" action="/iphone">
          <button type="submit" class="placeholder-btn">iPhone</button>
        </form>
        <form method="get" action="/file-transfers">
          <button type="submit" class="placeholder-btn">File Transfers</button>
        </form>
        <form method="get" action="/rf-data">
          <button type="submit" class="placeholder-btn">RF Data</button>
        </form>
        <form method="get" action="/maintenance">
          <button type="submit" class="placeholder-btn">Maintenance</button>
        </form>
        <form method="get" action="/batch-downloads">
          <button type="submit" class="placeholder-btn">Batch Downloads</button>
        </form>
      </div>

      {_render_section_title("Known Arduinos")}
      <div id="mismatch-banner" class="warn-banner"></div>
      {_render_scrollbox("devicebox", "Loading Arduino status...", "known-arduino-box")}

      {_render_titled_scroll_panel("Uploads Today", "uploadsbox", "Loading uploaded-file list...")}

      {_render_titled_scroll_panel("Health", "healthbox", "Loading health status...")}

      {_render_titled_scroll_panel("Activity Log", "activitybox", "Loading activity output...")}
"""
    script_js = f"""
    const devicebox = document.getElementById("devicebox");
    const healthbox = document.getElementById("healthbox");
    const uploadsbox = document.getElementById("uploadsbox");
    const activitybox = document.getElementById("activitybox");
    const mismatchBanner = document.getElementById("mismatch-banner");
    let uploadsTimer = null;
    let devicesTimer = null;
    let activityTimer = null;
    let healthTimer = null;
    async function refreshHealth() {{
      try {{
        const resp = await fetch("/health-status", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        healthbox.textContent = txt || "(No health status yet)";
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}
    async function refreshUploads() {{
      try {{
        const resp = await fetch("/uploads-today", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        uploadsbox.textContent = txt || "(No uploaded files today)";
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}

    async function refreshDevices() {{
      try {{
        const resp = await fetch("/api/devices-table", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const payload = await resp.json();
        if (!payload || !payload.ok) {{
          return;
        }}
        if (payload.html && payload.html.length > 0) {{
          devicebox.innerHTML = payload.html;
        }} else {{
          devicebox.textContent = "(No device status yet)";
        }}
        const msg = (payload.mismatch_message || "").trim();
        if (msg.length > 0) {{
          mismatchBanner.textContent = msg;
          mismatchBanner.style.display = "block";
        }} else {{
          mismatchBanner.textContent = "";
          mismatchBanner.style.display = "none";
        }}
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}

    async function refreshActivity() {{
      try {{
        const resp = await fetch("/activity", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        const nearBottom = (activitybox.scrollTop + activitybox.clientHeight) >= (activitybox.scrollHeight - 30);
        activitybox.textContent = txt || "(No activity yet)";
        if (nearBottom) {{
          activitybox.scrollTop = activitybox.scrollHeight;
        }}
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}
    function stopPolling() {{
      if (uploadsTimer) {{ clearInterval(uploadsTimer); uploadsTimer = null; }}
      if (devicesTimer) {{ clearInterval(devicesTimer); devicesTimer = null; }}
      if (activityTimer) {{ clearInterval(activityTimer); activityTimer = null; }}
      if (healthTimer) {{ clearInterval(healthTimer); healthTimer = null; }}
    }}
    function startPolling() {{
      if (uploadsTimer || devicesTimer || activityTimer || healthTimer) {{
        return;
      }}
      uploadsTimer = setInterval(refreshUploads, {UI_POLL_UPLOADS_MS});
      devicesTimer = setInterval(refreshDevices, {UI_POLL_DEVICES_MS});
      activityTimer = setInterval(refreshActivity, {UI_POLL_ACTIVITY_MS});
      healthTimer = setInterval(refreshHealth, {UI_POLL_HEALTH_MS});
    }}
    async function refreshAllNow() {{
      await Promise.allSettled([refreshHealth(), refreshUploads(), refreshDevices(), refreshActivity()]);
    }}
    document.addEventListener("visibilitychange", () => {{
      if (document.hidden) {{
        stopPolling();
        return;
      }}
      refreshAllNow();
      startPolling();
    }});
    refreshAllNow();
    if (!document.hidden) {{
      startPolling();
    }}
"""
    return _render_shared_page(
        ctx=ctx,
        body_html=body_html,
        web_app_header=WEB_APP_HEADER,
        active_network_profile=ACTIVE_NETWORK_PROFILE,
        active_network_profile_source=ACTIVE_NETWORK_PROFILE_SOURCE,
        extra_css=extra_css,
        script_js=script_js,
    )


def render_file_transfers_page(message: str = "", selected_uid: str = "") -> bytes:
    """Render file transfers page."""
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    ctx = PageContext(page_title=f"{WEB_APP_NAME} - File Transfers", state=state, message=message, subtitle="File Transfers")
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    selected_uid = (selected_uid or "").strip()

    selected_device: dict[str, str] | None = None
    for d in devices:
        if (d.get("unique_id", "") or "").strip() == selected_uid:
            selected_device = d
            break

    device_rows_html_block = _build_device_select_rows(devices, selected_uid)

    selected_short = ""
    remote_note = "(Select a known Arduino to view SD files)"
    uploaded_note = "(Select a known Arduino to view Gateway-saved files)"
    history_text = "(Select a known Arduino to view complete DB history)"
    active_rows: list[dict[str, str]] = []
    active_error = ""
    try:
        active_rows = list_active_transfers(Path(DEFAULT_DB_PATH))
    except Exception as exc:  # noqa: BLE001
        active_error = str(exc)
    active_busy = len(active_rows) > 0
    busy_note_html = ""
    if active_error:
        busy_note_html = f'<p><strong>Warning:</strong> Active-transfer check failed: {html.escape(active_error)}</p>'
    elif active_busy:
        status_lines = []
        status_lines.append("Active uploads currently running:")
        status_lines.append("short_uid  unique_id                              device_ip      file               started_at")
        status_lines.append("--------   ------------------------------------   -----------    ----------------   -------------------")
        by_uid = {str(d.get("unique_id", "")): d for d in devices}
        for row in active_rows:
            uid = row.get("unique_id", "")
            d = by_uid.get(uid, {})
            short_uid = (d.get("short_uid", "") or "").strip()
            if not short_uid:
                short_uid = uid[-6:] if len(uid) >= 6 else uid
            status_lines.append(
                f"{short_uid:<8}   {uid:<36}   {row.get('device_ip', ''):<11}    "
                f"{row.get('source_filename', ''):<16}   {row.get('started_at', '')}"
            )
        busy_note_html = (
            "<div class=\"busy-note\"><pre>"
            + html.escape("\n".join(status_lines))
            + "</pre></div>"
        )

    if selected_device is not None:
        selected_short = (selected_device.get("short_uid", "") or "").strip()
        if not selected_short:
            selected_short = selected_uid[-6:] if len(selected_uid) >= 6 else selected_uid
        remote_note = "Loading files from Arduino..."
        uploaded_note = "Loading Gateway-saved file list..."
        history_text = "Loading SQLite history..."

    files_title_suffix = selected_short if selected_short else "..."
    extra_css = """
    .device-head, .device-sep { white-space: pre; }
    .device-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .device-row:hover { background: #eef5ff; }
    .device-row.selected { background: #d7e9ff; font-weight: 700; }
    .sd-head, .sd-sep { white-space: pre; }
    .sd-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .sd-row:hover { background: #eef5ff; }
    .sd-row.selected { background: #c8f7d1; font-weight: 700; }
    .upload-head, .upload-sep { white-space: pre; }
    .upload-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .upload-row:hover { background: #eef5ff; }
    .upload-row.selected { background: #ffe1ba; font-weight: 700; }
    .grid2 { margin-top: 0.8rem; display: grid; gap: 0.8rem; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
    .grid2 > div { min-width: 0; }
    .list-actions { display: flex; justify-content: flex-end; gap: 0.4rem; margin-bottom: 0.25rem; min-height: 2.2rem; }
    .delete-btn { background: #c53030; border-color: #9b2c2c; }
    .progress-overlay {
      position: fixed;
      inset: 0;
      background: rgba(13, 29, 47, 0.35);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9999;
    }
    .progress-card {
      background: #ffffff;
      border: 1px solid #9cb2c9;
      border-radius: 8px;
      min-width: 260px;
      padding: 0.8rem 1rem;
      box-shadow: 0 12px 24px rgba(0, 0, 0, 0.18);
      text-align: center;
      font-weight: 700;
      color: #1f2937;
    }
    .busy-note {
      margin: 0.3rem 0 0.8rem 0;
      border: 1px solid #e5b97a;
      background: #fff4dd;
      border-radius: 6px;
      padding: 0.5rem;
    }
    .busy-note pre {
      margin: 0;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.82rem;
      line-height: 1.3;
      color: #6e4b00;
    }
    @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
    """
    body_html = f"""
      {_render_controls_row([
          _render_action_form(action="/", label="Dashboard", method="get"),
          _render_action_form(
              action="/file-transfers",
              label="Wait/Refresh",
              method="get",
              hidden_fields=[("uid", selected_uid)],
              hidden_input_class="selected-uid-field",
              hidden_class_names={"uid"},
          ),
      ])}
      {busy_note_html}

      {_render_known_arduinos_selector(
          action="/file-transfers",
          selected_uid=selected_uid,
          device_rows_html_block=device_rows_html_block,
          button_label="Load File Lists",
          show_button=False,
      )}

      <div class="grid2">
        <div>
          <div class="section-title" id="files-on-title">{html.escape(f"Arduino SD files on {files_title_suffix}")}</div>
          <div class="list-actions">
            <form method="post" action="/file-transfers-upload-selected" id="sd-upload-form">
              <input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />
              <input type="hidden" name="remote_filename" value="" id="sd-selected-name" />
              <input type="hidden" name="upload_op_id" value="" id="sd-upload-op-id" />
              <button type="submit" id="sd-upload-button">Upload</button>
            </form>
            <form method="post" action="/file-transfers-delete-sd" id="sd-delete-form">
              <input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />
              <input type="hidden" name="remote_filename" value="" id="sd-delete-selected-name" />
              <button type="submit" class="delete-btn" id="sd-delete-button">Delete on SD</button>
            </form>
          </div>
          {_render_scrollbox("sd-list-box", remote_note)}
        </div>
        <div>
          <div class="section-title" id="files-uploaded-title">{html.escape(f"Gateway files saved for {files_title_suffix}")}</div>
          <div class="list-actions">
            <form method="get" action="/file-transfers-download-uploaded" id="uploaded-download-form">
              <input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />
              <input type="hidden" name="saved_path" value="" id="uploaded-download-path" />
              <input type="hidden" name="source_filename" value="" id="uploaded-download-name" />
              <button type="submit" id="uploaded-download-button">Download to Laptop</button>
            </form>
            <form method="post" action="/file-transfers-delete-uploaded" id="uploaded-delete-form">
              <input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />
              <input type="hidden" name="saved_path" value="" id="uploaded-selected-path" />
              <input type="hidden" name="source_filename" value="" id="uploaded-selected-name" />
              <button type="submit" class="delete-btn" id="uploaded-delete-button">Delete</button>
            </form>
          </div>
          {_render_scrollbox("uploaded-list-box", uploaded_note)}
        </div>
      </div>

      {_render_titled_scroll_panel(f"Complete SQLite History for {files_title_suffix}", "history-box", history_text)}

      {_render_titled_scroll_panel("Python Log", "pythonlogbox", "Loading python log...")}

      <div id="upload-progress-overlay" class="progress-overlay">
        <div class="progress-card" id="upload-progress-text">Uploading selected file... Please wait.</div>
      </div>
      <div id="delete-progress-overlay" class="progress-overlay">
        <div class="progress-card" id="delete-progress-text">Deleting selected file... Please wait.</div>
      </div>
"""
    script_js = f"""
  (function() {{
    const rows = Array.from(document.querySelectorAll(".device-row"));
    const uidFields = Array.from(document.querySelectorAll(".selected-uid-field"));
    const needsDeviceControls = Array.from(document.querySelectorAll(".needs-device"));
    const sdListBox = document.getElementById("sd-list-box");
    const uploadedListBox = document.getElementById("uploaded-list-box");
    const historyBox = document.getElementById("history-box");
    const uploadForm = document.getElementById("sd-upload-form");
    const sdSelectedName = document.getElementById("sd-selected-name");
    const sdUploadOpId = document.getElementById("sd-upload-op-id");
    const sdDeleteForm = document.getElementById("sd-delete-form");
    const sdDeleteSelectedName = document.getElementById("sd-delete-selected-name");
    const uploadOverlay = document.getElementById("upload-progress-overlay");
    const uploadProgressText = document.getElementById("upload-progress-text");
    const deleteOverlay = document.getElementById("delete-progress-overlay");
    const deleteProgressText = document.getElementById("delete-progress-text");
    const downloadForm = document.getElementById("uploaded-download-form");
    const deleteForm = document.getElementById("uploaded-delete-form");
    const sdUploadButton = document.getElementById("sd-upload-button");
    const sdDeleteButton = document.getElementById("sd-delete-button");
    const uploadedDownloadButton = document.getElementById("uploaded-download-button");
    const uploadedDeleteButton = document.getElementById("uploaded-delete-button");
    const selectedPath = document.getElementById("uploaded-selected-path");
    const selectedName = document.getElementById("uploaded-selected-name");
    const downloadPath = document.getElementById("uploaded-download-path");
    const downloadName = document.getElementById("uploaded-download-name");
    const pythonLogBox = document.getElementById("pythonlogbox");
    const filesOnTitle = document.getElementById("files-on-title");
    const filesUploadedTitle = document.getElementById("files-uploaded-title");
    let pythonLogTimer = null;
    function getSelectedUid() {{
      for (const f of uidFields) {{
        const v = (f.value || "").trim();
        if (v.length > 0) return v;
      }}
      return "";
    }}
    function getSelectedShortLabel(uid) {{
      const selectedUid = (uid || "").trim();
      for (const r of rows) {{
        if (r.dataset.uid === selectedUid) {{
          const shortLabel = (r.dataset.short || "").trim();
          return shortLabel || selectedUid;
        }}
      }}
      return selectedUid || "Arduino";
    }}
    function offlineSdMessage(uid) {{
      return getSelectedShortLabel(uid) + " is offline";
    }}
    function getSdRows() {{
      return Array.from(document.querySelectorAll("#sd-list-box .sd-row"));
    }}
    function getUploadRows() {{
      return Array.from(document.querySelectorAll("#uploaded-list-box .upload-row"));
    }}
    function getSelectedSdRows() {{
      return getSdRows().filter((r) => r.classList.contains("selected"));
    }}
    function getSelectedUploadRows() {{
      return getUploadRows().filter((r) => r.classList.contains("selected"));
    }}
    function appendGeneratedInputs(form, className, fieldName, values) {{
      if (!form) return;
      Array.from(form.querySelectorAll("." + className)).forEach((el) => el.remove());
      values.slice(1).forEach((value) => {{
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = fieldName;
        input.value = value;
        input.className = className;
        form.appendChild(input);
      }});
    }}
    function syncSdDeleteFields() {{
      const names = getSelectedSdRows().map((r) => r.dataset.name || "").filter((v) => v.trim().length > 0);
      if (sdSelectedName) sdSelectedName.value = names.length === 1 ? names[0] : "";
      if (sdDeleteSelectedName) sdDeleteSelectedName.value = names[0] || "";
      appendGeneratedInputs(sdDeleteForm, "generated-sd-delete-field", "remote_filename", names);
      return names;
    }}
    function syncUploadedDeleteFields() {{
      const selectedRows = getSelectedUploadRows();
      const paths = selectedRows.map((r) => r.dataset.path || "").filter((v) => v.trim().length > 0);
      const names = selectedRows.map((r) => r.dataset.name || "").filter((v) => v.trim().length > 0);
      if (selectedPath) selectedPath.value = paths[0] || "";
      if (selectedName) selectedName.value = names[0] || "";
      appendGeneratedInputs(deleteForm, "generated-uploaded-delete-path-field", "saved_path", paths);
      appendGeneratedInputs(deleteForm, "generated-uploaded-delete-name-field", "source_filename", names);
      return {{ paths, names }};
    }}
    function updateActionButtons() {{
      const hasUid = uidFields.some((f) => ((f.value || "").trim().length > 0));
      needsDeviceControls.forEach((el) => {{
        el.disabled = !hasUid;
      }});
      const sdSelectedCount = getSelectedSdRows().length;
      const uploadedSelectedCount = getSelectedUploadRows().length;
      if (sdUploadButton) sdUploadButton.disabled = !(hasUid && sdSelectedCount === 1);
      if (sdDeleteButton) {{
        sdDeleteButton.disabled = !(hasUid && sdSelectedCount > 0);
        sdDeleteButton.textContent = sdSelectedCount > 1 ? "Delete on SD (" + sdSelectedCount + ")" : "Delete on SD";
      }}
      if (uploadedDownloadButton) uploadedDownloadButton.disabled = !(hasUid && uploadedSelectedCount === 1);
      if (uploadedDeleteButton) {{
        uploadedDeleteButton.disabled = !(hasUid && uploadedSelectedCount > 0);
        uploadedDeleteButton.textContent = uploadedSelectedCount > 1 ? "Delete (" + uploadedSelectedCount + ")" : "Delete";
      }}
    }}
    function setSelectedUid(uid, triggerLoad = true) {{
      let selectedShort = "...";
      uidFields.forEach((f) => {{ f.value = uid; }});
      rows.forEach((r) => {{
        if (r.dataset.uid === uid) {{
          r.classList.add("selected");
          selectedShort = (r.dataset.short || "").trim() || "...";
        }} else r.classList.remove("selected");
      }});
      if (filesOnTitle) filesOnTitle.textContent = "Arduino SD files on " + selectedShort;
      if (filesUploadedTitle) filesUploadedTitle.textContent = "Gateway files saved for " + selectedShort;
      if (sdSelectedName) sdSelectedName.value = "";
      if (sdDeleteSelectedName) sdDeleteSelectedName.value = "";
      if (selectedPath) selectedPath.value = "";
      if (selectedName) selectedName.value = "";
      if (downloadPath) downloadPath.value = "";
      if (downloadName) downloadName.value = "";
      appendGeneratedInputs(sdDeleteForm, "generated-sd-delete-field", "remote_filename", []);
      appendGeneratedInputs(deleteForm, "generated-uploaded-delete-path-field", "saved_path", []);
      appendGeneratedInputs(deleteForm, "generated-uploaded-delete-name-field", "source_filename", []);
      updateActionButtons();
      if (triggerLoad) {{
        loadAllFilePanels(uid);
      }}
    }}
    rows.forEach((r) => {{
      r.addEventListener("click", () => setSelectedUid(r.dataset.uid || "", true));
    }});

    function setSelectedSdRow(row) {{
      if (!row) {{
        getSdRows().forEach((r) => r.classList.remove("selected"));
        if (sdSelectedName) sdSelectedName.value = "";
        if (sdDeleteSelectedName) sdDeleteSelectedName.value = "";
        appendGeneratedInputs(sdDeleteForm, "generated-sd-delete-field", "remote_filename", []);
        updateActionButtons();
        return;
      }}
      row.classList.toggle("selected");
      syncSdDeleteFields();
      updateActionButtons();
    }}
    function bindSdRows() {{
      getSdRows().forEach((r) => {{
        r.addEventListener("click", () => setSelectedSdRow(r));
      }});
    }}
    function setSelectedUploadRow(row) {{
      if (!row) {{
        getUploadRows().forEach((r) => r.classList.remove("selected"));
        if (selectedPath) selectedPath.value = "";
        if (selectedName) selectedName.value = "";
        if (downloadPath) downloadPath.value = "";
        if (downloadName) downloadName.value = "";
        appendGeneratedInputs(deleteForm, "generated-uploaded-delete-path-field", "saved_path", []);
        appendGeneratedInputs(deleteForm, "generated-uploaded-delete-name-field", "source_filename", []);
        updateActionButtons();
        return;
      }}
      row.classList.toggle("selected");
      const selectedRows = getSelectedUploadRows();
      const firstRow = selectedRows.length === 1 ? selectedRows[0] : null;
      syncUploadedDeleteFields();
      if (downloadPath) downloadPath.value = firstRow ? (firstRow.dataset.path || "") : "";
      if (downloadName) downloadName.value = firstRow ? (firstRow.dataset.name || "") : "";
      updateActionButtons();
    }}
    function bindUploadedRows() {{
      getUploadRows().forEach((r) => {{
        r.addEventListener("click", () => setSelectedUploadRow(r));
      }});
    }}
    async function loadRemoteFiles(uid) {{
      if (!sdListBox) return;
      if (!uid) {{
        sdListBox.textContent = "(Select a known Arduino to view SD files)";
        setSelectedSdRow(null);
        return;
      }}
      sdListBox.textContent = "Loading files from Arduino...";
      try {{
        const resp = await fetch("/api/file-transfers/remote-files?uid=" + encodeURIComponent(uid), {{ cache: "no-store" }});
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {{
          sdListBox.textContent = offlineSdMessage(uid);
          setSelectedSdRow(null);
          return;
        }}
        if (payload.html && payload.html.length > 0) {{
          sdListBox.innerHTML = payload.html;
        }} else {{
          sdListBox.textContent = payload.note || "(No files reported by Arduino)";
        }}
      }} catch (_err) {{
        sdListBox.textContent = offlineSdMessage(uid);
      }}
      setSelectedSdRow(null);
      bindSdRows();
      updateActionButtons();
    }}
    async function loadUploadedFiles(uid) {{
      if (!uploadedListBox) return;
      if (!uid) {{
        uploadedListBox.textContent = "(Select a known Arduino to view Gateway-saved files)";
        setSelectedUploadRow(null);
        return;
      }}
      uploadedListBox.textContent = "Loading Gateway-saved file list...";
      try {{
        const resp = await fetch("/api/file-transfers/uploaded-files?uid=" + encodeURIComponent(uid), {{ cache: "no-store" }});
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {{
          uploadedListBox.textContent = payload && payload.message ? payload.message : "(Could not load Gateway-saved files.)";
          setSelectedUploadRow(null);
          return;
        }}
        if (payload.html && payload.html.length > 0) {{
          uploadedListBox.innerHTML = payload.html;
        }} else {{
          uploadedListBox.textContent = payload.note || "(No Gateway-saved files found for this Arduino)";
        }}
      }} catch (_err) {{
        uploadedListBox.textContent = "(Could not load Gateway-saved files.)";
      }}
      setSelectedUploadRow(null);
      bindUploadedRows();
      updateActionButtons();
    }}
    async function loadHistory(uid) {{
      if (!historyBox) return;
      if (!uid) {{
        historyBox.textContent = "(Select a known Arduino to view complete DB history)";
        return;
      }}
      historyBox.textContent = "Loading SQLite history...";
      try {{
        const resp = await fetch("/api/file-transfers/history?uid=" + encodeURIComponent(uid), {{ cache: "no-store" }});
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {{
          historyBox.textContent = payload && payload.message ? payload.message : "(Could not load history.)";
          return;
        }}
        historyBox.textContent = payload.text || "(No DB history for this Arduino)";
      }} catch (_err) {{
        historyBox.textContent = "(Could not load history.)";
      }}
    }}
    async function loadAllFilePanels(uid) {{
      const selectedUid = (uid || "").trim();
      const localFilesPromise = loadUploadedFiles(selectedUid);
      await Promise.allSettled([
        localFilesPromise,
        loadRemoteFiles(selectedUid),
        loadHistory(selectedUid),
      ]);
    }}
    if (uploadForm) {{
      uploadForm.addEventListener("submit", (ev) => {{
        ev.preventDefault();
        const name = (sdSelectedName && sdSelectedName.value) ? sdSelectedName.value : "selected file";
        const ok = window.confirm("Upload selected SD file '" + name + "' now?");
        if (!ok) return;
        const opId = "op_" + Date.now().toString() + "_" + Math.floor(Math.random() * 100000).toString();
        if (sdUploadOpId) sdUploadOpId.value = opId;
        if (uploadOverlay) uploadOverlay.style.display = "flex";
        if (uploadProgressText) uploadProgressText.textContent = "Uploading selected file... 0% ... please wait.";

        let progressTimer = null;
        const pollProgress = () => {{
          fetch("/upload-progress?op=" + encodeURIComponent(opId), {{ cache: "no-store" }})
            .then((resp) => resp.json())
            .then((state) => {{
              if (!state) return;
              const pct = Number.isFinite(state.pct) ? state.pct : 0;
              if (uploadProgressText) {{
                if (state.ok) {{
                  uploadProgressText.textContent = "Uploading selected file - " + pct + "% - please wait.";
                }} else {{
                  uploadProgressText.textContent = "Uploading selected file... preparing transfer... please wait.";
                }}
              }}
              if (state.done && progressTimer) {{
                clearInterval(progressTimer);
                progressTimer = null;
              }}
            }})
            .catch((_err) => {{
              // Ignore transient poll errors.
            }});
        }};
        progressTimer = setInterval(pollProgress, {UI_POLL_UPLOAD_PROGRESS_MS});
        pollProgress();

        const params = new URLSearchParams(new FormData(uploadForm));
        fetch(uploadForm.action, {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" }},
          body: params.toString(),
          cache: "no-store",
        }})
          .then((resp) => resp.text())
          .then((htmlText) => {{
            if (progressTimer) {{
              clearInterval(progressTimer);
              progressTimer = null;
            }}
            document.open();
            document.write(htmlText);
            document.close();
          }})
          .catch((_err) => {{
            if (progressTimer) {{
              clearInterval(progressTimer);
              progressTimer = null;
            }}
            if (uploadOverlay) uploadOverlay.style.display = "none";
            window.alert("Upload request failed before completion. Check activity log.");
        }});
      }});
    }}
    if (sdDeleteForm) {{
      sdDeleteForm.addEventListener("submit", (ev) => {{
        ev.preventDefault();
        const names = syncSdDeleteFields();
        if (names.length < 1) {{
          window.alert("Select at least one SD file to delete.");
          return;
        }}
        const label = names.length === 1 ? "'" + names[0] + "'" : names.length + " selected SD files";
        const ok1 = window.confirm("Delete " + label + " now?");
        if (!ok1) {{
          return;
        }}
        const uploadedNames = new Set(
          getUploadRows()
            .map((r) => (r.dataset.name || "").trim().toUpperCase())
            .filter((v) => v.length > 0)
        );
        const missingUploads = names.filter((name) => !uploadedNames.has(name.trim().toUpperCase()));
        if (missingUploads.length > 0) {{
          const missingLabel = missingUploads.length === 1 ? "'" + missingUploads[0] + "'" : missingUploads.length + " selected files";
          const ok2 = window.confirm(missingLabel + " not uploaded. Proceed anyway?");
          if (!ok2) {{
            return;
          }}
        }}
        if (deleteOverlay) deleteOverlay.style.display = "flex";
        if (deleteProgressText) {{
          deleteProgressText.textContent = names.length === 1 ? "Deleting " + names[0] + ". Please wait." : "Deleting " + names.length + " files. Please wait.";
        }}
        const params = new URLSearchParams(new FormData(sdDeleteForm));
        fetch(sdDeleteForm.action, {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" }},
          body: params.toString(),
          cache: "no-store",
        }})
          .then((resp) => resp.text())
          .then((htmlText) => {{
            document.open();
            document.write(htmlText);
            document.close();
          }})
          .catch((_err) => {{
            if (deleteOverlay) deleteOverlay.style.display = "none";
            window.alert("Delete request failed before completion. Check activity log.");
          }});
      }});
    }}
    if (deleteForm) {{
      deleteForm.addEventListener("submit", (ev) => {{
        ev.preventDefault();
        const selected = syncUploadedDeleteFields();
        if (selected.paths.length < 1) {{
          window.alert("Select at least one uploaded file to delete.");
          return;
        }}
        const label = selected.names.length === 1 ? "'" + selected.names[0] + "'" : selected.paths.length + " selected uploaded files";
        const ok = window.confirm("Delete local uploaded file " + label + "?");
        if (!ok) return;
        if (deleteOverlay) deleteOverlay.style.display = "flex";
        if (deleteProgressText) {{
          deleteProgressText.textContent = selected.paths.length === 1 ? "Deleting " + (selected.names[0] || "selected file") + ". Please wait." : "Deleting " + selected.paths.length + " files. Please wait.";
        }}
        const params = new URLSearchParams(new FormData(deleteForm));
        fetch(deleteForm.action, {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" }},
          body: params.toString(),
          cache: "no-store",
        }})
          .then((resp) => resp.text())
          .then((htmlText) => {{
            document.open();
            document.write(htmlText);
            document.close();
          }})
          .catch((_err) => {{
            if (deleteOverlay) deleteOverlay.style.display = "none";
            window.alert("Delete request failed before completion. Check activity log.");
          }});
      }});
    }}
    if (downloadForm) {{
      downloadForm.addEventListener("submit", (ev) => {{
        const name = (downloadName && downloadName.value) ? downloadName.value : "selected file";
        const ok = window.confirm("Download selected Gateway file '" + name + "' to this laptop?");
        if (!ok) {{
          ev.preventDefault();
        }}
      }});
    }}
    async function refreshPythonLog() {{
      if (!pythonLogBox) return;
      try {{
        const resp = await fetch("/python-log", {{ cache: "no-store" }});
        if (!resp.ok) return;
        const txt = await resp.text();
        const nearBottom = (pythonLogBox.scrollTop + pythonLogBox.clientHeight) >= (pythonLogBox.scrollHeight - 30);
        pythonLogBox.textContent = txt || "(No python log output yet)";
        if (nearBottom) {{
          pythonLogBox.scrollTop = pythonLogBox.scrollHeight;
        }}
      }} catch (_err) {{
        // Keep last displayed text.
      }}
    }}
    function stopPythonLogPolling() {{
      if (pythonLogTimer) {{
        clearInterval(pythonLogTimer);
        pythonLogTimer = null;
      }}
    }}
    function startPythonLogPolling() {{
      if (pythonLogTimer) {{
        return;
      }}
      pythonLogTimer = setInterval(refreshPythonLog, {UI_POLL_PYTHON_LOG_MS});
    }}
    document.addEventListener("visibilitychange", () => {{
      if (document.hidden) {{
        stopPythonLogPolling();
        return;
      }}
      refreshPythonLog();
      startPythonLogPolling();
    }});
    refreshPythonLog();
    if (!document.hidden) {{
      startPythonLogPolling();
    }}
    const initialUid = getSelectedUid();
    if (initialUid) {{
      loadAllFilePanels(initialUid);
    }}
    bindSdRows();
    bindUploadedRows();
    updateActionButtons();
  }})();
"""
    return _render_shared_page(
        ctx=ctx,
        body_html=body_html,
        web_app_header=WEB_APP_HEADER,
        active_network_profile=ACTIVE_NETWORK_PROFILE,
        active_network_profile_source=ACTIVE_NETWORK_PROFILE_SOURCE,
        extra_css=extra_css,
        script_js=script_js,
    )


def _find_device_by_uid(devices: list[dict[str, str]], selected_uid: str) -> dict[str, str] | None:
    """Find the selected device row by unique UID."""
    for d in devices:
        if (d.get("unique_id", "") or "").strip() == selected_uid:
            return d
    return None


def _format_ip_for_table(ip: str) -> str:
    """Format IPv4 text so last octet is always width 3 for table alignment."""
    txt = (ip or "").strip()
    if not txt:
        return ""
    if "." not in txt:
        return txt
    head, tail = txt.rsplit(".", 1)
    if not tail.isdigit():
        return txt
    if len(tail) >= 3:
        return txt
    return f"{head}.{tail.ljust(3)}"


def _query_device_rtc_display(device_ip: str, timeout_s: float = 0.35) -> tuple[str, str]:
    """Fetch short RTC display strings for device table (date,time or status)."""
    ip = (device_ip or "").strip()
    if not ip:
        return "-", "-"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_TIME", (ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        if src_ip != ip:
            return "src_mismatch", "src_mismatch"
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("TIME,"):
            parts = line.split(",", 2)
            ts = parts[2].strip() if len(parts) > 2 else ""
            if ts and "T" in ts and len(ts) >= 19:
                # Example: 2026-05-12T10:44:12 -> (2026-05-12, 10:44:12)
                return ts[:10], ts[11:19]
            if ts:
                return ts[:10] if len(ts) >= 10 else "date_ok", "time_ok"
            return "date_ok", "time_ok"
        return "time_err", "time_err"
    except socket.timeout:
        return "timeout", "timeout"
    except Exception:
        return "query_err", "query_err"
    finally:
        sock.close()


def _get_cached_device_rtc_display(unique_id: str, device_ip: str, status: str) -> tuple[str, str]:
    """Return cached RTC table values with short TTL to avoid hammering devices."""
    uid = (unique_id or "").strip()
    ip = (device_ip or "").strip()
    if not ip:
        return "-", "-"
    # Only query if device appears online/upload-active.
    st = (status or "").strip().lower()
    if st not in {"online", "upload"}:
        return "-", "-"
    key = f"{uid}|{ip}"
    now = time.monotonic()
    with DEVICE_RTC_CACHE_LOCK:
        cached = DEVICE_RTC_CACHE.get(key)
        if cached and (now - cached[0]) <= DEVICE_RTC_CACHE_TTL_S:
            return cached[1], cached[2]
    rtc_date, rtc_time = _query_device_rtc_display(ip)
    with DEVICE_RTC_CACHE_LOCK:
        DEVICE_RTC_CACHE[key] = (now, rtc_date, rtc_time)
    return rtc_date, rtc_time


def _build_device_select_rows(devices: list[dict[str, str]], selected_uid: str) -> str:
    """Build device select rows."""
    rows: list[tuple[str, str, str]] = []
    mismatches: list[str] = []
    for d in devices:
        uid = (d.get("unique_id", "") or "").strip()
        short_uid = (d.get("short_uid", "") or "").strip()
        if not short_uid:
            short_uid = uid[-6:] if len(uid) >= 6 else uid
        if str(d.get("short_uid_collision", "0")) in {"1", "true", "True"}:
            short_uid = f"{short_uid}*"
        burrow = (d.get("burrow_id", "") or "").strip() or "-"
        status = (d.get("status", "UNKNOWN") or "UNKNOWN").strip()
        if len(status) > 6:
            status = status[:6]
        ap_id = (d.get("ap_id", "") or "").strip()
        net_uid = (d.get("network_uid", "") or "").strip()
        fw_ver = (d.get("firmware_version", "") or "").strip()
        raw_recv_ip = (d.get("recv_ip", "") or "").strip()
        raw_device_ip = (d.get("device_ip", "") or "").strip()
        ip = _format_ip_for_table((raw_device_ip or raw_recv_ip).strip())
        recv_ip = _format_ip_for_table(raw_recv_ip)
        rtc_date, rtc_time = "-", "-"
        dev_ip_raw = (d.get("device_ip", "") or "").strip()
        if dev_ip_raw and raw_recv_ip and dev_ip_raw != raw_recv_ip:
            mismatches.append(short_uid if short_uid else uid)
        last_seen_raw = (d.get("last_seen", "") or "").strip()
        line = (
            f"{status:<6}   {burrow:<12}   {short_uid:<8}   {fw_ver:<6}   "
            f"{ap_id:<9}   {net_uid:<15}   {rtc_date:<10}   {rtc_time:<8}   "
            f"{ip:<14}   {recv_ip:<14}    {last_seen_raw:<19}   {uid:<36}"
        )
        rows.append((uid, short_uid, line))
    if not rows:
        return '<div style="font-style:italic;">No devices discovered yet.</div>'

    out = []
    if mismatches:
        out.append(
            '<div style="margin-bottom:4px;color:#8a3300;font-weight:700;">'
            + html.escape(f"Warning: device_ip != recv_ip for {len(mismatches)} device(s): {', '.join(mismatches)}")
            + "</div>"
        )
    out.append('<div class="device-head">status   burrow_id      short_uid  fw_ver   ap_id       network_uid       rtc_date     rtc_time   device_ip         recv_ip           last_seen             unique_id</div>')
    out.append('<div class="device-sep">------   ------------   --------   ------   ---------   ---------------   ----------   --------   --------------    --------------    -------------------   ------------------------------------</div>')
    for uid, short_uid, line in rows:
        selected_cls = " selected" if uid == selected_uid else ""
        out.append(
            f'<div class="device-row{selected_cls}" data-uid="{html.escape(uid)}" data-short="{html.escape(short_uid)}">{html.escape(line)}</div>'
        )
    return "".join(out)


def _format_hhmm_today(raw_ts: str) -> str:
    """Return HH:MM only when timestamp is today."""
    d = _iso_to_dt(raw_ts)
    if d is None or d.date() != dt.date.today():
        return ""
    return d.strftime("%H:%M")


def _format_gateway_uptime() -> str:
    """Return compact Gateway uptime text."""
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        seconds = int(float(raw))
    except Exception:
        return "unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _read_iphone_files_today(db_path: Path) -> list[dict[str, str]]:
    """Return successful Gateway file receipts for today, newest first."""
    if not db_path.exists():
        return []
    today = dt.date.today().isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT
                  COALESCE(d.short_uid, ''),
                  COALESCE(t.network_uid, ''),
                  COALESCE(t.source_filename, ''),
                  COALESCE(t.saved_path, ''),
                  COALESCE(t.event_ts, '')
                FROM transfer_events t
                LEFT JOIN devices d
                  ON d.unique_id = t.unique_id
                WHERE date(substr(t.event_ts, 1, 10)) = ?
                  AND lower(COALESCE(t.status, '')) = 'saved'
                ORDER BY t.event_ts DESC
                """,
                (today,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []

    out: list[dict[str, str]] = []
    for short_uid, network_uid, filename, saved_path, event_ts in rows:
        uid = str(short_uid or "").strip()
        if not uid:
            net = str(network_uid or "").strip()
            uid = net[-6:] if len(net) >= 6 else net
        size_mb = ""
        try:
            p = Path(str(saved_path or "")).expanduser()
            if p.exists() and p.is_file():
                size_mb = f"{(p.stat().st_size / (1024.0 * 1024.0)):.2f}"
        except Exception:
            size_mb = ""
        out.append(
            {
                "uid": uid,
                "time": _format_hhmm_today(str(event_ts)),
                "size_mb": size_mb,
                "filename": str(filename or ""),
            }
        )
    return out


def _iphone_device_status(row: dict[str, str]) -> tuple[str, str, str]:
    """Return status key, visible dot, and short label for iPhone device rows."""
    last_seen = _iso_to_dt(row.get("last_seen", ""))
    status = (row.get("status", "") or "").strip().lower()
    if status in {"online", "upload"}:
        return "online", "●", "online"
    if last_seen is not None and last_seen.date() == dt.date.today():
        return "today", "●", "seen today"
    return "missing", "●", "not seen today"


def _render_iphone_page(message: str = "") -> bytes:
    """Render compact field/iPhone status page."""
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    ctx = PageContext(page_title=f"{WEB_APP_NAME} - iPhone", state=state, message=message, subtitle="iPhone Field Status")
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    files_today = _read_iphone_files_today(Path(DEFAULT_DB_PATH))
    online_count = sum(1 for d in devices if (d.get("status", "") or "").strip().lower() in {"online", "upload"})
    total_count = len(devices)
    last_upload = files_today[0]["time"] if files_today else "--"
    uptime = _format_gateway_uptime()

    device_rows = []
    for d in sorted(devices, key=lambda r: ((r.get("burrow_id", "") or "zzzz"), (r.get("short_uid", "") or r.get("unique_id", "")))):
        uid = (d.get("short_uid", "") or "").strip()
        if not uid:
            unique_id = (d.get("unique_id", "") or "").strip()
            uid = unique_id[-6:] if len(unique_id) >= 6 else unique_id
        burrow = (d.get("burrow_id", "") or "-").strip()
        seen = _format_hhmm_today(d.get("last_seen", ""))
        status_key, dot, label = _iphone_device_status(d)
        device_rows.append(
            "<tr>"
            f"<td>{html.escape(burrow)}</td>"
            f"<td>{html.escape(uid)}</td>"
            f"<td>{html.escape(seen)}</td>"
            f'<td class="status-dot {status_key}" title="{html.escape(label)}">{dot}</td>'
            "</tr>"
        )
    if not device_rows:
        device_rows.append('<tr><td colspan="4" class="empty-row">No known Arduinos</td></tr>')

    file_rows = []
    for f in files_today:
        file_rows.append(
            "<tr>"
            f"<td>{html.escape(f['uid'])}</td>"
            f"<td>{html.escape(f['time'])}</td>"
            f"<td class=\"num\">{html.escape(f['size_mb'])}</td>"
            f"<td class=\"filename\">{html.escape(f['filename'])}</td>"
            "</tr>"
        )
    if not file_rows:
        file_rows.append('<tr><td colspan="4" class="empty-row">No files received today</td></tr>')

    extra_css = """
    body { background: #f8fafc; }
    .shell { max-width: 760px; margin: 0 auto; padding: 0.6rem; }
    .panel { padding: 0.75rem; box-shadow: none; border-radius: 6px; }
    .title { font-size: 1.15rem; }
    .subtitle, .status { font-size: 0.82rem; margin-bottom: 0.45rem; }
    .summary-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.45rem; margin: 0.6rem 0; }
    .summary-cell { border: 1px solid var(--line); border-radius: 6px; background: #fbfdff; padding: 0.45rem; }
    .summary-label { color: #475569; font-size: 0.75rem; font-weight: 700; }
    .summary-value { color: #0b2d4b; font-size: 1.1rem; font-weight: 800; margin-top: 0.1rem; }
    .iphone-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 0.45rem; margin: 0.6rem 0; }
    .iphone-actions button { width: 100%; padding: 0.7rem 0.4rem; font-weight: 700; }
    .section-title { margin: 0.9rem 0 0.35rem 0; }
    .iphone-table { width: 100%; border-collapse: collapse; font-size: 0.86rem; table-layout: fixed; }
    .iphone-table th, .iphone-table td { border-bottom: 1px solid #d8e1ea; padding: 0.42rem 0.25rem; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .iphone-table th { color: #304a64; font-size: 0.74rem; text-transform: uppercase; }
    .iphone-table .num { text-align: right; }
    .iphone-table .filename { width: 44%; }
    .status-dot { text-align: center; font-size: 1.15rem; line-height: 1; }
    .status-dot.online { color: #15803d; }
    .status-dot.today { color: #ca8a04; }
    .status-dot.missing { color: #dc2626; }
    .empty-row { color: #64748b; font-style: italic; text-align: center; }
    """
    body_html = f"""
      <div class="summary-grid">
        <div class="summary-cell"><div class="summary-label">Arduinos Online</div><div class="summary-value">{online_count} / {total_count}</div></div>
        <div class="summary-cell"><div class="summary-label">Files Today</div><div class="summary-value">{len(files_today)}</div></div>
        <div class="summary-cell"><div class="summary-label">Last Upload</div><div class="summary-value">{html.escape(last_upload)}</div></div>
        <div class="summary-cell"><div class="summary-label">Gateway Uptime</div><div class="summary-value">{html.escape(uptime)}</div></div>
      </div>
      <div class="iphone-actions">
        <form method="get" action="/"><button type="submit">Dashboard</button></form>
        <form method="post" action="/iphone-poll"><button type="submit">Poll</button></form>
      </div>

      {_render_section_title("Known Arduinos")}
      <table class="iphone-table">
        <thead><tr><th>Burr</th><th>UID</th><th>Seen</th><th>Status</th></tr></thead>
        <tbody>{"".join(device_rows)}</tbody>
      </table>

      {_render_section_title("Files Received Today")}
      <table class="iphone-table">
        <thead><tr><th>UID</th><th>Time</th><th>MB</th><th class="filename">Filename</th></tr></thead>
        <tbody>{"".join(file_rows)}</tbody>
      </table>
"""
    return _render_shared_page(
        ctx=ctx,
        body_html=body_html,
        web_app_header=WEB_APP_HEADER,
        active_network_profile=ACTIVE_NETWORK_PROFILE,
        active_network_profile_source=ACTIVE_NETWORK_PROFILE_SOURCE,
        extra_css=extra_css,
    )


def _maintenance_info_lines(device_ip: str) -> list[str]:
    """Collect maintenance command output lines for one device IP."""
    status = query_device_status(device_ip=device_ip)
    config = query_device_config(device_ip=device_ip)
    diag = query_device_diagnostics(device_ip=device_ip)
    rtc = query_device_time(device_ip=device_ip)
    lines = []
    lines.append("Maintenance Info     | Value")
    lines.append("-------------------- | -------------------------------------------------------------")
    lines.append(f"Get Status           | {status}")
    lines.append(f"Get Config           | {config}")
    lines.append(f"Get Diagnostics      | {diag}")
    lines.append(f"Get RTC Time         | {rtc}")
    return lines


MAINTENANCE_PANEL_TIMEOUT_S = 5.0
MAINTENANCE_PANEL_GAP_S = 0.15


def _rtc_panel_lines(device_ip: str, timeout_s: float = MAINTENANCE_PANEL_TIMEOUT_S) -> tuple[str, str, str]:
    """Build two-line RTC panel text (header/separator/value)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_TIME", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        line = data.decode("utf-8", errors="replace").strip()
        if src_ip != device_ip:
            return "result", "------", f"unexpected_source={src_ip}"
        if line.startswith("TIME,"):
            parts = line.split(",", 2)
            epoch = parts[1] if len(parts) > 1 else ""
            ts = parts[2] if len(parts) > 2 else ""
            return _format_two_line_columns([("epoch", epoch), ("timestamp", ts)])
        return "result", "------", line
    except socket.timeout:
        return "result", "------", f"timeout after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return "result", "------", f"error: {exc}"
    finally:
        sock.close()


def _dict_panel_lines(
    fetch_fn,
    device_ip: str,
    fallback_order: list[str],
    timeout_s: float = MAINTENANCE_PANEL_TIMEOUT_S,
) -> tuple[str, str, str]:
    """Query a maintenance payload and render fixed-width header/value rows."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        data = fetch_fn(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if not data:
            return "result", "------", "no data"
        keys = [k for k in fallback_order if k in data] + [k for k in data.keys() if k not in fallback_order]
        cols = [(k, str(data.get(k, ""))) for k in keys]
        return _format_two_line_columns(cols) if cols else ("result", "------", "ok")
    except TimeoutError:
        return "result", "------", f"timeout after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return "result", "------", f"error: {exc}"
    finally:
        sock.close()


def _maintenance_panel_data(device_ip: str) -> dict[str, tuple[str, str, str]]:
    """Fetch all maintenance mini-panel payloads for one device."""
    rtc = _rtc_panel_lines(device_ip=device_ip)
    time.sleep(MAINTENANCE_PANEL_GAP_S)
    status = _dict_panel_lines(
        fetch_fn=protocol_get_device_status,
        device_ip=device_ip,
        fallback_order=["UPTIME", "MODE", "WIFI_POLICY", "WIFI_SLEEPING", "SD_FREE_KB", "LAST_DATA_TS", "BATTERY"],
        timeout_s=MAINTENANCE_PANEL_TIMEOUT_S,
    )
    time.sleep(MAINTENANCE_PANEL_GAP_S)
    config = _dict_panel_lines(
        fetch_fn=protocol_get_device_config,
        device_ip=device_ip,
        fallback_order=["START_HOUR", "END_HOUR", "WIFI_POLICY", "GRACE_MIN", "WAKE_HOUR", "DEVICE_ID"],
        timeout_s=MAINTENANCE_PANEL_TIMEOUT_S,
    )
    time.sleep(MAINTENANCE_PANEL_GAP_S)
    diagnostics = _dict_panel_lines(
        fetch_fn=protocol_get_device_diagnostics,
        device_ip=device_ip,
        fallback_order=[
            "RTC_OK",
            "RTC_TIME",
            "CACHE_TIME",
            "CACHE_SOURCE",
            "CACHE_AGE_SEC",
            "CACHE_RTC_DELTA_SEC",
            "WIFI_POLICY",
            "WIFI_LAST_ACTIVITY",
            "WIFI_NEXT_PROBE",
            "RTC_ERRORS",
            "I2C_ERRORS",
            "SD_ERRORS",
        ],
        timeout_s=MAINTENANCE_PANEL_TIMEOUT_S,
    )
    return {
        "RTC Time": rtc,
        "Status": status,
        "Config": config,
        "Diagnostics": diagnostics,
    }


def _format_two_line_columns(cols: list[tuple[str, str]]) -> tuple[str, str, str]:
    """Format two line columns."""
    if not cols:
        return "result", "------", ""
    # Add one trailing space to every column width so columns are separated by one space.
    widths = [max(len(h), len(v)) + 1 for h, v in cols]
    header = "".join(h.ljust(widths[i]) for i, (h, _v) in enumerate(cols)).rstrip()
    # Per user spec: dashes per column use (column width - 1), preserving an inter-column space.
    separator = "".join((("-" * max(1, widths[i] - 1)).ljust(widths[i])) for i in range(len(cols))).rstrip()
    values = "".join(v.ljust(widths[i]) for i, (_h, v) in enumerate(cols)).rstrip()
    return header, separator, values


def _mini_panel_block(header: str, separator: str, values: str) -> str:
    """Render one maintenance mini-panel block."""
    # Build a dashboard-style 3-line block:
    # header row
    # dashed separator row
    # value row
    return f"{header}\n{separator}\n{values}"


def render_maintenance_page(message: str = "", selected_uid: str = "", burrow_input: str | None = None) -> bytes:
    """Render maintenance page."""
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    ctx = PageContext(page_title=f"{WEB_APP_NAME} - Maintenance", state=state, message=message, subtitle="Maintenance")
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    selected_uid = (selected_uid or "").strip()
    selected_device = _find_device_by_uid(devices, selected_uid)
    selected_burrow = (burrow_input if burrow_input is not None else "").strip()
    _last_offset_hours, last_preset = get_last_set_time_state()
    tz_selected = (last_preset or "ast").strip().lower()
    if tz_selected not in TZ_PRESET_OFFSETS:
        tz_selected = "ast"

    panel_placeholders: dict[str, str] = {
        "RTC Time": "select a known Arduino",
        "Status": "select a known Arduino",
        "Config": "select a known Arduino",
        "Diagnostics": "select a known Arduino",
    }
    selected_short = "..."
    if selected_device is not None:
        uid = (selected_device.get("unique_id", "") or "").strip()
        selected_short = (selected_device.get("short_uid", "") or "").strip()
        if not selected_short:
            selected_short = uid[-6:] if len(uid) >= 6 else uid
        if burrow_input is None:
            selected_burrow = (selected_device.get("burrow_id", "") or "").strip()
        panel_placeholders = {
            "RTC Time": "loading...",
            "Status": "loading...",
            "Config": "loading...",
            "Diagnostics": "loading...",
        }
    device_rows_html_block = _build_device_select_rows(devices, selected_uid)

    tz_options = [
        ("ast", "AST (UTC-4)"),
        ("adt", "ADT (UTC-3)"),
        ("est", "EST (UTC-5)"),
        ("edt", "EDT (UTC-4)"),
    ]
    tz_options_html = "\n".join(
        f'<option value="{html.escape(value)}"{" selected" if tz_selected == value else ""}>{html.escape(label)}</option>'
        for value, label in tz_options
    )
    wifi_policy = load_wifi_policy(DEFAULT_WIFI_POLICY_PATH)
    wifi_policy = normalize_wifi_policy(wifi_policy)
    policy_mode = str(wifi_policy["policy"])
    policy_grace_min = str(wifi_policy["grace_min"])
    policy_wake_hour = str(wifi_policy["wake_hour"])
    policy_options = [
        (POLICY_STAY_ACTIVE, "Stay active"),
        (POLICY_MORNING_ONLY, "Morning only"),
    ]
    policy_options_html = "\n".join(
        f'<option value="{html.escape(value)}"{" selected" if policy_mode == value else ""}>{html.escape(label)}</option>'
        for value, label in policy_options
    )

    extra_css = """
    .device-head, .device-sep { white-space: pre; }
    .device-row { white-space: pre; cursor: pointer; border-radius: 4px; }
    .device-row:hover { background: #eef5ff; }
    .device-row.selected { background: #d7e9ff; font-weight: 700; }
    .mini-grid { margin-top: 0.9rem; display: grid; gap: 0.8rem; grid-template-columns: 1fr 1fr; }
    .mini-title { margin: 0 0 0.25rem 0; font-size: 0.9rem; color: #304a64; font-weight: 700; }
    .mini-box { border: 1px solid var(--line); background: #fbfdff; border-radius: 6px; height: 88px; overflow: auto; padding: 0.55rem; white-space: pre; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.84rem; line-height: 1.3; }
    .set-rtc-inline { display:flex; align-items:center; gap:0.45rem; flex-wrap:wrap; }
    .set-rtc-inline select { padding:0.45rem; border:1px solid #9cb2c9; border-radius:4px; background:#fff; color:#1f2937; font-size:0.92rem; }
    .policy-inline { display:flex; align-items:center; gap:0.45rem; flex-wrap:wrap; margin-top:0.7rem; }
    .policy-inline select, .policy-inline input { padding:0.45rem; border:1px solid #9cb2c9; border-radius:4px; background:#fff; color:#1f2937; font-size:0.92rem; }
    .policy-inline input { width:4.8rem; }
    @media (max-width: 900px) { .mini-grid { grid-template-columns: 1fr; } }
    """
    body_html = f"""
      {_render_controls_row([
          _render_action_form(action="/", label="Dashboard", method="get"),
      ])}

      {_render_known_arduinos_selector(
          action="/maintenance",
          selected_uid=selected_uid,
          device_rows_html_block=device_rows_html_block,
          button_label="Load Maintenance Info",
          show_button=False,
      )}
      <form method="post" action="/maintenance-burrow">
        <div class="controls" style="margin-top:0.5rem;">
          <input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />
          <label for="burrow_id_input">Burrow_ID:</label>
          <input id="burrow_id_input" name="burrow_id" type="text" maxlength="32" value="{html.escape(selected_burrow)}" class="needs-device" style="padding:0.45rem; border:1px solid #9cb2c9; border-radius:4px; width:10rem;" />
          <button type="submit" name="mode" value="edit" class="needs-device">Edit Burrow_ID</button>
          <button type="submit" name="mode" value="save" class="needs-device">Save Burrow_ID</button>
        </div>
      </form>

      <form method="post" action="/maintenance-wifi-policy" class="policy-inline">
        <input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />
        <label for="wifi_policy">WiFi Policy:</label>
        <select id="wifi_policy" name="policy">{policy_options_html}</select>
        <label for="policy_grace_min">Grace min:</label>
        <input id="policy_grace_min" name="grace_min" type="number" min="0" max="360" value="{html.escape(policy_grace_min)}" />
        <label for="policy_wake_hour">Wake hour:</label>
        <input id="policy_wake_hour" name="wake_hour" type="number" min="0" max="23" value="{html.escape(policy_wake_hour)}" />
        <button type="submit" name="mode" value="save">Save Gateway Policy</button>
        <button type="submit" name="mode" value="apply" class="needs-device">Apply to Selected</button>
      </form>

      {_render_controls_row([
          _render_action_form(
              action="/maintenance-action",
              label="Get RTC Time",
              method="post",
              hidden_fields=[("uid", selected_uid), ("action", "get-time")],
              hidden_input_class="selected-uid-field",
              hidden_class_names={"uid"},
              button_class="needs-device",
          ),
          (
              '<form method="post" action="/maintenance-action" class="set-rtc-inline">'
              f'<input type="hidden" name="uid" value="{html.escape(selected_uid)}" class="selected-uid-field" />'
              '<input type="hidden" name="action" value="set-time" />'
              '<label for="tz_preset">TZ:</label>'
              f'<select id="tz_preset" name="tz_preset" class="needs-device">{tz_options_html}</select>'
              '<button type="submit" class="needs-device">Set RTC Time</button>'
              '</form>'
          ),
          _render_action_form(
              action="/maintenance-action",
              label="Ping",
              method="post",
              hidden_fields=[("uid", selected_uid), ("action", "ping")],
              hidden_input_class="selected-uid-field",
              hidden_class_names={"uid"},
              button_class="needs-device",
          ),
          _render_action_form(
              action="/maintenance-action",
              label="Reboot",
              method="post",
              hidden_fields=[("uid", selected_uid), ("action", "reboot")],
              hidden_input_class="selected-uid-field",
              hidden_class_names={"uid"},
              button_class="needs-device",
          ),
      ], extra_style="margin-top:0.8rem;")}

      <div class="section-title" id="maintenance-info-title">{html.escape(f"Maintenance Info for {selected_short}")}</div>
      <div class="mini-grid">
        <div>
          <div class="mini-title">RTC Time</div>
          <div class="mini-box" id="panel-rtc">{html.escape(panel_placeholders["RTC Time"])}</div>
        </div>
        <div>
          <div class="mini-title">Status</div>
          <div class="mini-box" id="panel-status">{html.escape(panel_placeholders["Status"])}</div>
        </div>
        <div>
          <div class="mini-title">Config</div>
          <div class="mini-box" id="panel-config">{html.escape(panel_placeholders["Config"])}</div>
        </div>
        <div>
          <div class="mini-title">Diagnostics</div>
          <div class="mini-box" id="panel-diagnostics">{html.escape(panel_placeholders["Diagnostics"])}</div>
        </div>
      </div>
"""
    script_js = f"""
  (function() {{
    const rows = Array.from(document.querySelectorAll(".device-row"));
    const uidFields = Array.from(document.querySelectorAll(".selected-uid-field"));
    const needsDeviceControls = Array.from(document.querySelectorAll(".needs-device"));
    const panelRtc = document.getElementById("panel-rtc");
    const panelStatus = document.getElementById("panel-status");
    const panelConfig = document.getElementById("panel-config");
    const panelDiagnostics = document.getElementById("panel-diagnostics");
    const maintenanceInfoTitle = document.getElementById("maintenance-info-title");
    function getSelectedUid() {{
      for (const f of uidFields) {{
        const v = (f.value || "").trim();
        if (v.length > 0) return v;
      }}
      return "";
    }}
    function setPanelText(target, text) {{
      if (!target) return;
      target.textContent = text || "";
    }}
    function setAllPanels(text) {{
      setPanelText(panelRtc, text);
      setPanelText(panelStatus, text);
      setPanelText(panelConfig, text);
      setPanelText(panelDiagnostics, text);
    }}
    async function loadMaintenancePanels(uid) {{
      const selectedUid = (uid || "").trim();
      if (!selectedUid) {{
        setAllPanels("select a known Arduino");
        return;
      }}
      setAllPanels("loading...");
      try {{
        const resp = await fetch("/api/maintenance/panels?uid=" + encodeURIComponent(selectedUid), {{ cache: "no-store" }});
        const payload = await resp.json();
        if (!resp.ok || !payload || !payload.ok) {{
          setAllPanels(payload && payload.message ? payload.message : "Could not load maintenance data.");
          return;
        }}
        const p = payload.panels || {{}};
        setPanelText(panelRtc, p["RTC Time"] || "no data");
        setPanelText(panelStatus, p["Status"] || "no data");
        setPanelText(panelConfig, p["Config"] || "no data");
        setPanelText(panelDiagnostics, p["Diagnostics"] || "no data");
      }} catch (_err) {{
        setAllPanels("Could not load maintenance data.");
      }}
    }}
    function updateNeedsDeviceState() {{
      const hasUid = uidFields.some((f) => ((f.value || "").trim().length > 0));
      needsDeviceControls.forEach((el) => {{
        el.disabled = !hasUid;
      }});
    }}
    function setSelectedUid(uid, triggerLoad = true) {{
      let selectedShort = "...";
      uidFields.forEach((f) => {{ f.value = uid; }});
      rows.forEach((r) => {{
        if (r.dataset.uid === uid) {{
          r.classList.add("selected");
          selectedShort = (r.dataset.short || "").trim() || "...";
        }} else r.classList.remove("selected");
      }});
      if (maintenanceInfoTitle) maintenanceInfoTitle.textContent = "Maintenance Info for " + selectedShort;
      updateNeedsDeviceState();
      if (triggerLoad) {{
        loadMaintenancePanels(uid);
      }}
    }}
    rows.forEach((r) => {{
      r.addEventListener("click", () => setSelectedUid(r.dataset.uid || "", true));
    }});
    const initialUid = getSelectedUid();
    if (initialUid) {{
      loadMaintenancePanels(initialUid);
    }}
    updateNeedsDeviceState();
  }})();
"""
    return _render_shared_page(
        ctx=ctx,
        body_html=body_html,
        web_app_header=WEB_APP_HEADER,
        active_network_profile=ACTIVE_NETWORK_PROFILE,
        active_network_profile_source=ACTIVE_NETWORK_PROFILE_SOURCE,
        extra_css=extra_css,
        script_js=script_js,
    )


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for dashboard, API, and action routes."""
    def _safe_write(self, raw: bytes) -> None:
        """Write response bytes while tolerating disconnected clients."""
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected before receiving full response.
            return

    def _send_html(self, body: bytes, code: int = HTTPStatus.OK) -> None:
        """Send an HTML response payload."""
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._safe_write(body)

    def _send_text(self, body: str, code: int = HTTPStatus.OK) -> None:
        """Send a plain-text response payload."""
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self._safe_write(raw)

    def _send_json(self, payload: dict, code: int = HTTPStatus.OK) -> None:
        """Send a JSON response payload."""
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self._safe_write(raw)

    def _handle_get_monitor_routes(self, route: str, query: dict[str, list[str]]) -> bool:
        """Serve monitor/status GET endpoints used by polling UI widgets."""
        if route == "/health":
            self._send_json(get_health_payload())
            return True
        if route == "/health-status":
            self._send_text(get_cached_text("health-status", ENDPOINT_CACHE_TTL_S, lambda: format_health_status_text(get_health_payload())))
            return True
        if route == "/devices":
            self._send_text(
                get_cached_text(
                    "devices",
                    ENDPOINT_CACHE_TTL_S,
                    lambda: read_devices_status(Path("data/discovered_devices.csv")),
                )
            )
            return True
        if route == "/uploads-today":
            self._send_text(
                get_cached_text(
                    "uploads-today",
                    ENDPOINT_CACHE_TTL_S,
                    lambda: read_today_uploads_status(Path(DEFAULT_DB_PATH)),
                )
            )
            return True
        if route == "/activity":
            self._send_text(
                get_cached_text(
                    "activity",
                    ENDPOINT_CACHE_TTL_S,
                    read_activity_status,
                )
            )
            return True
        if route == "/python-log":
            self._send_text(
                get_cached_text(
                    "python-log",
                    ENDPOINT_CACHE_TTL_S,
                    read_python_log_status,
                )
            )
            return True
        if route == "/upload-progress":
            op = (query.get("op") or [""])[0].strip()
            self._send_json(get_upload_progress(op))
            return True
        return False

    def _handle_get_api_routes(self, route: str, query: dict[str, list[str]]) -> bool:
        """Serve JSON data endpoints for File Transfers/Maintenance pages."""
        if route == "/api/devices-table":
            self._send_json(get_devices_table_payload())
            return True
        if route == "/api/rf-data/preview-local":
            selected_uid = (query.get("uid") or [""])[0].strip()
            saved_path = (query.get("saved_path") or [""])[0].strip()
            self._send_json(get_rf_data_local_preview_payload(selected_uid=selected_uid, saved_path=saved_path))
            return True
        if route == "/api/rf-data/preview-remote":
            selected_uid = (query.get("uid") or [""])[0].strip()
            remote_filename = (query.get("remote_filename") or [""])[0].strip()
            self._send_json(get_rf_data_remote_preview_payload(selected_uid=selected_uid, remote_filename=remote_filename))
            return True
        if route == "/api/batch-downloads/preview":
            date_raw = (query.get("date") or [""])[0].strip()
            kind = (query.get("kind") or [""])[0].strip().upper()
            self._send_json(get_batch_download_preview_payload(date_raw=date_raw, kind=kind))
            return True
        if route == "/api/file-transfers/remote-files":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_json(get_file_transfers_remote_files_payload(selected_uid))
            return True
        if route == "/api/file-transfers/uploaded-files":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_json(get_file_transfers_uploaded_files_payload(selected_uid))
            return True
        if route == "/api/file-transfers/history":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_json(get_file_transfers_history_payload(selected_uid))
            return True
        if route == "/api/maintenance/panels":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_json(get_maintenance_panels_payload(selected_uid))
            return True
        return False

    def _handle_get_page_routes(self, route: str, query: dict[str, list[str]]) -> bool:
        """Serve full HTML page routes."""
        if route == "/logs":
            self._send_text(read_log_tail(MANAGER._log_path))
            return True
        if route == "/batch-downloads":
            self._send_html(render_batch_downloads_page())
            return True
        if route == "/iphone":
            self._send_html(_render_iphone_page())
            return True
        if route == "/rf-data":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_html(render_rf_data_page(selected_uid=selected_uid))
            return True
        if route == "/batch-downloads-download":
            date_raw = (query.get("date") or [""])[0].strip()
            kind = (query.get("kind") or [""])[0].strip().upper()
            preview = get_batch_download_preview_payload(date_raw=date_raw, kind=kind)
            if not bool(preview.get("ok")):
                msg = f"Batch download failed: {preview.get('message', 'unknown error')}"
                append_action_log("batch-downloads-download", msg)
                self._send_html(render_batch_downloads_page(message=msg))
                return True
            target_date = str(preview.get("date", "")).strip()
            files, err = _iter_bundle_source_files(selected_uid="", kind=kind, target_date=dt.datetime.strptime(target_date, "%Y-%m-%d").date())
            if err:
                msg = f"Batch download failed: {err}"
                append_action_log("batch-downloads-download", msg)
                self._send_html(render_batch_downloads_page(message=msg))
                return True
            if not files:
                msg = f"No files found for {target_date} type={kind}."
                append_action_log("batch-downloads-download", msg)
                self._send_html(render_batch_downloads_page(message=msg))
                return True
            zip_name = f"gateway_files_{target_date}_{kind}_all.zip"
            with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b") as tmp:
                with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for p, arcname in files:
                        zf.write(p, arcname)
                tmp.seek(0)
                raw = tmp.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self._safe_write(raw)
            append_action_log(
                "batch-downloads-download",
                f"Downloaded bundle '{zip_name}' files={len(files)} filter={kind} date={target_date}",
            )
            return True
        if route == "/file-transfers-download-uploaded":
            selected_uid = (query.get("uid") or [""])[0].strip()
            saved_path = (query.get("saved_path") or [""])[0].strip()
            source_filename = (query.get("source_filename") or [""])[0].strip()
            target, err = resolve_local_uploaded_file_for_download(saved_path=saved_path, selected_uid=selected_uid)
            if target is None:
                msg = f"Download failed for '{source_filename or 'selected file'}': {err}"
                append_action_log("file-transfers-download-uploaded", msg)
                self._send_html(render_file_transfers_page(message=msg, selected_uid=selected_uid))
                return True
            content_type, _enc = mimetypes.guess_type(str(target))
            if not content_type:
                content_type = "application/octet-stream"
            dl_name = (source_filename or target.name).replace('"', "_")
            file_size = target.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{dl_name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            with target.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self._safe_write(chunk)
            append_action_log("file-transfers-download-uploaded", f"Downloaded '{target.name}' ({file_size} bytes).")
            return True
        if route == "/file-transfers":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_html(render_file_transfers_page(selected_uid=selected_uid))
            return True
        if route == "/maintenance":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_html(render_maintenance_page(selected_uid=selected_uid))
            return True
        if route == "/":
            self._send_html(render_page())
            return True
        return False

    def _handle_post_ops_routes(self, form: dict[str, list[str]]) -> bool:
        """Process POST controls for scheduler lifecycle and manual poll."""
        if self.path == "/start":
            msg = MANAGER.start()
            append_action_log("start", msg)
            self._send_html(render_page(msg))
            return True
        if self.path == "/stop":
            msg = MANAGER.stop()
            append_action_log("stop", msg)
            self._send_html(render_page(msg))
            return True
        if self.path == "/poll-now":
            msg = MANAGER.poll_now()
            append_action_log("poll-now", msg)
            self._send_html(render_page(msg))
            return True
        if self.path == "/iphone-poll":
            msg = MANAGER.poll_now()
            append_action_log("iphone-poll", msg)
            self._send_html(_render_iphone_page(msg))
            return True
        if self.path == "/file-transfers-stop-safe":
            selected_uid = (form.get("uid") or [""])[0].strip()
            msg = MANAGER.stop()
            append_action_log("stop-for-file-transfers", msg)
            self._send_html(render_file_transfers_page(message=msg, selected_uid=selected_uid))
            return True
        return False

    def _handle_post_file_transfer_routes(self, form: dict[str, list[str]]) -> bool:
        """Process POST actions for upload/delete file operations."""
        if self.path == "/file-transfers-delete-uploaded":
            selected_uid = (form.get("uid") or [""])[0].strip()
            saved_paths = [v.strip() for v in form.get("saved_path", []) if v.strip()]
            source_filenames = [v.strip() for v in form.get("source_filename", []) if v.strip()]
            if not saved_paths:
                self._send_html(render_file_transfers_page(message="Select an uploaded file first.", selected_uid=selected_uid))
                return True

            results: list[tuple[bool, str, str]] = []
            for idx, saved_path in enumerate(saved_paths):
                source_filename = source_filenames[idx] if idx < len(source_filenames) else Path(saved_path).name
                ok, detail = delete_local_uploaded_file(saved_path)
                results.append((ok, source_filename, detail))

            ok_count = sum(1 for ok, _name, _detail in results if ok)
            fail_rows = [(name, detail) for ok, name, detail in results if not ok]
            if len(results) == 1:
                ok, source_filename, detail = results[0]
                if ok:
                    msg = f"Deleted uploaded file '{source_filename}'. {detail}"
                else:
                    msg = f"Delete failed for '{source_filename}': {detail}"
            elif not fail_rows:
                msg = f"Deleted {ok_count} uploaded file(s)."
            else:
                sample = "; ".join(f"{name}: {detail}" for name, detail in fail_rows[:3])
                more = "" if len(fail_rows) <= 3 else f"; +{len(fail_rows) - 3} more"
                msg = f"Deleted {ok_count} uploaded file(s); failed {len(fail_rows)}. {sample}{more}"
            append_action_log("file-transfers-delete-uploaded", msg)
            self._send_html(render_file_transfers_page(message=msg, selected_uid=selected_uid))
            return True
        if self.path == "/file-transfers-delete-sd":
            selected_uid = (form.get("uid") or [""])[0].strip()
            remote_filenames = []
            seen_remote_filenames = set()
            for value in form.get("remote_filename", []):
                remote_filename = value.strip()
                if not remote_filename or remote_filename in seen_remote_filenames:
                    continue
                seen_remote_filenames.add(remote_filename)
                remote_filenames.append(remote_filename)
            devices = read_devices_rows(Path("data/discovered_devices.csv"))
            selected_device = _find_device_by_uid(devices, selected_uid)
            if selected_device is None:
                self._send_html(render_file_transfers_page(message="Select a known Arduino first.", selected_uid=selected_uid))
                return True
            device_ip = (selected_device.get("device_ip", "") or selected_device.get("recv_ip", "")).strip()
            if not device_ip:
                self._send_html(render_file_transfers_page(message="Selected Arduino has no IP address.", selected_uid=selected_uid))
                return True
            if not remote_filenames:
                self._send_html(render_file_transfers_page(message="Select a file from SD list first.", selected_uid=selected_uid))
                return True
            ok_gate, reason = can_web_access_file_transfers(selected_uid, "Delete on SD")
            if not ok_gate:
                append_action_log("file-transfers-delete-sd", reason)
                self._send_html(render_file_transfers_page(message=reason, selected_uid=selected_uid))
                return True

            results: list[tuple[bool, str, str]] = []
            for remote_filename in remote_filenames:
                ok, detail = delete_remote_file(device_ip=device_ip, remote_filename=remote_filename, timeout_s=8.0)
                results.append((ok, remote_filename, detail))

            ok_count = sum(1 for ok, _name, _detail in results if ok)
            fail_rows = [(name, detail) for ok, name, detail in results if not ok]
            if len(results) == 1:
                ok, remote_filename, detail = results[0]
                if ok:
                    msg = detail
                else:
                    msg = f"Delete on SD failed for '{remote_filename}': {detail}"
            elif not fail_rows:
                msg = f"Deleted {ok_count} SD file(s) on {device_ip}."
            else:
                sample = "; ".join(f"{name}: {detail}" for name, detail in fail_rows[:3])
                more = "" if len(fail_rows) <= 3 else f"; +{len(fail_rows) - 3} more"
                msg = f"Deleted {ok_count} SD file(s) on {device_ip}; failed {len(fail_rows)}. {sample}{more}"
            append_action_log("file-transfers-delete-sd", msg)
            self._send_html(render_file_transfers_page(message=msg, selected_uid=selected_uid))
            return True
        if self.path == "/file-transfers-upload-selected":
            selected_uid = (form.get("uid") or [""])[0].strip()
            remote_filename = (form.get("remote_filename") or [""])[0].strip()
            upload_op_id = (form.get("upload_op_id") or [""])[0].strip()
            devices = read_devices_rows(Path("data/discovered_devices.csv"))
            selected_device = _find_device_by_uid(devices, selected_uid)
            if selected_device is None:
                self._send_html(render_file_transfers_page(message="Select a known Arduino first.", selected_uid=selected_uid))
                return True
            device_ip = (selected_device.get("device_ip", "") or selected_device.get("recv_ip", "")).strip()
            short_uid = (selected_device.get("short_uid", "") or "").strip()
            network_uid = (selected_device.get("network_uid", "") or "").strip()
            burrow_id = (selected_device.get("burrow_id", "") or "").strip()
            ap_id = (selected_device.get("ap_id", "") or "").strip()
            if not device_ip:
                self._send_html(render_file_transfers_page(message="Selected Arduino has no IP address.", selected_uid=selected_uid))
                return True
            if not remote_filename:
                self._send_html(render_file_transfers_page(message="Select a file from SD list first.", selected_uid=selected_uid))
                return True
            ok_gate, reason = can_web_access_file_transfers(selected_uid, "Upload selected SD file")
            if not ok_gate:
                self._send_html(render_file_transfers_page(message=reason, selected_uid=selected_uid))
                return True
            set_upload_progress(upload_op_id, 0, "upload starting", done=False, error=False)

            def _on_progress(pct: int, written: int, total: int, name: str) -> None:
                """Update web upload progress state from transfer callbacks."""
                set_upload_progress(
                    upload_op_id,
                    pct,
                    f"uploading {name} ({written}/{total})",
                    done=False,
                    error=False,
                )

            ok, detail, result = upload_selected_remote_file(
                unique_id=selected_uid,
                short_uid=short_uid,
                network_uid=network_uid,
                burrow_id=burrow_id,
                ap_id=ap_id,
                device_ip=device_ip,
                remote_filename=remote_filename,
                progress_callback=_on_progress,
            )
            if ok:
                set_upload_progress(upload_op_id, 100, "upload complete", done=True, error=False)
            else:
                set_upload_progress(upload_op_id, 0, detail, done=True, error=True)
            upload_cid = new_correlation_id("UPL")
            upload_category = "ok" if ok else categorize_command_error(detail)
            detail = f"{detail} [cid={upload_cid} category={upload_category}]"
            result["message"] = detail
            if not ok:
                result["error_text"] = f"{result.get('error_text', '')} [cid={upload_cid} category={upload_category}]".strip()
            run_id = f"WEB_MANUAL_{int(time.time() * 1000)}"
            try:
                log_transfer_event(Path(DEFAULT_DB_PATH), run_id, result)
            except Exception as exc:  # noqa: BLE001
                append_action_log("file-transfers-upload-selected-db-log-error", f"Failed to write transfer event: {exc}")
            msg = detail
            append_action_log("file-transfers-upload-selected", msg)
            self._send_html(render_file_transfers_page(message=msg, selected_uid=selected_uid))
            return True
        return False

    def _handle_post_maintenance_routes(self, form: dict[str, list[str]]) -> bool:
        """Process POST maintenance commands and Burrow_ID edits."""
        if self.path == "/maintenance-action":
            selected_uid = (form.get("uid") or [""])[0].strip()
            action = (form.get("action") or [""])[0].strip()
            devices = read_devices_rows(Path("data/discovered_devices.csv"))
            selected_device = _find_device_by_uid(devices, selected_uid)
            if selected_device is None:
                self._send_html(render_maintenance_page(message="Select a known Arduino first.", selected_uid=selected_uid))
                return True
            device_ip = (selected_device.get("device_ip", "") or selected_device.get("recv_ip", "")).strip()
            if not device_ip:
                self._send_html(render_maintenance_page(message="Selected Arduino has no IP address.", selected_uid=selected_uid))
                return True
            ok_gate, reason = can_web_contact_arduino(selected_uid, f"Maintenance {action}")
            if not ok_gate:
                append_action_log("maintenance-blocked", reason)
                self._send_html(render_maintenance_page(message=reason, selected_uid=selected_uid))
                return True
            if action == "get-time":
                msg = run_maintenance_action_with_retry(
                    action="GET_TIME",
                    fn=lambda: query_device_time(device_ip=device_ip),
                    timeout_s=2.0,
                )
                append_action_log("maintenance-get-time", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return True
            if action == "set-time":
                preset = (form.get("tz_preset") or ["ast"])[0].strip().lower()
                if preset not in TZ_PRESET_OFFSETS:
                    self._send_html(
                        render_maintenance_page(
                            message=f"Invalid timezone preset: {preset}",
                            selected_uid=selected_uid,
                        )
                    )
                    return True
                offset_hours = TZ_PRESET_OFFSETS[preset]
                set_last_set_time_state(offset_hours, preset)
                msg = run_maintenance_action_with_retry(
                    action="SET_TIME",
                    fn=lambda: set_device_time(device_ip=device_ip, offset_hours=offset_hours),
                    timeout_s=2.0,
                )
                append_action_log("maintenance-set-time", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return True
            if action == "ping":
                msg = run_maintenance_action_with_retry(
                    action="PING",
                    fn=lambda: ping_device(device_ip=device_ip),
                    timeout_s=2.0,
                )
                append_action_log("maintenance-ping", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return True
            if action == "reboot":
                msg = run_maintenance_action_with_retry(
                    action="REBOOT",
                    fn=lambda: reboot_device(device_ip=device_ip),
                    timeout_s=3.0,
                )
                append_action_log("maintenance-reboot", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return True
            self._send_html(render_maintenance_page(message=f"Unknown maintenance action: {action}", selected_uid=selected_uid))
            return True
        if self.path == "/maintenance-burrow":
            selected_uid = (form.get("uid") or [""])[0].strip()
            burrow_id = (form.get("burrow_id") or [""])[0].strip()
            mode = (form.get("mode") or ["save"])[0].strip().lower()
            if not selected_uid:
                self._send_html(render_maintenance_page(message="Select a known Arduino first.", selected_uid=selected_uid))
                return True
            if mode == "edit":
                current = _current_burrow_for_uid(selected_uid)
                msg = f"Loaded current Burrow_ID for {selected_uid}."
                append_action_log("maintenance-edit-burrow", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid, burrow_input=current))
                return True
            msg = assign_burrow_id_for_uid(unique_id=selected_uid, burrow_id=burrow_id)
            append_action_log("maintenance-save-burrow", msg)
            self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid, burrow_input=burrow_id))
            return True
        if self.path == "/maintenance-wifi-policy":
            selected_uid = (form.get("uid") or [""])[0].strip()
            mode = (form.get("mode") or ["save"])[0].strip().lower()
            policy = {
                "policy": (form.get("policy") or [POLICY_STAY_ACTIVE])[0],
                "grace_min": (form.get("grace_min") or ["60"])[0],
                "wake_hour": (form.get("wake_hour") or ["16"])[0],
            }
            clean = save_wifi_policy(policy, DEFAULT_WIFI_POLICY_PATH)
            msg = (
                f"Saved Gateway WiFi policy: {clean['policy']} "
                f"grace_min={clean['grace_min']} wake_hour={clean['wake_hour']}."
            )
            if mode == "apply":
                if not selected_uid:
                    self._send_html(render_maintenance_page(message="Select a known Arduino first.", selected_uid=selected_uid))
                    return True
                devices = read_devices_rows(Path("data/discovered_devices.csv"))
                selected_device = _find_device_by_uid(devices, selected_uid)
                if selected_device is None:
                    self._send_html(render_maintenance_page(message="Invalid device selection.", selected_uid=selected_uid))
                    return True
                device_ip = (selected_device.get("device_ip") or selected_device.get("recv_ip") or "").strip()
                if not device_ip:
                    self._send_html(render_maintenance_page(message="Selected Arduino has no IP address.", selected_uid=selected_uid))
                    return True
                apply_msg = set_device_wifi_policy(device_ip=device_ip, policy=clean)
                msg = f"{msg} {apply_msg}"
            append_action_log("maintenance-wifi-policy", msg)
            self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
            return True
        return False

    def _handle_post_legacy_routes(self, form: dict[str, list[str]]) -> bool:
        """Process older POST routes retained for backward compatibility."""
        if self.path == "/assign-burrow-id":
            short_uid = (form.get("short_uid") or [""])[0].strip().upper()
            burrow_id = (form.get("burrow_id") or [""])[0].strip()
            if not short_uid:
                self._send_html(render_page("short_uid is required for burrow assignment."))
                return True
            msg = assign_burrow_id(short_uid=short_uid, burrow_id=burrow_id)
            self._send_html(render_page(msg))
            return True
        if self.path == "/force-upload":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = MANAGER.start_force_upload(uid=uid, device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/query-time":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = query_device_time(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/set-time":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            preset = (form.get("tz_preset") or ["ast"])[0].strip().lower()
            if preset in TZ_PRESET_OFFSETS:
                offset_hours = TZ_PRESET_OFFSETS[preset]
            else:
                preset = "manual"
                offset_raw = (form.get("offset_hours") or [str(WEB_SET_TIME_OFFSET_HOURS)])[0].strip()
                try:
                    offset_hours = float(offset_raw)
                except ValueError:
                    self._send_html(render_page(f"Invalid offset_hours value: {offset_raw}"))
                    return True
            set_last_set_time_state(offset_hours, preset)
            msg = set_device_time(device_ip=device_ip, offset_hours=offset_hours)
            self._send_html(render_page(msg))
            return True
        if self.path == "/ping":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = ping_device(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/get-status":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = query_device_status(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/get-config":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = query_device_config(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/get-diag":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = query_device_diagnostics(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/get-last-data":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = query_last_data(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/set-config":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            start_hour_raw = (form.get("start_hour") or [""])[0].strip()
            end_hour_raw = (form.get("end_hour") or [""])[0].strip()
            updates = {}
            if start_hour_raw:
                try:
                    start_hour = int(start_hour_raw)
                    if 0 <= start_hour <= 23:
                        updates["START_HOUR"] = str(start_hour)
                    else:
                        self._send_html(render_page("START_HOUR must be 0-23"))
                        return True
                except ValueError:
                    self._send_html(render_page("Invalid START_HOUR"))
                    return True
            if end_hour_raw:
                try:
                    end_hour = int(end_hour_raw)
                    if 0 <= end_hour <= 23:
                        updates["END_HOUR"] = str(end_hour)
                    else:
                        self._send_html(render_page("END_HOUR must be 0-23"))
                        return True
                except ValueError:
                    self._send_html(render_page("Invalid END_HOUR"))
                    return True
            if not updates:
                self._send_html(render_page("No config updates specified"))
                return True
            msg = set_device_config(device_ip=device_ip, config_updates=updates)
            self._send_html(render_page(msg))
            return True
        if self.path == "/reboot":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = reboot_device(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/enter-data-mode":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            ok, reason = can_enter_data_mode(uid=uid)
            if not ok:
                self._send_html(render_page(reason))
                return True
            msg = enter_data_mode(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        if self.path == "/clear-errors":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return True
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return True
            msg = clear_device_errors(device_ip=device_ip)
            self._send_html(render_page(msg))
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        """Serve GET requests with centralized exception handling."""
        try:
            self._do_GET_impl()
        except Exception as exc:  # noqa: BLE001
            cid = new_correlation_id("WEBGET")
            msg = f"GET {self.path} failed: {exc} [cid={cid}]"
            append_action_log("web-get-error", msg)
            parsed = urlparse(self.path)
            route = parsed.path
            if route.startswith("/api/") or route in {"/upload-progress", "/health"}:
                self._send_json({"ok": False, "error": str(exc), "cid": cid}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_html(render_page(f"Internal error. cid={cid}"), HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        """Serve POST requests with centralized exception handling."""
        try:
            self._do_POST_impl()
        except Exception as exc:  # noqa: BLE001
            cid = new_correlation_id("WEBPOST")
            msg = f"POST {self.path} failed: {exc} [cid={cid}]"
            append_action_log("web-post-error", msg)
            self._send_html(render_page(f"Internal error. cid={cid}"), HTTPStatus.INTERNAL_SERVER_ERROR)

    def _do_GET_impl(self) -> None:
        """Dispatch GET requests to monitor/API/page route groups."""
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if self._handle_get_monitor_routes(route, query):
            return
        if self._handle_get_api_routes(route, query):
            return
        if self._handle_get_page_routes(route, query):
            return
        self._send_html(render_page("Not found."), HTTPStatus.NOT_FOUND)

    def _do_POST_impl(self) -> None:
        """Dispatch POST requests to route groups after parsing form data."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        form = parse_qs(body, keep_blank_values=True)
        invalidate_endpoint_cache()
        if self._handle_post_ops_routes(form):
            return
        if self._handle_post_file_transfer_routes(form):
            return
        if self._handle_post_maintenance_routes(form):
            return
        if self._handle_post_legacy_routes(form):
            return
        self._send_html(render_page("Not found."), HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        """Suppress default HTTP request logging noise."""
        return


def main() -> int:
    """Initialize DB/server and run the web UI event loop."""
    host = DEFAULT_WEB_HOST
    port = DEFAULT_WEB_PORT
    db_path = Path(DEFAULT_DB_PATH).expanduser()
    try:
        init_db(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: DB init failed for '{db_path}': {exc}")
    schema_info = get_db_schema_info(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"{WEB_APP_NAME} web control ready: http://{host}:{port}")
    print(
        "DB schema: "
        f"path={schema_info.get('path', str(db_path))} "
        f"exists={schema_info.get('db_exists', '0')} "
        f"version={schema_info.get('schema_version', '') or 'unknown'} "
        f"tables={schema_info.get('table_count', '0')} "
        f"indexes={schema_info.get('index_count', '0')}"
    )
    if schema_info.get("error"):
        print(f"Warning: DB schema inspection error: {schema_info.get('error')}")
    print(
        "Startup config: "
        f"profile={ACTIVE_NETWORK_PROFILE} "
        f"source={ACTIVE_NETWORK_PROFILE_SOURCE} "
        f"db={DEFAULT_DB_PATH} "
        f"poll_ms(devices={UI_POLL_DEVICES_MS},uploads={UI_POLL_UPLOADS_MS},"
        f"activity={UI_POLL_ACTIVITY_MS},python_log={UI_POLL_PYTHON_LOG_MS},"
        f"upload_progress={UI_POLL_UPLOAD_PROGRESS_MS})"
    )
    access_host = host
    if host in {"0.0.0.0", "::", ""}:
        access_host = DEFAULT_BIND_IP
    print(
        "Laptop access: "
        f"join WiFi '{WEB_APP_NAME}' and open http://{access_host}:{port} "
        "(or use routed/VPN access)."
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
