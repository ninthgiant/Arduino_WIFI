from __future__ import annotations

import socket
import time
import zlib
import re
from pathlib import Path
from typing import Callable

from .records import append_file_receive_log


def parse_payload(payload: str) -> dict[str, str | int] | None:
    parts = [p.strip() for p in payload.split(",")]
    if len(parts) != 3:
        return None
    device_id, unix_time_raw, sample_raw = parts
    try:
        unix_time = int(unix_time_raw)
        sample = int(sample_raw)
    except ValueError:
        return None
    return {"device_id": device_id, "unix_time": unix_time, "sample": sample}


class _BufferedSocketReader:
    def __init__(self, sock: socket.socket, chunk_size: int = 4096):
        self.sock = sock
        self.buf = bytearray()
        self.chunk_size = chunk_size

    def recv_until_newline(self, limit: int = 512) -> bytes:
        while True:
            idx = self.buf.find(b"\n")
            if idx != -1:
                line = bytes(self.buf[: idx + 1])
                del self.buf[: idx + 1]
                if len(line) > limit:
                    raise ValueError("Header exceeded maximum length")
                return line

            if len(self.buf) >= limit:
                raise ValueError("Header exceeded maximum length")

            part = self.sock.recv(self.chunk_size)
            if not part:
                raise ConnectionError("Socket closed while waiting for newline-terminated header")
            self.buf.extend(part)

    def recv_exact(self, n: int) -> bytes:
        if n <= 0:
            return b""
        while len(self.buf) < n:
            part = self.sock.recv(max(self.chunk_size, n - len(self.buf)))
            if not part:
                raise ConnectionError("Socket closed during payload read")
            self.buf.extend(part)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


class _ResumeRequested(Exception):
    def __init__(self, offset: int, reason: str):
        super().__init__(reason)
        self.offset = offset
        self.reason = reason


def _parse_tcp_disconnected_offset(err_line: str, transfer_id: str) -> int | None:
    # Expected shape:
    # ERROR,<transfer_id>,TCP_DISCONNECTED,<offset>
    m = re.match(rf"^ERROR,{re.escape(transfer_id)},TCP_DISCONNECTED,(\d+)$", err_line.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _new_transfer_id(prefix: str = "T") -> str:
    return f"{prefix}{int(time.time() * 1000)}"


def _partial_path(path: Path, transfer_id: str) -> Path:
    stem = path.stem
    suffix = path.suffix
    return path.with_name(f"{stem}_PARTIAL_{transfer_id}{suffix}")


def _open_transfer_stream(
    control_sock: socket.socket,
    server: socket.socket,
    device_ip: str,
    control_port: int,
    transfer_id: str,
    requested_filename: str,
    tcp_port: int,
    start_offset: int,
    timeout_s: float,
) -> tuple[socket.socket, str, int, int]:
    start_msg = f"START_FILE,{transfer_id},{requested_filename},{tcp_port},{start_offset}".encode("utf-8")
    control_sock.sendto(start_msg, (device_ip, control_port))
    print(f"[{device_ip}] START_FILE sent (tcp_port={tcp_port}, offset={start_offset})")

    file_size = None
    chunk_size = None
    filename = requested_filename
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue

        line = data.decode("utf-8", errors="replace").strip()
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0] == "ERROR" and parts[1] == transfer_id:
            raise RuntimeError(line)
        if len(parts) != 5 or parts[0] != "FILE_INFO" or parts[1] != transfer_id:
            continue
        filename = parts[2]
        file_size = int(parts[3])
        chunk_size = int(parts[4])
        print(f"[{device_ip}] FILE_INFO: name={filename} size={file_size} chunk={chunk_size}")
        break

    if file_size is None or chunk_size is None:
        raise TimeoutError("Did not receive FILE_INFO for transfer")

    conn, addr = server.accept()
    conn.settimeout(timeout_s)
    print(f"[{device_ip}] TCP connected from {addr[0]}:{addr[1]}")
    return conn, filename, file_size, chunk_size


