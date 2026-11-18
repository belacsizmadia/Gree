"""Semantic HTTP facade for TapHome Packet Parser integrations."""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any


MODE_VALUES = {"auto": 0, "cool": 1, "dry": 2, "fan": 3, "heat": 4}
FAN_VALUES = {"auto": 0, "low": 1, "mediumlow": 2, "medium": 3, "mediumhigh": 4, "high": 5}
HORIZONTAL_SWING_VALUES = {
    "default": 0,
    "fullswing": 1,
    "left": 2,
    "leftcenter": 3,
    "center": 4,
    "rightcenter": 5,
    "right": 6,
}
VERTICAL_SWING_VALUES = {
    "default": 0,
    "fullswing": 1,
    "fixedupper": 2,
    "fixeduppermiddle": 3,
    "fixedmiddle": 4,
    "fixedlowermiddle": 5,
    "fixedlower": 6,
    "swingupper": 7,
    "swinguppermiddle": 8,
    "swingmiddle": 9,
    "swinglowermiddle": 10,
    "swinglower": 11,
}

READABLE_FIELDS = (
    "power",
    "mode",
    "currentTemperature",
    "targetTemperature",
    "temperatureUnit",
    "fanSpeed",
    "horizontalSwing",
    "verticalSwing",
    "freshAir",
    "quiet",
    "turbo",
    "sleep",
    "light",
    "xfan",
    "anion",
    "currentHumidity",
    "targetHumidity",
    "cleanFilter",
)
WRITABLE_FIELDS = (
    "power",
    "mode",
    "targetTemperature",
    "fanSpeed",
    "horizontalSwing",
    "verticalSwing",
    "freshAir",
    "quiet",
    "turbo",
    "sleep",
    "light",
    "xfan",
    "anion",
)
BOOLEAN_FIELDS = {"power", "freshAir", "quiet", "turbo", "sleep", "light", "xfan", "anion"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TapHomeGateway:
    """Convert the nested Gree state to a flat, semantic Packet Parser API."""

    def __init__(
        self,
        climate: Any,
        *,
        profiles: Any | None = None,
        device_identifier: str | None = None,
        cache_seconds: float = 1.0,
    ) -> None:
        self.climate = climate
        self.profiles = profiles
        self.device_identifier = device_identifier.strip() if device_identifier else None
        self.cache_seconds = max(0.0, min(float(cache_seconds), 30.0))
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._cached_states: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_state: dict[str, Any] | None = None
        self._last_read_at: str | None = None
        self._last_write_at: str | None = None
        self._last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(getattr(self.climate, "available", True))

    def status(self) -> dict[str, Any]:
        """Return gateway diagnostics without starting a climate-network request."""
        with self._lock:
            cached = dict(self._last_state) if self._last_state else None
            profiles = self.profiles.list() if self.profiles is not None else []
            return {
                "template": "Gree AC - HTTP Packet Parser",
                "templateRevision": "1.1",
                "transport": "HTTP/JSON",
                "enabled": True,
                "available": self.available,
                "running": True,
                "connected": cached is not None and self._last_error is None,
                "endpoint": "/api/taphome/ac/{name}",
                "templateEndpoint": "/api/taphome/template.xml",
                "deviceIdentifier": self.device_identifier,
                "cacheSeconds": self.cache_seconds,
                "lastReadAt": self._last_read_at,
                "lastWriteAt": self._last_write_at,
                "error": self._last_error,
                "state": cached,
                "profiles": profiles,
                "readableFields": list(READABLE_FIELDS),
                "writableFields": list(WRITABLE_FIELDS),
            }

    def read(self, name: str | None = None, *, force: bool = False) -> dict[str, Any]:
        """Read the selected climate and return flat semantic values."""
        identifier, profile_name, cache_key = self._resolve_device(name)
        with self._lock:
            cached = self._cached_states.get(cache_key)
            if not force and cached and time.monotonic() - cached[0] <= self.cache_seconds:
                return dict(cached[1])

        with self._request_lock:
            with self._lock:
                cached = self._cached_states.get(cache_key)
                if not force and cached and time.monotonic() - cached[0] <= self.cache_seconds:
                    return dict(cached[1])
            try:
                result = self._query_climate(identifier)
                payload = self._semantic_state(result, profile_name)
            except Exception as error:
                with self._lock:
                    self._last_error = str(error)
                raise
            with self._lock:
                self._cached_states[cache_key] = (time.monotonic(), payload)
                self._last_state = payload
                self._last_read_at = utc_now()
                self._last_error = None
                return dict(payload)

    def control(self, changes: dict[str, Any], name: str | None = None) -> dict[str, Any]:
        """Apply semantic values and return the resulting semantic state."""
        normalized = self._normalize_changes(changes)
        identifier, profile_name, cache_key = self._resolve_device(name)
        with self._request_lock:
            try:
                result = self._control_climate(normalized, identifier)
                payload = self._semantic_state(result, profile_name)
            except Exception as error:
                with self._lock:
                    self._last_error = str(error)
                raise
            with self._lock:
                self._cached_states[cache_key] = (time.monotonic(), payload)
                self._last_state = payload
                self._last_read_at = utc_now()
                self._last_write_at = self._last_read_at
                self._last_error = None
                return dict(payload)

    def sync_now(self, name: str | None = None) -> dict[str, Any]:
        """Explicitly refresh the Packet Parser state."""
        return self.read(name, force=True)

    def _resolve_device(self, name: str | None) -> tuple[str | None, str | None, str]:
        if name:
            if self.profiles is None:
                raise ValueError("A név szerinti klímaprofilok nincsenek beállítva.")
            profile = self.profiles.resolve(name)
            return profile["ip"], profile["name"], f"profile:{profile['name'].lower()}:{profile['ip']}"

        if self.profiles is not None:
            profiles = self.profiles.list()
            if len(profiles) == 1:
                profile = profiles[0]
                return profile["ip"], profile["name"], f"profile:{profile['name'].lower()}:{profile['ip']}"
            if len(profiles) > 1:
                raise ValueError("Több klímaprofil van beállítva; add meg a profil nevét az API URL-ben.")

        identifier = self.device_identifier
        return identifier, None, f"device:{identifier or 'automatic'}"

    def _query_climate(self, identifier: str | None) -> dict[str, Any]:
        try:
            return self.climate.query(identifier)
        except ValueError:
            self.climate.scan(self._target_ip(identifier))
            return self.climate.query(identifier)

    def _control_climate(self, changes: dict[str, Any], identifier: str | None) -> dict[str, Any]:
        try:
            return self.climate.control(changes, identifier)
        except ValueError as original_error:
            # Discovery errors are recoverable. Validation errors from an already
            # selected device must be returned to the caller unchanged.
            try:
                self.climate.scan(self._target_ip(identifier))
                return self.climate.control(changes, identifier)
            except ValueError:
                raise original_error

    @staticmethod
    def _target_ip(identifier: str | None) -> str | None:
        if not identifier:
            return None
        try:
            return str(ipaddress.IPv4Address(identifier))
        except ipaddress.AddressValueError:
            return None

    @staticmethod
    def _semantic_state(result: dict[str, Any], climate_name: str | None = None) -> dict[str, Any]:
        state = result.get("state") or {}
        device = result.get("device") or {}
        unit = str(state.get("temperatureUnit") or "°C").replace("°", "")
        current_temperature = state.get("currentTemperature")
        target_temperature = state.get("targetTemperature")
        if unit.upper() == "F":
            current_temperature = TapHomeGateway._fahrenheit_to_celsius(current_temperature)
            target_temperature = TapHomeGateway._fahrenheit_to_celsius(target_temperature)
            unit = "C"
        return {
            "apiVersion": "1.0",
            "online": True,
            "updatedAt": result.get("updatedAt") or utc_now(),
            "climateName": climate_name,
            "deviceName": device.get("name"),
            "deviceMac": device.get("mac"),
            "deviceIp": device.get("ip"),
            "power": state.get("power"),
            "mode": TapHomeGateway._enum_value(state.get("mode")),
            "currentTemperature": current_temperature,
            "targetTemperature": target_temperature,
            "temperatureUnit": unit,
            "fanSpeed": TapHomeGateway._enum_value(state.get("fanSpeed")),
            "horizontalSwing": TapHomeGateway._enum_value(state.get("horizontalSwing")),
            "verticalSwing": TapHomeGateway._enum_value(state.get("verticalSwing")),
            "freshAir": state.get("freshAir"),
            "quiet": state.get("quiet"),
            "turbo": state.get("turbo"),
            "sleep": state.get("sleep"),
            "light": state.get("light"),
            "xfan": state.get("xfan"),
            "anion": state.get("anion"),
            "currentHumidity": state.get("currentHumidity"),
            "targetHumidity": state.get("targetHumidity"),
            "cleanFilter": state.get("cleanFilter"),
        }

    @staticmethod
    def _enum_value(value: Any) -> Any:
        if isinstance(value, dict):
            name = value.get("name")
            return None if name in (None, "unknown") else name
        return value

    @staticmethod
    def _fahrenheit_to_celsius(value: Any) -> float | None:
        if value is None:
            return None
        converted = (float(value) - 32) * 5 / 9
        return round(converted, 1)

    @classmethod
    def _normalize_changes(cls, changes: dict[str, Any]) -> dict[str, Any]:
        if not changes:
            raise ValueError("Nincs alkalmazható klímabeállítás.")
        unknown = sorted(set(changes) - set(WRITABLE_FIELDS))
        if unknown:
            raise ValueError(f"Nem támogatott TapHome mező: {', '.join(unknown)}")

        normalized: dict[str, Any] = {}
        for field, value in changes.items():
            if field in BOOLEAN_FIELDS:
                normalized[field] = cls._boolean(value, field)
            elif field == "targetTemperature":
                normalized[field] = cls._bounded_number(value, 16, 30, "targetTemperature")
            elif field == "mode":
                normalized[field] = cls._enum_input(value, MODE_VALUES, "mode")
            elif field == "fanSpeed":
                normalized[field] = cls._enum_input(value, FAN_VALUES, "fanSpeed")
            elif field == "horizontalSwing":
                normalized[field] = cls._enum_input(value, HORIZONTAL_SWING_VALUES, "horizontalSwing")
            elif field == "verticalSwing":
                normalized[field] = cls._enum_input(value, VERTICAL_SWING_VALUES, "verticalSwing")
        return normalized

    @staticmethod
    def _key(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.strip().lower())

    @classmethod
    def _enum_input(cls, value: Any, mapping: dict[str, int], field: str) -> int:
        if isinstance(value, str):
            key = cls._key(value)
            if field == "mode" and key in {"fanonly", "wind"}:
                key = "fan"
            if key in mapping:
                return mapping[key]
        allowed = ", ".join(mapping)
        raise ValueError(f"A(z) {field} értéke ezek egyike lehet: {allowed}.")

    @staticmethod
    def _boolean(value: Any, field: str) -> bool:
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"A(z) {field} értéke true vagy false lehet.")

    @staticmethod
    def _bounded_number(value: Any, minimum: int, maximum: int, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Érvénytelen {field} érték.")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Érvénytelen {field} érték.") from error
        if not number.is_integer() or number < minimum or number > maximum:
            raise ValueError(f"A(z) {field} engedélyezett tartománya: {minimum}–{maximum} °C.")
        return int(number)
