#!/usr/bin/env python3
"""Gree USB Monitor - tiny Raspberry Pi friendly serial-to-web bridge."""

from __future__ import annotations

import argparse
import asyncio
import glob
import ipaddress
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
from urllib.parse import parse_qs, unquote, urlparse

from climate_profiles import ClimateProfileStore
from taphome_gateway import TapHomeGateway

try:
    import serial  # type: ignore[import-not-found]
    from serial.tools import list_ports  # type: ignore[import-not-found]
except ImportError:  # Demo mode and the UI still work without pyserial.
    serial = None
    list_ports = None

try:
    from greeclimate.device import (  # type: ignore[import-not-found]
        Device as ClimateDevice,
        FanSpeed,
        HorizontalSwing,
        Mode,
        TemperatureUnits,
        VerticalSwing,
    )
    from greeclimate.deviceinfo import DeviceInfo as ClimateDeviceInfo  # type: ignore[import-not-found]
    from greeclimate.discovery import Discovery as ClimateDiscovery  # type: ignore[import-not-found]
    from greeclimate.exceptions import (  # type: ignore[import-not-found]
        DeviceNotBoundError,
        DeviceTimeoutError,
    )
except ImportError:  # The USB monitor remains usable if the Wi-Fi client is not installed.
    ClimateDevice = None
    ClimateDeviceInfo = None
    ClimateDiscovery = None
    DeviceNotBoundError = None
    DeviceTimeoutError = None
    FanSpeed = None
    HorizontalSwing = None
    Mode = None
    TemperatureUnits = None
    VerticalSwing = None


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TAPHOME_TEMPLATE_PATH = APP_DIR / "deploy" / "Gree_AC_PacketParser.xml"
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


class ClimateGatewayError(RuntimeError):
    """Expected local-network communication error."""


class ClimateGatewayUnavailable(ClimateGatewayError):
    """The optional local Gree client is not installed."""


