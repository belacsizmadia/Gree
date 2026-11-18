#!/usr/bin/env python3
"""Gree USB Monitor - tiny Raspberry Pi friendly serial-to-web bridge."""

from __future__ import annotations

import argparse
import glob
import json
import math
import mimetypes
import os
import queue
import random
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import serial  # type: ignore[import-not-found]
    from serial.tools import list_ports  # type: ignore[import-not-found]
except ImportError:  # Demo mode and the UI still work without pyserial.
    serial = None
    list_ports = None


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
NUMBER_PATTERN = re.compile(r"(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
SUPPORTED_BAUDRATES = {1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def extract_numeric_values(text: str) -> dict[str, float]:
    """Extract chartable values from JSON or a plain-text serial message."""
    stripped = text.strip()
    if not stripped:
        return {}

    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        decoded = None

    values: dict[str, float] = {}
    if isinstance(decoded, dict):
        for key, value in decoded.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                values[str(key)[:32]] = float(value)
                if len(values) == 6:
                    break
        if values:
            return values

    for index, match in enumerate(NUMBER_PATTERN.finditer(stripped)):
        value = float(match.group(0))
        if math.isfinite(value):
            values[f"érték {index + 1}"] = value
        if len(values) == 3:
            break
    return values


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {"device": self.device, "description": self.description}


class SerialMonitor:
    """Owns the serial reader and fans readings out to SSE subscribers."""

    def __init__(self, max_read_size: int = 512) -> None:
        self.max_read_size = max(1, min(max_read_size, 4096))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._serial_handle: Any = None
        self._clients: set[queue.Queue[dict[str, Any]]] = set()
        self._mode: str | None = None
        self._device: str | None = None
        self._baudrate: int | None = None
        self._last_error: str | None = None
        self._started_at: str | None = None
        self._last_packet_at: str | None = None
        self._packet_count = 0
        self._byte_count = 0
        self._sequence = 0

    def available_ports(self) -> list[PortInfo]:
        discovered: dict[str, PortInfo] = {}
        if list_ports is not None:
            try:
                for port in list_ports.comports():
                    discovered[port.device] = PortInfo(port.device, port.description or "Soros USB-eszköz")
            except Exception:
                pass

        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usb*", "/dev/cu.SLAB*"):
            for device in glob.glob(pattern):
                discovered.setdefault(device, PortInfo(device, "Soros eszköz"))
        return sorted(discovered.values(), key=lambda item: item.device)

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._reader_thread and self._reader_thread.is_alive())
            return {
                "connected": running,
                "mode": self._mode,
                "device": self._device,
                "baudrate": self._baudrate,
                "startedAt": self._started_at,
                "lastPacketAt": self._last_packet_at,
                "packets": self._packet_count,
                "bytes": self._byte_count,
                "error": self._last_error,
                "serialAvailable": serial is not None,
            }

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        client: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._clients.discard(client)

    def connect(self, device: str, baudrate: int) -> dict[str, Any]:
        if serial is None:
            raise RuntimeError("A pyserial nincs telepítve. Futtasd: pip install -r requirements.txt")
        device = device.strip()
        if not device:
            raise ValueError("Adj meg egy soros eszközt, például /dev/ttyUSB0.")
        if baudrate not in SUPPORTED_BAUDRATES:
            raise ValueError("Nem támogatott baud rate.")

        self.disconnect()
        try:
            handle = serial.Serial(device, baudrate=baudrate, timeout=0.25)
        except Exception as error:
            raise RuntimeError(f"Nem sikerült megnyitni: {device} ({error})") from error

        with self._lock:
            self._reset_session("serial", device, baudrate)
            self._serial_handle = handle
            self._stop_event.clear()
            self._reader_thread = threading.Thread(target=self._read_serial, name="serial-reader", daemon=True)
            self._reader_thread.start()
        self._broadcast_state()
        return self.status()

    def start_demo(self) -> dict[str, Any]:
        self.disconnect()
        with self._lock:
            self._reset_session("demo", "Beépített jelforrás", None)
            self._stop_event.clear()
            self._reader_thread = threading.Thread(target=self._run_demo, name="demo-reader", daemon=True)
            self._reader_thread.start()
        self._broadcast_state()
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            thread = self._reader_thread
            handle = self._serial_handle
            self._stop_event.set()
            self._serial_handle = None

        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.5)

        with self._lock:
            self._reader_thread = None
            self._mode = None
            self._device = None
            self._baudrate = None
        self._broadcast_state()
        return self.status()

    def _reset_session(self, mode: str, device: str, baudrate: int | None) -> None:
        self._mode = mode
        self._device = device
        self._baudrate = baudrate
        self._last_error = None
        self._started_at = utc_now()
        self._last_packet_at = None
        self._packet_count = 0
        self._byte_count = 0

    def _read_serial(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                handle = self._serial_handle
            if handle is None:
                return
            try:
                waiting = getattr(handle, "in_waiting", 0)
                payload = handle.read(min(max(waiting, 1), self.max_read_size))
                if payload:
                    self._publish(payload)
            except Exception as error:
                with self._lock:
                    self._last_error = f"Olvasási hiba: {error}"
                self._broadcast_state()
                return

    def _run_demo(self) -> None:
        phase = 0.0
        while not self._stop_event.wait(0.42):
            phase += 0.15
            sample = {
                "temperature": round(23.2 + math.sin(phase) * 1.7 + random.uniform(-0.08, 0.08), 2),
                "humidity": round(49.0 + math.cos(phase * 0.7) * 4.0 + random.uniform(-0.15, 0.15), 2),
                "pressure": round(1013.0 + math.sin(phase * 0.24) * 2.4, 2),
            }
            self._publish((json.dumps(sample, separators=(",", ":")) + "\n").encode())

    def _publish(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        with self._lock:
            self._sequence += 1
            timestamp = utc_now()
            self._last_packet_at = timestamp
            self._packet_count += 1
            self._byte_count += len(payload)
            event = {
                "type": "reading",
                "sequence": self._sequence,
                "receivedAt": timestamp,
                "byteLength": len(payload),
                "text": text,
                "hex": payload.hex(" ").upper(),
                "values": extract_numeric_values(text),
            }
        self._fan_out(event)

    def _broadcast_state(self) -> None:
        self._fan_out({"type": "state", "status": self.status()})

    def _fan_out(self, event: dict[str, Any]) -> None:
        with self._lock:
            clients = tuple(self._clients)
        for client in clients:
            try:
                client.put_nowait(event)
            except queue.Full:
                try:
                    client.get_nowait()
                    client.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], monitor: SerialMonitor):
        super().__init__(address, handler)
        self.monitor = monitor

    def handle_error(self, request: Any, client_address: tuple[str, int]) -> None:
        """Browser reloads commonly reset an SSE socket; that is not a server error."""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "GreeUsbMonitor/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def monitor(self) -> SerialMonitor:
        return self.server.monitor  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json(self.monitor.status())
        elif path == "/api/ports":
            self._send_json({"ports": [port.as_dict() for port in self.monitor.available_ports()]})
        elif path == "/api/stream":
            self._serve_event_stream()
        else:
            self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/connect":
                body = self._read_json()
                status = self.monitor.connect(str(body.get("device", "")), int(body.get("baudrate", 115200)))
                self._send_json(status)
            elif path == "/api/demo":
                self._read_json(optional=True)
                self._send_json(self.monitor.start_demo())
            elif path == "/api/disconnect":
                self._read_json(optional=True)
                self._send_json(self.monitor.disconnect())
            else:
                self._send_json({"error": "Ismeretlen végpont."}, HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self._send_json({"error": "Váratlan szerverhiba."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json(self, optional: bool = False) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("Túl nagy kérés.")
        if length == 0:
            if optional:
                return {}
            raise ValueError("Hiányzó kérésadat.")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("Érvénytelen JSON.") from error
        if not isinstance(value, dict):
            raise ValueError("A kérésnek JSON objektumnak kell lennie.")
        return value

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_event_stream(self) -> None:
        client = self.monitor.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            initial = {"type": "state", "status": self.monitor.status()}
            self._write_sse(initial)
            while True:
                try:
                    event = client.get(timeout=15)
                    self._write_sse(event)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.monitor.unsubscribe(client)

    def _write_sse(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path).lstrip("/")
        requested = (STATIC_DIR / relative).resolve()
        try:
            requested.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not requested.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content = requested.read_bytes()
        mime_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, message_format: str, *args: Any) -> None:
        if os.environ.get("GREE_QUIET") != "1":
            print(f"[{self.log_date_time_string()}] {message_format % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="USB soros adatok valós idejű webes megjelenítője")
    parser.add_argument("--host", default=os.environ.get("GREE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GREE_PORT", "8080")))
    parser.add_argument("--serial-port", default=os.environ.get("GREE_SERIAL_PORT"))
    parser.add_argument("--baudrate", type=int, default=int(os.environ.get("GREE_BAUDRATE", "115200")))
    parser.add_argument("--demo", action="store_true", default=os.environ.get("GREE_DEMO") == "1")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = SerialMonitor()
    httpd = AppServer((args.host, args.port), AppHandler, monitor)

    if args.demo:
        monitor.start_demo()
    elif args.serial_port:
        try:
            monitor.connect(args.serial_port, args.baudrate)
        except (ValueError, RuntimeError) as error:
            print(f"Automatikus csatlakozás sikertelen: {error}")

    stop_once = threading.Event()

    def shutdown(_signum: int, _frame: Any) -> None:
        if not stop_once.is_set():
            stop_once.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"Gree USB Monitor: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever(poll_interval=0.25)
    finally:
        monitor.disconnect()
        httpd.server_close()


if __name__ == "__main__":
    main()
