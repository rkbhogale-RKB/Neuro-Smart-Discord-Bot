"""Predefined knowledge about the OTHER bots in the server: which prefix they use, whether they
need an @mention, whether they are slash-command-only, and how long they usually take to answer.

Stored in app_profiles.json (copy app_profiles.example.json). Keys are the bot's name (lowercase)
or its user id. Edit the file by hand or use /neuro appprofile in Discord."""
import json
import logging
import os

import config

log = logging.getLogger("app_profiles")


def _load() -> dict[str, dict]:
    try:
        with open(config.APP_PROFILES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("could not read %s, ignoring it", config.APP_PROFILES_PATH)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in data.items() if isinstance(v, dict)}


def get(app) -> dict:
    """Profile for a discord Member/User (matched by id, name or display name), else {}."""
    profiles = _load()
    for key in (str(app.id), app.name.lower(), app.display_name.lower()):
        if key in profiles:
            return profiles[key]
    return {}


def all_profiles() -> dict[str, dict]:
    return _load()


def set_profile(key: str, *, prefix: str | None = None, mention: bool | None = None,
                slash_only: bool | None = None, wait: float | None = None) -> dict:
    """Create or update one profile and save the file. Returns the new profile."""
    key = key.strip().lstrip("@").lower()
    if not key:
        raise ValueError("empty app name")
    profiles = _load()
    p = dict(profiles.get(key, {}))
    if prefix is not None:
        p["prefix"] = prefix.strip()[:10]
    if mention is not None:
        p["mention"] = bool(mention)
    if slash_only is not None:
        p["slash_only"] = bool(slash_only)
    if wait is not None:
        p["wait"] = max(1.0, min(float(wait), 30.0))
    profiles[key] = p
    tmp = config.APP_PROFILES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config.APP_PROFILES_PATH)
    return p


def describe(p: dict) -> str:
    if not p:
        return "no profile"
    bits = []
    if p.get("slash_only"):
        bits.append("slash-commands only")
    if p.get("prefix"):
        bits.append(f"prefix `{p['prefix']}`")
    if p.get("mention"):
        bits.append("needs @mention")
    if p.get("wait"):
        bits.append(f"waits {p['wait']}s")
    return ", ".join(bits) or "empty profile"
