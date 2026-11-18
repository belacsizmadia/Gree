"""TapHome Gree_AC compatible Modbus TCP gateway."""

from __future__ import annotations

import asyncio
import ipaddress
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from pymodbus import ModbusDeviceIdentification  # type: ignore[import-not-found]
    from pymodbus.datastore import (  # type: ignore[import-not-found]
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
    )
    from pymodbus.server import ModbusTcpServer  # type: ignore[import-not-found]
except ImportError:
    ModbusDeviceIdentification = None
    ModbusDeviceContext = None
    ModbusSequentialDataBlock = None
    ModbusServerContext = None
    ModbusTcpServer = None


GREE_AC_REGISTERS: dict[int, str] = {
    2: "power",
    4: "ambient_temperature_x10",
    5: "idu_address",
    17: "mode",
    19: "fan_speed",
    20: "target_temperature",
    22: "vertical_swing",
    23: "horizontal_swing",
    24: "fresh_air",
    25: "sleep",
    34: "contamination_grade",
    39: "ambient_sensor_selection",
    49: "outdoor_temperature",
    77: "dred",
    82: "return_air_temperature",
    83: "display_temperature",
}

WRITABLE_REGISTERS = {2, 17, 19, 20, 22, 23}
MODE_TO_TAPHOME = {0: 5, 1: 1, 2: 3, 3: 4, 4: 2}
TAPHOME_TO_MODE = {0: 1, 1: 1, 2: 4, 3: 2, 4: 3, 5: 0}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


if ModbusSequentialDataBlock is not None:

    class TapHomeRegisterBlock(ModbusSequentialDataBlock):
        """Thread-safe holding-register block with a callback for external writes."""

        def __init__(self, write_callback: Callable[[int, int], None]) -> None:
            # ModbusDeviceContext adds one to every protocol address. Starting the
            # block at 1 keeps TapHome H:2 mapped to values[2].
            super().__init__(1, [0] * 128)
            self._write_callback = write_callback
            self._lock = threading.RLock()
            self._internal_write = False

        def getValues(self, address: int, count: int = 1) -> Any:  # noqa: N802
            with self._lock:
                return super().getValues(address, count)

        def setValues(self, address: int, values: Any) -> Any:  # noqa: N802
            value_list = values if isinstance(values, list) else [values]
            with self._lock:
                result = super().setValues(address, value_list)
                internal = self._internal_write
            if result is None and not internal:
                for offset, value in enumerate(value_list):
                    self._write_callback(address - 1 + offset, int(value))
            return result

        def update_registers(self, values: dict[int, int]) -> None:
            with self._lock:
                self._internal_write = True
                try:
                    for register, value in values.items():
                        super().setValues(register + 1, [int(value) & 0xFFFF])
                finally:
                    self._internal_write = False

else:
    TapHomeRegisterBlock = None  # type: ignore[assignment,misc]


