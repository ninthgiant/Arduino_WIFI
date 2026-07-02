#!/usr/bin/env python3
"""Directly upload one named Arduino SD file without LIST_FILES."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from bsm_network.config import DEFAULT_DISCOVER_PORT
from bsm_network.protocol import ping_device, transfer_file_protocol
from bsm_network.records import build_local_filename, ensure_unique_filename


def _control_socket(bind_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    requested = (bind_ip or "").strip() or "0.0.0.0"
    try:
        sock.bind((requested, 0))
    except OSError:
        if requested == "0.0.0.0":
            raise
        sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.2)
    return sock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", required=True, help="Arduino IP address")
    parser.add_argument("--file", required=True, help="Exact Arduino SD filename, e.g. TR260701.TXT")
    parser.add_argument("--bind-ip", default="192.168.10.1", help="Gateway LAN IP used for transfer listener")
    parser.add_argument("--port", type=int, default=DEFAULT_DISCOVER_PORT, help="Arduino UDP control port")
    parser.add_argument("--out-dir", default="data/files", help="Gateway output directory")
    parser.add_argument("--uid", default="DIRECT", help="Arduino unique ID for local filename/logging")
    parser.add_argument("--short-uid", default="DIRECT", help="Short UID folder/name tag")
    parser.add_argument("--timeout", type=float, default=180.0, help="Transfer timeout seconds")
    parser.add_argument("--skip-ping", action="store_true", help="Skip initial PING check")
    parser.add_argument("--tolerant", action="store_true", help="Save partial file if integrity/resume fails")
    args = parser.parse_args()

    filename = args.file.strip()
    short_uid = args.short_uid.strip() or "DIRECT"
    uid = args.uid.strip() or short_uid
    out_root = Path(args.out_dir).expanduser()
    out_dir = out_root / short_uid
    local_name = build_local_filename(filename, uid, device_short_uid=short_uid)
    local_name = ensure_unique_filename(local_name, out_dir)

    sock = _control_socket(args.bind_ip)
    try:
        if not args.skip_ping:
            pong = ping_device(sock, args.ip, args.port, timeout_s=3.0)
            print(f"PING OK: {pong}")

        saved = transfer_file_protocol(
            control_sock=sock,
            device_ip=args.ip,
            control_port=args.port,
            local_bind_ip=args.bind_ip,
            requested_filename=filename,
            output_dir=out_dir,
            device_uid=uid,
            device_short_uid=short_uid,
            log_root=Path("data/file_logs"),
            local_filename=local_name,
            timeout_s=args.timeout,
            tolerant_integrity=args.tolerant,
            mark_partial_received=args.tolerant,
        )
        print(f"SAVED: {saved}")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
