"""Persistent, named climate endpoints used by the UI and TapHome API."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
from pathlib import Path
from typing import Any


PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class ClimateProfileStore:
    """Thread-safe storage for API-safe names and local IPv4 addresses."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else None
        self._lock = threading.RLock()
        self._profiles: dict[str, dict[str, str]] = {}
        self._load()

    def list(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(profile) for profile in sorted(self._profiles.values(), key=lambda item: item["name"].lower())]

    def upsert(self, name: Any, ip: Any) -> dict[str, str]:
        profile = {"name": self.validate_name(name), "ip": self.validate_ip(ip)}
        with self._lock:
            self._profiles[profile["name"].lower()] = profile
            self._save()
            return dict(profile)

    def delete(self, name: Any) -> dict[str, str]:
        validated = self.validate_name(name)
        with self._lock:
            profile = self._profiles.pop(validated.lower(), None)
            if profile is None:
                raise ValueError(f"Nincs ilyen klímaprofil: {validated}.")
            self._save()
            return dict(profile)

    def resolve(self, name: Any) -> dict[str, str]:
        validated = self.validate_name(name)
        with self._lock:
            profile = self._profiles.get(validated.lower())
            if profile is None:
                raise ValueError(f"Nincs ilyen klímaprofil: {validated}.")
            return dict(profile)

    @staticmethod
    def validate_name(value: Any) -> str:
        name = str(value or "").strip()
        if not PROFILE_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                "A profilnév 1–32 karakter lehet: betű, szám, aláhúzás vagy kötőjel; szóköz nem használható."
            )
        return name

    @staticmethod
    def validate_ip(value: Any) -> str:
        text = str(value or "").strip()
        try:
            address = ipaddress.ip_address(text)
        except ValueError as error:
            raise ValueError("Érvényes klíma IPv4-címet adj meg.") from error
        if not isinstance(address, ipaddress.IPv4Address) or address.is_multicast or address.is_unspecified:
            raise ValueError("Érvényes klíma IPv4-címet adj meg.")
        return str(address)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
            profiles = decoded.get("profiles", []) if isinstance(decoded, dict) else []
            if not isinstance(profiles, list):
                raise ValueError("A profiles mező nem lista.")
            for item in profiles:
                if not isinstance(item, dict):
                    raise ValueError("Hibás klímaprofil.")
                name = self.validate_name(item.get("name"))
                ip = self.validate_ip(item.get("ip"))
                self._profiles[name.lower()] = {"name": name, "ip": ip}
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"A klímaprofilok nem tölthetők be ({self.path}): {error}") from error

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = {"version": 1, "profiles": self.list()}
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"A klímaprofilok nem menthetők: {error}") from error
