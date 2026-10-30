"""Local, disposable dialogue previews; confirmed decisions belong to Temporal."""

import hashlib
import json
from uuid import uuid4

from nicole.settings import state_dir


def preview_path(request_id: str, turn_id: str):
    key = hashlib.sha256(json.dumps([request_id, turn_id]).encode()).hexdigest()
    return state_dir() / "streams" / (key + ".json")


def write_preview(request_id: str, turn_id: str, reply: str):
    path = preview_path(request_id, turn_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(reply))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_preview(request_id: str, turn_id: str) -> str:
    try:
        value = json.loads(preview_path(request_id, turn_id).read_text())
        return value if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


def clear_preview(request_id: str, turn_id: str):
    preview_path(request_id, turn_id).unlink(missing_ok=True)


def partial_reply(content: str) -> str:
    """Decode only the top-level reply string, even across split JSON escapes."""
    content = content.strip()
    if content.startswith("```") and "\n" in content:
        content = content.split("\n", 1)[1].lstrip()
    if not content.startswith("{"):
        return ""
    decoder = json.JSONDecoder()
    rest = content[1:].lstrip()
    while rest:
        try:
            key, offset = decoder.raw_decode(rest)
            rest = rest[offset:].lstrip()
            if not rest.startswith(":"):
                return ""
            rest = rest[1:].lstrip()
            if key == "reply":
                if not rest.startswith('"'):
                    return ""
                try:
                    reply, _ = decoder.raw_decode(rest)
                    return reply.encode("utf-8", errors="ignore").decode()[:1200]
                except ValueError:
                    # An incomplete escape must wait for the next provider chunk.
                    for length in range(len(rest), 0, -1):
                        try:
                            reply = json.loads(rest[:length] + '"')
                            return reply.encode("utf-8", errors="ignore").decode()[
                                :1200
                            ]
                        except ValueError:
                            continue
                    return ""
            _, offset = decoder.raw_decode(rest)
            rest = rest[offset:].lstrip()
            if not rest.startswith(","):
                return ""
            rest = rest[1:].lstrip()
        except (ValueError, TypeError):
            return ""
    return ""