def request_remote_file_list(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float,
) -> list[str]:
    retry_attempts = 3
    retry_backoff_s = 0.25
    last_err = f"LIST_FILES timeout for {device_ip}"

    for attempt in range(1, retry_attempts + 1):
        transfer_id = _new_transfer_id("L")
        cmd = f"LIST_FILES,{transfer_id}".encode("utf-8")
        control_sock.sendto(cmd, (device_ip, control_port))

        files: list[str] = []
        seen: set[str] = set()
        got_end = False
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                try:
                    data, (src_ip, _) = control_sock.recvfrom(2048)
                except socket.timeout:
                    continue
                if src_ip != device_ip:
                    continue

                line = data.decode("utf-8", errors="replace").strip()
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2 or parts[1] != transfer_id:
                    continue

                msg_type = parts[0]
                if msg_type == "ERROR":
                    last_err = line
                    raise RuntimeError(line)
                if msg_type == "FILE_LIST_BEGIN":
                    continue
                if msg_type == "FILE_ITEM" and len(parts) >= 3:
                    name = parts[2]
                    if name and name not in seen:
                        seen.add(name)
                        files.append(name)
                    continue
                if msg_type == "FILE_LIST_END":
                    got_end = True
                    break
        except RuntimeError:
            # Retry device-reported transient errors with the same policy as timeout.
            pass

        if got_end:
            return files

        if not last_err.startswith("ERROR,"):
            last_err = f"LIST_FILES timeout for {device_ip}"
        if attempt < retry_attempts:
            time.sleep(retry_backoff_s * attempt)

    if last_err.startswith("ERROR,"):
        raise RuntimeError(last_err)
    raise TimeoutError(f"{last_err} (attempts={retry_attempts})")


def send_time_sync(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    epoch: int | None = None,
    timeout_s: float = 3.0,
) -> bool:
    if epoch is None:
        epoch = int(time.time())

    msg = f"SET_TIME,{epoch}".encode("utf-8")
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_TIME,"):
            return True
        if line.startswith("ERR_TIME,"):
            return False
    return False


def ping_device(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 2.0,
) -> str:
    msg = b"PING"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("PONG,"):
            return line
    raise TimeoutError(f"PING timeout for {device_ip}")


def get_device_status(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 2.0,
) -> dict[str, str]:
    msg = b"GET_STATUS"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("STATUS,"):
            parts = line.split(",")
            status = {}
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    status[key] = value
            return status
    raise TimeoutError(f"GET_STATUS timeout for {device_ip}")


def get_device_config(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 2.0,
) -> dict[str, str]:
    msg = b"GET_CONFIG"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("CONFIG,"):
            parts = line.split(",")
            config = {}
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    config[key] = value
            return config
    raise TimeoutError(f"GET_CONFIG timeout for {device_ip}")


def get_device_diagnostics(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 2.0,
) -> dict[str, str]:
    msg = b"GET_DIAGNOSTICS"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("DIAG,"):
            parts = line.split(",")
            diag = {}
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    diag[key] = value
            return diag
    raise TimeoutError(f"GET_DIAGNOSTICS timeout for {device_ip}")


def get_last_data(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 2.0,
) -> dict[str, str]:
    msg = b"GET_LAST_DATA"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("LAST_DATA,"):
            parts = line.split(",")
            data_dict = {}
            for part in parts[1:]:
                if "=" in part:
                    key, value = part.split("=", 1)
                    data_dict[key] = value
            return data_dict
    raise TimeoutError(f"GET_LAST_DATA timeout for {device_ip}")


def set_device_config(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    config_updates: dict[str, str],
    timeout_s: float = 3.0,
) -> bool:
    params = ",".join(f"{k}={v}" for k, v in config_updates.items())
    msg = f"SET_CONFIG,{params}".encode("utf-8")
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_CONFIG"):
            return True
        if line.startswith("ERR_CONFIG"):
            return False
    return False