class TapHomeGateway:
    """Translate Gree local state to the official TapHome Gree_AC register map."""

    def __init__(
        self,
        climate: Any,
        *,
        enabled: bool = False,
        host: str = "0.0.0.0",
        port: int = 502,
        slave_id: int = 1,
        device_identifier: str | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        if port < 1 or port > 65535:
            raise ValueError("A TapHome Modbus-portnak 1 és 65535 közé kell esnie.")
        if slave_id < 1 or slave_id > 247:
            raise ValueError("A TapHome Slave ID tartománya 1–247.")
        self.climate = climate
        self.enabled = bool(enabled)
        self.host = host
        self.port = int(port)
        self.slave_id = int(slave_id)
        self.device_identifier = device_identifier.strip() if device_identifier else None
        self.poll_interval = max(1.0, min(float(poll_interval), 300.0))

        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._commands: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=100)
        self._registers = {register: 0 for register in GREE_AC_REGISTERS}
        self._block: Any = None
        self._context: Any = None
        self._server: Any = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._running = False
        self._connected = False
        self._last_sync_at: str | None = None
        self._last_command_at: str | None = None
        self._last_error: str | None = None
        self._last_device: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return all(
            item is not None
            for item in (
                ModbusDeviceIdentification,
                ModbusDeviceContext,
                ModbusSequentialDataBlock,
                ModbusServerContext,
                ModbusTcpServer,
            )
        )

    def start(self) -> None:
        if not self.enabled or self._server_thread:
            return
        if not self.available:
            with self._lock:
                self._last_error = "A pymodbus nincs telepítve. Futtasd: pip install -r requirements.txt"
            return

        self._block = TapHomeRegisterBlock(self._queue_register_write)
        device_context = ModbusDeviceContext(hr=self._block)
        self._context = ModbusServerContext(devices={self.slave_id: device_context}, single=False)
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker, name="taphome-worker", daemon=True)
        self._server_thread = threading.Thread(target=self._run_server, name="taphome-modbus", daemon=True)
        self._worker_thread.start()
        self._server_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            pass
        loop = self._server_loop
        server = self._server
        if loop and server and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(server.shutdown(), loop).result(timeout=3)
            except Exception:
                pass
        for thread in (self._worker_thread, self._server_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=3)
        with self._lock:
            self._worker_thread = None
            self._server_thread = None
            self._running = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            registers = dict(self._registers)
            return {
                "template": "Gree_AC",
                "templateRevision": "1.0",
                "templateSource": "https://taphome.com/compatibility/devices/gree-ac/template.xml",
                "transport": "Modbus TCP",
                "enabled": self.enabled,
                "available": self.available,
                "running": self._running,
                "connected": self._connected,
                "host": self.host,
                "port": self.port,
                "slaveId": self.slave_id,
                "pollInterval": self.poll_interval,
                "deviceIdentifier": self.device_identifier,
                "device": self._last_device,
                "lastSyncAt": self._last_sync_at,
                "lastCommandAt": self._last_command_at,
                "error": self._last_error,
                "holdingRegisters": {str(key): value for key, value in sorted(registers.items())},
                "registerNames": {str(key): value for key, value in GREE_AC_REGISTERS.items()},
            }

    def sync_now(self) -> dict[str, Any]:
        with self._sync_lock:
            try:
                result = self._query_climate()
                self._publish_state(result)
            except Exception as error:
                with self._lock:
                    self._connected = False
                    self._last_error = str(error)
                raise
        return self.status()

    def submit_register_write(self, register: int, value: int) -> dict[str, Any]:
        register = int(register)
        value = int(value)
        self._validate_write(register, value)
        self._queue_register_write(register, value)
        return {"accepted": True, "register": register, "value": value, "queuedAt": utc_now()}

    def apply_register_write(self, register: int, value: int) -> dict[str, Any]:
        """Apply a register write synchronously; mainly used by HTTP and tests."""
        register = int(register)
        value = int(value)
        self._validate_write(register, value)
        changes = self._changes_for_write(register, value)
        with self._sync_lock:
            result = self._control_climate(changes)
            self._publish_state(result)
            with self._lock:
                self._last_command_at = utc_now()
        return self.status()

    def _run_server(self) -> None:
        try:
            asyncio.run(self._serve_modbus())
        except Exception as error:
            with self._lock:
                self._running = False
                self._last_error = f"A Modbus TCP szerver nem indult el: {error}"
        finally:
            self._server = None
            self._server_loop = None

    async def _serve_modbus(self) -> None:
        self._server_loop = asyncio.get_running_loop()
        identity = ModbusDeviceIdentification(
            info_name={
                "VendorName": "Gree Local Monitor",
                "ProductCode": "GREE-TH",
                "ProductName": "TapHome Gree_AC Gateway",
                "ModelName": "Raspberry Pi",
                "MajorMinorRevision": "1.0",
            }
        )
        server = ModbusTcpServer(
            context=self._context,
            identity=identity,
            address=(self.host, self.port),
            ignore_missing_devices=False,
        )
        self._server = server
        with self._lock:
            self._running = True
        await server.serve_forever()

    def _worker(self) -> None:
        next_sync = 0.0
        while not self._stop_event.is_set():
            timeout = max(0.05, min(1.0, next_sync - time.monotonic())) if next_sync else 0.05
            try:
                command = self._commands.get(timeout=timeout)
            except queue.Empty:
                command = None
            if command is not None:
                try:
                    self.apply_register_write(*command)
                except Exception as error:
                    with self._lock:
                        self._last_error = str(error)
            if self._stop_event.is_set():
                break
            if time.monotonic() >= next_sync:
                try:
                    self.sync_now()
                except Exception:
                    pass
                next_sync = time.monotonic() + self.poll_interval

    def _queue_register_write(self, register: int, value: int) -> None:
        try:
            self._validate_write(register, value)
            self._commands.put_nowait((register, value))
        except (ValueError, queue.Full) as error:
            with self._lock:
                self._last_error = str(error) if isinstance(error, ValueError) else "A TapHome parancssor megtelt."

    def _query_climate(self) -> dict[str, Any]:
        try:
            return self.climate.query(self.device_identifier)
        except ValueError:
            target = self._target_ip()
            self.climate.scan(target)
            return self.climate.query(self.device_identifier)

    def _control_climate(self, changes: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.climate.control(changes, self.device_identifier)
        except ValueError:
            target = self._target_ip()
            self.climate.scan(target)
            return self.climate.control(changes, self.device_identifier)

    def _target_ip(self) -> str | None:
        if not self.device_identifier:
            return None
        try:
            return str(ipaddress.IPv4Address(self.device_identifier))
        except ipaddress.AddressValueError:
            return None

    def _publish_state(self, result: dict[str, Any]) -> None:
        registers = self._registers_from_state(result)
        with self._lock:
            self._registers.update(registers)
            self._connected = True
            self._last_sync_at = utc_now()
            self._last_error = None
            self._last_device = result.get("device")
        if self._block is not None:
            self._block.update_registers(registers)

    def _registers_from_state(self, result: dict[str, Any]) -> dict[int, int]:
        state = result.get("state") or {}
        mode = (state.get("mode") or {}).get("value")
        fan = (state.get("fanSpeed") or {}).get("value")
        vertical = (state.get("verticalSwing") or {}).get("value")
        horizontal = (state.get("horizontalSwing") or {}).get("value")
        room = state.get("currentTemperature")
        target = state.get("targetTemperature")
        if state.get("temperatureUnit") == "°F":
            room = self._fahrenheit_to_celsius(room)
            target = self._fahrenheit_to_celsius(target)
        room_value = self._encode_signed(round(float(room) * 10)) if room is not None else 0
        diagnostic_temp = round(float(room)) & 0xFFFF if room is not None else 0
        fan_value = 6 if state.get("turbo") else int(fan or 0)
        return {
            2: 170 if state.get("power") else 85,
            4: room_value,
            5: self.slave_id,
            17: MODE_TO_TAPHOME.get(mode, 5),
            19: fan_value,
            20: int(target) if target is not None else 20,
            22: int(vertical or 0),
            23: int(horizontal or 0),
            24: 1 if state.get("freshAir") else 0,
            25: 1 if state.get("sleep") else 0,
            34: 1 if state.get("cleanFilter") else 0,
            39: 0,
            49: 0,
            77: 0,
            82: diagnostic_temp,
            83: diagnostic_temp,
        }

    @staticmethod
    def _encode_signed(value: int) -> int:
        return (0x8000 | abs(value)) if value < 0 else value & 0x7FFF

    @staticmethod
    def _fahrenheit_to_celsius(value: Any) -> float | None:
        return None if value is None else (float(value) - 32) * 5 / 9

    @staticmethod
    def _validate_write(register: int, value: int) -> None:
        if register not in WRITABLE_REGISTERS:
            raise ValueError(f"A H:{register} regiszter a Gree_AC sablonban nem írható.")
        if value < 0 or value > 0xFFFF:
            raise ValueError("A Modbus regiszterérték tartománya 0–65535.")

    @staticmethod
    def _changes_for_write(register: int, value: int) -> dict[str, Any]:
        if register == 2:
            if value not in (85, 170):
                raise ValueError("A H:2 értéke 85 (KI) vagy 170 (BE) lehet.")
            return {"power": value == 170}
        if register == 17:
            if value not in TAPHOME_TO_MODE:
                raise ValueError("A H:17 üzemmód tartománya 0–5.")
            return {"mode": TAPHOME_TO_MODE[value]}
        if register == 19:
            if value < 0 or value > 6:
                raise ValueError("A H:19 ventilátor tartománya 0–6.")
            return {"fanSpeed": min(value, 5), "turbo": value == 6}
        if register == 20:
            if value < 16 or value > 30:
                raise ValueError("A H:20 célhőmérséklet tartománya 16–30 °C.")
            return {"targetTemperature": value}
        if register == 22:
            if value < 0 or value > 11:
                raise ValueError("A H:22 függőleges légterelő tartománya 0–11.")
            return {"verticalSwing": value}
        if register == 23:
            if value < 0 or value > 8:
                raise ValueError("A H:23 vízszintes légterelő tartománya 0–8.")
            return {"horizontalSwing": min(value, 6)}
        raise ValueError(f"A H:{register} regiszter nem támogatott.")

