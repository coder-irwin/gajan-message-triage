"""Load and normalise the input file without ever raising on a bad record.

The file is "real-shaped, not clean". Every record is repaired or flagged,
never dropped silently: a record we cannot read still produces a result
that lands in a human queue.
"""
from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import InboundMessage

KNOWN_BRANDS = {"vitalis-wellness", "hair-studio", "voyage-travel"}
KNOWN_CHANNELS = {"whatsapp", "email", "instagram"}
MAX_CHARS = 6000   # longer bodies are cut for the model and flagged for a human


def load_records(path: str | Path) -> tuple[list[Any], list[str]]:
    """Return (raw_records, file_level_problems). Accepts a JSON array,
    an object wrapping one ({"messages": [...]}), or JSON Lines."""
    problems: list[str] = []
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if "�" in text:
        problems.append("file contained bytes that are not valid UTF-8; replaced")
    text = text.lstrip("﻿")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Fall back to JSON Lines, keeping whatever lines parse.
        records, bad = [], 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
                records.append({"_unparseable": line[:500]})
        problems.append(f"file is not a single JSON document ({exc.msg}); read as JSON Lines, {bad} unparseable line(s)")
        return records, problems

    if isinstance(data, list):
        return data, problems
    if isinstance(data, dict):
        for key in ("messages", "data", "items", "records"):
            if isinstance(data.get(key), list):
                problems.append(f"top-level object; used the '{key}' array")
                return data[key], problems
        problems.append("top-level object treated as a single message")
        return [data], problems
    problems.append(f"top-level JSON is a {type(data).__name__}; treated as one message")
    return [data], problems


def _clean_text(value: Any, problems: list[str]) -> str:
    if value is None:
        problems.append("text is null (possibly a media-only message)")
        return ""
    if isinstance(value, (dict, list)):
        problems.append(f"text is a {type(value).__name__}, not a string; serialised")
        value = json.dumps(value, ensure_ascii=False)
    elif not isinstance(value, str):
        problems.append(f"text is a {type(value).__name__}, not a string; converted")
        value = str(value)
    # Normalise unicode and strip control characters except newlines and tabs.
    value = unicodedata.normalize("NFKC", value)
    cleaned = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    if cleaned != value:
        problems.append("removed control or invisible characters")
    cleaned = cleaned.strip()
    if not cleaned and value:
        problems.append("text is whitespace only")
    return cleaned


def normalise(records: Iterable[Any]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    seen: dict[str, int] = {}
    for idx, rec in enumerate(records):
        problems: list[str] = []
        if not isinstance(rec, dict):
            problems.append(f"record is a {type(rec).__name__}, not an object")
            rec = {"text": rec}
        if "_unparseable" in rec:
            problems.append("record could not be parsed as JSON")
            rec = {"text": rec["_unparseable"]}

        msg_id = rec.get("id")
        if not isinstance(msg_id, str) or not msg_id.strip():
            problems.append("missing id; generated one")
            msg_id = f"ROW-{idx + 1:05d}"
        msg_id = msg_id.strip()
        if msg_id in seen:
            seen[msg_id] += 1
            problems.append(f"duplicate id {msg_id}; suffixed")
            msg_id = f"{msg_id}#dup{seen[msg_id]}"
        else:
            seen[msg_id] = 1

        brand = rec.get("brand")
        if not isinstance(brand, str) or not brand.strip():
            problems.append("missing brand")
            brand = "unknown"
        brand = brand.strip().lower()
        if brand not in KNOWN_BRANDS and brand != "unknown":
            problems.append(f"unknown brand '{brand}'")

        channel = rec.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            problems.append("missing channel")
            channel = "unknown"
        channel = channel.strip().lower()
        if channel not in KNOWN_CHANNELS and channel != "unknown":
            problems.append(f"unknown channel '{channel}'")

        received_at = rec.get("received_at")
        if isinstance(received_at, (int, float)) and not isinstance(received_at, bool):
            try:
                received_at = datetime.fromtimestamp(received_at, tz=timezone.utc).isoformat()
                problems.append("received_at was a unix timestamp; converted to UTC")
            except (OverflowError, OSError, ValueError):
                received_at = "invalid"
        if received_at is not None:
            try:
                datetime.fromisoformat(str(received_at))
                received_at = str(received_at)
            except ValueError:
                problems.append(f"unparseable received_at '{str(received_at)[:40]}'")
                received_at = None
        else:
            problems.append("missing received_at; relative dates cannot be resolved")

        text = _clean_text(rec.get("text"), problems)
        truncated = len(text) > MAX_CHARS
        if truncated:
            problems.append(f"text longer than {MAX_CHARS} chars; model saw the first {MAX_CHARS}")

        out.append(InboundMessage(
            id=msg_id, brand=brand, channel=channel, received_at=received_at,
            text=text, problems=problems, truncated=truncated,
        ))
    return out