def set_wifi_policy(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    command: str,
    timeout_s: float = 3.0,
) -> bool:
    """Send SET_WIFI_POLICY to an Arduino and return whether it was accepted."""
    control_sock.sendto(command.encode("utf-8"), (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_WIFI_POLICY"):
            return True
        if line.startswith("ERR_WIFI_POLICY"):
            return False
    return False


def reboot_device(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 3.0,
) -> bool:
    msg = b"REBOOT"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_REBOOT"):
            return True
    return False


def enter_data_mode(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 3.0,
) -> bool:
    msg = b"ENTER_DATA_MODE"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_ENTER_DATA_MODE"):
            return True
    return False


def clear_device_errors(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 3.0,
) -> bool:
    msg = b"CLEAR_ERRORS"
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_CLEAR_ERRORS"):
            return True
    return False


def transfer_file_protocol(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    local_bind_ip: str,
    requested_filename: str,
    output_dir: Path,
    device_uid: str | None = None,
    device_short_uid: str | None = None,
    log_root: Path | None = None,
    local_filename: str | None = None,
    timeout_s: float = 30.0,
    tolerant_integrity: bool = False,
    mark_partial_received: bool = False,
    progress_callback: Callable[[int, int, int, str], None] | None = None,
    integrity_result: dict[str, str | int] | None = None,
) -> Path:
    if not str(requested_filename or "").lower().endswith(".txt"):
        raise ValueError(f"Refusing non-.txt transfer request: {requested_filename}")
    transfer_id = _new_transfer_id("T")
    transfer_start = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{device_ip}] Transfer start: id={transfer_id} file={requested_filename}")

    requested_bind = (local_bind_ip or "").strip() or "0.0.0.0"
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((requested_bind, 0))
    except OSError as exc:
        if requested_bind != "0.0.0.0":
            server.bind(("0.0.0.0", 0))
            print(
                f"[{device_ip}] Warning: transfer bind to {requested_bind} failed ({exc}). "
                "Using 0.0.0.0."
            )
        else:
            raise
    server.listen(1)
    server.settimeout(timeout_s)
    tcp_port = server.getsockname()[1]

    final_name = local_filename if local_filename else requested_filename
    out_path = output_dir / final_name
    stream_crc32 = 0
    expected_offset = 0
    bytes_written = 0
    next_progress_report = 10
    integrity_status = "verified"
    file_size = None
    filename = requested_filename
    max_resume_attempts = 8
    resume_attempts = 0
    last_progress_pct = -1

    def _emit_progress(pct: int, written: int, total: int, name: str) -> None:
        nonlocal last_progress_pct
        pct_clamped = 0 if pct < 0 else (100 if pct > 100 else pct)
        if pct_clamped == last_progress_pct:
            return
        last_progress_pct = pct_clamped
        if progress_callback is None:
            return
        try:
            progress_callback(pct_clamped, written, total, name)
        except Exception:
            # UI callback errors must never break file transfer.
            pass

    with out_path.open("wb") as out:
        completed = False
        while not completed:
            conn = None
            try:
                conn, filename, session_file_size, _chunk_size = _open_transfer_stream(
                    control_sock=control_sock,
                    server=server,
                    device_ip=device_ip,
                    control_port=control_port,
                    transfer_id=transfer_id,
                    requested_filename=requested_filename,
                    tcp_port=tcp_port,
                    start_offset=expected_offset,
                    timeout_s=timeout_s,
                )
                if file_size is None:
                    file_size = session_file_size
                    _emit_progress(0, bytes_written, file_size, filename)
                elif file_size != session_file_size:
                    raise ValueError(f"FILE_INFO size changed during resume: {file_size} -> {session_file_size}")

                reader = _BufferedSocketReader(conn)
                while True:
                    try:
                        header = reader.recv_until_newline().decode("utf-8", errors="replace").strip()
                    except (ConnectionError, TimeoutError, ValueError, socket.timeout) as exc:
                        raise _ResumeRequested(expected_offset, f"stream interrupted: {exc}") from exc

                    parts = [p.strip() for p in header.split(",")]
                    if not parts:
                        continue

                    if parts[0] == "CHUNK":
                        # Support both:
                        # - CHUNK,<id>,<idx>,<offset>,<len>
                        # - CHUNK,<id>,<idx>,<offset>,<len>,<crc32>
                        if len(parts) < 5 or parts[1] != transfer_id:
                            continue
                        _chunk_index = int(parts[2])
                        offset = int(parts[3])
                        payload_len = int(parts[4])
                        payload = reader.recv_exact(payload_len)

                        if offset < expected_offset:
                            # Duplicate chunk after resume; ignore.
                            continue
                        if offset > expected_offset:
                            raise _ResumeRequested(
                                expected_offset,
                                f"Chunk offset mismatch: expected={expected_offset}, got={offset}",
                            )

                        out.write(payload)
                        stream_crc32 = zlib.crc32(payload, stream_crc32)
                        expected_offset += payload_len
                        bytes_written += payload_len
                        if file_size and file_size > 0:
                            pct = int((bytes_written * 100) / file_size)
                            if pct >= next_progress_report:
                                print(f"[{device_ip}] Receiving {filename}: {pct}% ({bytes_written}/{file_size})")
                                _emit_progress(pct, bytes_written, file_size, filename)
                                next_progress_report += 10
                        continue

                    if parts[0] == "EOF":
                        if len(parts) != 4 or parts[1] != transfer_id:
                            continue
                        total_bytes = int(parts[2])
                        file_crc = parts[3].lower()
                        local_crc = f"{(stream_crc32 & 0xFFFFFFFF):08x}"
                        if total_bytes != bytes_written:
                            raise _ResumeRequested(
                                expected_offset,
                                f"EOF size mismatch (remote={total_bytes}, local={bytes_written})",
                            )
                        if file_crc != local_crc:
                            if tolerant_integrity:
                                integrity_status = "partial_eof_mismatch"
                                print(
                                    f"[{device_ip}] WARNING: EOF mismatch (remote crc={file_crc}, local crc={local_crc}); "
                                    "saving partial file."
                                )
                                completed = True
                                break
                            raise ValueError("EOF integrity check failed (crc mismatch)")
                        print(f"[{device_ip}] EOF verified: bytes={total_bytes} crc32={local_crc}")
                        if file_size and file_size > 0:
                            _emit_progress(100, bytes_written, file_size, filename)
                        completed = True
                        break

            except _ResumeRequested as exc:
                resume_attempts += 1
                if resume_attempts > max_resume_attempts:
                    if tolerant_integrity and bytes_written > 0:
                        integrity_status = "partial_resume_exhausted"
                        print(
                            f"[{device_ip}] WARNING: resume attempts exhausted at offset={expected_offset}; "
                            "saving partial file."
                        )
                        completed = True
                        break
                    raise TimeoutError(
                        f"Resume failed after {max_resume_attempts} attempts at offset={expected_offset}: {exc.reason}"
                    ) from exc
                print(
                    f"[{device_ip}] Resume attempt {resume_attempts}/{max_resume_attempts} "
                    f"from offset={exc.offset} ({exc.reason})"
                )
                time.sleep(0.25)
                continue
            except RuntimeError as exc:
                # Arduino may report a transport drop via UDP ERROR while we are trying to resume.
                err_line = str(exc)
                drop_offset = _parse_tcp_disconnected_offset(err_line, transfer_id)
                if drop_offset is not None:
                    expected_offset = max(expected_offset, drop_offset)
                    resume_attempts += 1
                    if resume_attempts > max_resume_attempts:
                        if tolerant_integrity and bytes_written > 0:
                            integrity_status = "partial_resume_exhausted"
                            print(
                                f"[{device_ip}] WARNING: resume attempts exhausted after TCP_DISCONNECTED at "
                                f"offset={expected_offset}; saving partial file."
                            )
                            completed = True
                            break
                        raise TimeoutError(
                            f"Resume failed after {max_resume_attempts} TCP_DISCONNECTED events at "
                            f"offset={expected_offset}"
                        ) from exc
                    print(
                        f"[{device_ip}] Resume attempt {resume_attempts}/{max_resume_attempts} "
                        f"from offset={expected_offset} (remote TCP_DISCONNECTED)"
                    )
                    time.sleep(0.25)
                    continue
                raise
            finally:
                if conn is not None:
                    conn.close()
    server.close()

    if integrity_status != "verified":
        partial_out_path = _partial_path(out_path, transfer_id)
        out_path.rename(partial_out_path)
        out_path = partial_out_path

    done_msg = f"DONE,{transfer_id}".encode("utf-8")
    control_sock.sendto(done_msg, (device_ip, control_port))
    print(f"[{device_ip}] DONE sent. Saved ({integrity_status}) -> {out_path}")
    transfer_seconds = time.monotonic() - transfer_start
    print(f"[{device_ip}] Transfer time: {transfer_seconds:.2f}s")
    if integrity_result is not None:
        integrity_result.clear()
        integrity_result.update(
            {
                "status": integrity_status,
                "source_filename": filename,
                "saved_path": str(out_path),
                "bytes_written": bytes_written,
                "file_size": int(file_size or 0),
                "checksum_crc32": f"{(stream_crc32 & 0xFFFFFFFF):08x}",
            }
        )

    should_mark_received = integrity_status == "verified" or (
        integrity_status != "verified" and mark_partial_received
    )
    if device_uid and log_root and should_mark_received:
        append_file_receive_log(
            log_root=log_root,
            device_uid=device_uid,
            device_short_uid=device_short_uid,
            source_filename=filename,
            saved_path=out_path,
            transfer_id=transfer_id,
            total_bytes=bytes_written,
            checksum_hex=f"{(stream_crc32 & 0xFFFFFFFF):08x}",
            transfer_seconds=transfer_seconds,
        )

    return out_path
