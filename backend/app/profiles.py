from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_profiles(path: str) -> list[dict[str, Any]]:
    if not path:
        return []
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"Company configuration file does not exist: {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    profiles = payload.get("companies")
    if not isinstance(profiles, list):
        raise ValueError("Company configuration must contain a 'companies' list.")
    keys: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict) or not all(
            profile.get(key) for key in ("key", "name", "source", "target")
        ):
            raise ValueError("Each company profile needs key, name, source, and target.")
        if profile["key"] in keys:
            raise ValueError(f"Duplicate company profile key: {profile['key']}")
        keys.add(profile["key"])
    return profiles


def public_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"key": profile["key"], "name": profile["name"]} for profile in profiles]