class ClimateGateway:
    """Expose local Gree Wi-Fi discovery and state reads to the HTTP layer."""

    def __init__(self, default_target: str | None = None, scan_seconds: int = 2, state_timeout: float = 6.0) -> None:
        self.default_target = self._validate_target(default_target) if default_target else None
        self.scan_seconds = max(1, min(int(scan_seconds), 10))
        self.state_timeout = max(1.0, min(float(state_timeout), 30.0))
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._last_scan_at: str | None = None

    @property
    def available(self) -> bool:
        return ClimateDiscovery is not None and ClimateDevice is not None and ClimateDeviceInfo is not None

    def overview(self) -> dict[str, Any]:
        with self._lock:
            devices = list(self._devices.values())
            last_scan_at = self._last_scan_at
        return {
            "available": self.available,
            "configuredTarget": self.default_target,
            "lastScanAt": last_scan_at,
            "devices": devices,
        }

    def scan(self, target: str | None = None) -> dict[str, Any]:
        self._ensure_available()
        target = self._validate_target(target) if target else self.default_target
        with self._lock:
            try:
                devices = asyncio.run(self._discover(target, self.scan_seconds))
            except (OSError, asyncio.TimeoutError) as error:
                raise ClimateGatewayError(f"A Gree felderítés sikertelen: {error}") from error
            self._remember(devices)
            self._last_scan_at = utc_now()
            return {
                "available": True,
                "target": target,
                "lastScanAt": self._last_scan_at,
                "devices": [self._serialize_info(info) for info in devices],
            }

    def query(self, identifier: str | None = None) -> dict[str, Any]:
        self._ensure_available()
        with self._lock:
            info = self._select_device(identifier)
            try:
                result = asyncio.run(self._query_device(info))
            except (OSError, asyncio.TimeoutError) as error:
                raise ClimateGatewayError(f"A klíma nem válaszolt: {error}") from error
            except Exception as error:
                timeout_types = tuple(
                    item for item in (DeviceTimeoutError, DeviceNotBoundError) if isinstance(item, type)
                )
                if timeout_types and isinstance(error, timeout_types):
                    raise ClimateGatewayError(
                        "A klíma nem válaszolt. Ellenőrizd, hogy a Pi és a klíma azonos Wi-Fi hálózaton van-e."
                    ) from error
                raise
            return result

    def control(self, changes: dict[str, Any], identifier: str | None = None) -> dict[str, Any]:
        """Apply validated climate changes and return the resulting local state."""
        self._ensure_available()
        if not changes:
            raise ValueError("Nincs alkalmazható klímabeállítás.")
        with self._lock:
            info = self._select_device(identifier)
            try:
                return asyncio.run(self._control_device(info, changes))
            except (OSError, asyncio.TimeoutError) as error:
                raise ClimateGatewayError(f"A klíma vezérlése sikertelen: {error}") from error
            except Exception as error:
                timeout_types = tuple(
                    item for item in (DeviceTimeoutError, DeviceNotBoundError) if isinstance(item, type)
                )
                if timeout_types and isinstance(error, timeout_types):
                    raise ClimateGatewayError("A klíma nem igazolta vissza a beállítást.") from error
                raise

    def _ensure_available(self) -> None:
        if not self.available:
            raise ClimateGatewayUnavailable(
                "A Gree Wi-Fi kliens nincs telepítve. Futtasd: pip install -r requirements.txt"
            )

    @staticmethod
    def _validate_target(value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        try:
            address = ipaddress.ip_address(text)
        except ValueError as error:
            raise ValueError("Érvénytelen IPv4-cím.") from error
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError("A Gree helyi protokolljához IPv4-cím szükséges.")
        if address.is_multicast or address.is_unspecified:
            raise ValueError("Ez az IPv4-cím nem használható klímacímként.")
        return str(address)

    @staticmethod
    def _device_key(mac: str) -> str:
        return mac.replace(":", "").replace("-", "").replace(".", "").lower()

    @staticmethod
    def _serialize_info(info: Any) -> dict[str, Any]:
        return {
            "ip": str(info.ip),
            "port": int(info.port),
            "mac": str(info.mac),
            "name": str(info.name or info.mac),
            "brand": info.brand,
            "model": info.model,
            "version": info.version,
            "mid": info.mid,
        }

    def _remember(self, devices: list[Any]) -> None:
        for info in devices:
            serialized = self._serialize_info(info)
            self._devices[self._device_key(serialized["mac"])] = serialized

    def _select_device(self, identifier: str | None) -> dict[str, Any]:
        if identifier:
            needle = identifier.strip().lower()
            normalized = self._device_key(needle)
            for key, info in self._devices.items():
                if key == normalized or info["ip"].lower() == needle:
                    return info
            raise ValueError("A kért klíma nincs a felderített eszközök között. Indíts felderítést.")

        if not self._devices and self.default_target:
            devices = asyncio.run(self._discover(self.default_target, self.scan_seconds))
            self._remember(devices)
            self._last_scan_at = utc_now()
        if not self._devices:
            raise ValueError("Még nincs felderített klíma. Hívd meg előbb a /api/ac/scan végpontot.")
        if len(self._devices) > 1:
            raise ValueError("Több klíma található; add meg a device paramétert.")
        return next(iter(self._devices.values()))

    async def _discover(self, target: str | None, wait_for: int) -> list[Any]:
        discovery = ClimateDiscovery(timeout=max(wait_for, 2))
        try:
            targets = [ipaddress.IPv4Address(target)] if target else None
            return await discovery.scan(wait_for=wait_for, target_addresses=targets)
        finally:
            discovery.close()

    async def _connect_device(self, cached: dict[str, Any]) -> tuple[Any, Any]:
        # The device metadata obtained during the first scan is enough for most
        # modules. Reusing it removes a roughly one-second discovery round-trip
        # from every read and write. Modules that require a fresh discovery still
        # get the previous behavior as an automatic fallback.
        cached_info = ClimateDeviceInfo(
            cached["ip"], cached["port"], cached["mac"], cached["name"],
            cached.get("brand"), cached.get("model"), cached.get("version"), cached.get("mid"),
        )
        try:
            return await self._bind_device(cached_info)
        except Exception as direct_error:
            refreshed = await self._discover(cached["ip"], 1)
            matching = next(
                (
                    item
                    for item in refreshed
                    if self._device_key(str(item.mac)) == self._device_key(str(cached["mac"]))
                ),
                None,
            )
            if matching is None:
                raise direct_error
            self._remember([matching])
            return await self._bind_device(matching)

    async def _bind_device(self, info: Any) -> tuple[Any, Any]:
        device = ClimateDevice(info, timeout=int(self.state_timeout), bind_timeout=min(4, int(self.state_timeout)))
        try:
            await device.bind()
            deadline = asyncio.get_running_loop().time() + self.state_timeout
            while not device.has_valid_state and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            if not device.has_valid_state:
                await device.update_state()
                retry_deadline = asyncio.get_running_loop().time() + self.state_timeout
                while not device.has_valid_state and asyncio.get_running_loop().time() < retry_deadline:
                    await asyncio.sleep(0.05)
            if not device.has_valid_state:
                raise asyncio.TimeoutError("az állapotlekérés időtúllépés miatt megszakadt")
        except Exception:
            device.close()
            raise
        return device, info

    async def _query_device(self, cached: dict[str, Any]) -> dict[str, Any]:
        device, info = await self._connect_device(cached)
        try:
            return self._serialize_state(device, info)
        finally:
            device.close()

    async def _control_device(self, cached: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        device, info = await self._connect_device(cached)
        try:
            validators: dict[str, tuple[str, Any]] = {
                "power": ("power", lambda value: bool(value)),
                "mode": ("mode", lambda value: self._bounded_int(value, 0, 4, "üzemmód")),
                "targetTemperature": (
                    "target_temperature",
                    lambda value: self._bounded_int(value, 16, 30, "célhőmérséklet"),
                ),
                "fanSpeed": ("fan_speed", lambda value: self._bounded_int(value, 0, 5, "ventilátor")),
                "horizontalSwing": (
                    "horizontal_swing",
                    lambda value: self._bounded_int(value, 0, 6, "vízszintes légterelő"),
                ),
                "verticalSwing": (
                    "vertical_swing",
                    lambda value: self._bounded_int(value, 0, 11, "függőleges légterelő"),
                ),
                "freshAir": ("fresh_air", lambda value: bool(value)),
                "quiet": ("quiet", lambda value: bool(value)),
                "turbo": ("turbo", lambda value: bool(value)),
                "sleep": ("sleep", lambda value: bool(value)),
                "light": ("light", lambda value: bool(value)),
                "xfan": ("xfan", lambda value: bool(value)),
                "anion": ("anion", lambda value: bool(value)),
            }
            unknown = sorted(set(changes) - set(validators))
            if unknown:
                raise ValueError(f"Nem támogatott klímabeállítás: {', '.join(unknown)}")
            for name, value in changes.items():
                attribute, validator = validators[name]
                validated = validator(value)
                if name == "targetTemperature" and device.temperature_units == 1:
                    validated = round((validated * 9 / 5) + 32)
                setattr(device, attribute, validated)
            await device.push_state_update()
            return self._serialize_state(device, info)
        finally:
            device.close()

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Érvénytelen {label} érték.")
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Érvénytelen {label} érték.") from error
        if number < minimum or number > maximum:
            raise ValueError(f"A(z) {label} engedélyezett tartománya: {minimum}–{maximum}.")
        return number

    @staticmethod
    def _enum_name(enum_type: Any, value: Any) -> str | None:
        if value is None or enum_type is None:
            return None
        try:
            return enum_type(value).name.lower()
        except (TypeError, ValueError):
            return "unknown"

    def _serialize_state(self, device: Any, info: Any) -> dict[str, Any]:
        unit_name = self._enum_name(TemperatureUnits, device.temperature_units)
        return {
            "device": self._serialize_info(info),
            "updatedAt": utc_now(),
            "firmware": device.version,
            "state": {
                "power": device.power,
                "mode": {"value": device.mode, "name": self._enum_name(Mode, device.mode)},
                "targetTemperature": device.target_temperature,
                "currentTemperature": device.current_temperature,
                "temperatureUnit": "°F" if unit_name == "f" else "°C",
                "fanSpeed": {"value": device.fan_speed, "name": self._enum_name(FanSpeed, device.fan_speed)},
                "horizontalSwing": {
                    "value": device.horizontal_swing,
                    "name": self._enum_name(HorizontalSwing, device.horizontal_swing),
                },
                "verticalSwing": {
                    "value": device.vertical_swing,
                    "name": self._enum_name(VerticalSwing, device.vertical_swing),
                },
                "freshAir": device.fresh_air,
                "quiet": device.quiet,
                "turbo": device.turbo,
                "sleep": device.sleep,
                "light": device.light,
                "xfan": device.xfan,
                "anion": device.anion,
                "currentHumidity": device.current_humidity,
                "targetHumidity": device.target_humidity,
                "cleanFilter": device.clean_filter,
            },
            "raw": dict(device.raw_properties),
        }


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        monitor: SerialMonitor,
        climate: ClimateGateway | None = None,
        taphome: TapHomeGateway | None = None,
        profiles: ClimateProfileStore | None = None,
    ):
        super().__init__(address, handler)
        self.monitor = monitor
        self.climate = climate or ClimateGateway()
        self.profiles = profiles or ClimateProfileStore()
        self.taphome = taphome or TapHomeGateway(self.climate, profiles=self.profiles)

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

    @property
    def climate(self) -> ClimateGateway:
        return self.server.climate  # type: ignore[attr-defined,no-any-return]

    @property
    def taphome(self) -> TapHomeGateway:
        return self.server.taphome  # type: ignore[attr-defined,no-any-return]

    @property
    def profiles(self) -> ClimateProfileStore:
        return self.server.profiles  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/status":
            self._send_json(self.monitor.status())
        elif path == "/api/ports":
            self._send_json({"ports": [port.as_dict() for port in self.monitor.available_ports()]})
        elif path == "/api/ac/devices":
            self._send_json(self.climate.overview())
        elif path == "/api/ac/profiles":
            self._send_json({"profiles": self.profiles.list()})
        elif path == "/api/taphome/status":
            self._send_json(self.taphome.status())
        elif path in ("/api/taphome/ac", "/api/taphome/gree-ac") or path.startswith("/api/taphome/ac/"):
            try:
                self._send_json(self.taphome.read(self._taphome_name(path)))
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except ClimateGatewayUnavailable as error:
                self._send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            except ClimateGatewayError as error:
                self._send_json({"error": str(error)}, HTTPStatus.GATEWAY_TIMEOUT)
            except Exception:
                self._send_json({"error": "Váratlan szerverhiba."}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/taphome/template.xml":
            self._serve_file(TAPHOME_TEMPLATE_PATH, "application/xml; charset=utf-8", download=True)
        elif path == "/api/ac/status":
            try:
                query = parse_qs(parsed.query)
                identifier = query.get("device", [None])[0]
                self._send_json(self.climate.query(identifier))
            except ValueError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except ClimateGatewayUnavailable as error:
                self._send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            except ClimateGatewayError as error:
                self._send_json({"error": str(error)}, HTTPStatus.GATEWAY_TIMEOUT)
            except Exception:
                self._send_json({"error": "Váratlan szerverhiba."}, HTTPStatus.INTERNAL_SERVER_ERROR)
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
            elif path == "/api/ac/scan":
                body = self._read_json(optional=True)
                target = str(body.get("target", "")).strip() or None
                self._send_json(self.climate.scan(target))
            elif path == "/api/ac/profiles":
                body = self._read_json()
                profile = self.profiles.upsert(body.get("name"), body.get("ip"))
                self._send_json({"profile": profile, "profiles": self.profiles.list()})
            elif path == "/api/ac/profiles/delete":
                body = self._read_json()
                profile = self.profiles.delete(body.get("name"))
                self._send_json({"deleted": profile, "profiles": self.profiles.list()})
            elif path == "/api/taphome/sync":
                body = self._read_json(optional=True)
                self._send_json(self.taphome.sync_now(str(body.get("name", "")).strip() or None))
            elif path in ("/api/taphome/ac", "/api/taphome/gree-ac") or path.startswith("/api/taphome/ac/"):
                body = self._read_json()
                self._send_json(self.taphome.control(body, self._taphome_name(path)))
            else:
                self._send_json({"error": "Ismeretlen végpont."}, HTTPStatus.NOT_FOUND)
        except ClimateGatewayUnavailable as error:
            self._send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except ClimateGatewayError as error:
            self._send_json({"error": str(error)}, HTTPStatus.GATEWAY_TIMEOUT)
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

    @staticmethod
    def _taphome_name(path: str) -> str | None:
        prefix = "/api/taphome/ac/"
        if not path.startswith(prefix):
            return None
        name = unquote(path[len(prefix):]).strip()
        if not name or "/" in name:
            raise ValueError("Érvénytelen klímaprofil az API URL-ben.")
        return name

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

        mime_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        self._serve_file(requested, f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type)

    def _serve_file(self, path: Path, content_type: str, *, download: bool = False) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, message_format: str, *args: Any) -> None:
        if os.environ.get("GREE_QUIET") != "1":
            print(f"[{self.log_date_time_string()}] {message_format % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gree klíma- és USB-adatok helyi webes megjelenítője")
    parser.add_argument("--host", default=os.environ.get("GREE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GREE_PORT", "8080")))
    parser.add_argument("--serial-port", default=os.environ.get("GREE_SERIAL_PORT"))
    parser.add_argument("--baudrate", type=int, default=int(os.environ.get("GREE_BAUDRATE", "115200")))
    parser.add_argument("--demo", action="store_true", default=os.environ.get("GREE_DEMO") == "1")
    parser.add_argument("--ac-host", default=os.environ.get("GREE_AC_HOST"))
    parser.add_argument("--taphome-device", default=os.environ.get("GREE_TAPHOME_DEVICE"))
    parser.add_argument(
        "--config", default=os.environ.get("GREE_CONFIG_PATH", str(APP_DIR / "data" / "climates.json"))
    )
    parser.add_argument(
        "--taphome-cache", type=float, default=float(os.environ.get("GREE_TAPHOME_CACHE_SECONDS", "1"))
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    monitor = SerialMonitor()
    climate = ClimateGateway(default_target=args.ac_host)
    profiles = ClimateProfileStore(args.config)
    taphome = TapHomeGateway(
        climate,
        profiles=profiles,
        device_identifier=args.taphome_device,
        cache_seconds=args.taphome_cache,
    )
    httpd = AppServer((args.host, args.port), AppHandler, monitor, climate, taphome, profiles)

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
