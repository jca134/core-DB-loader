"""
Where the optional Anthropic API key for the query page comes from.

Order: the ANTHROPIC_API_KEY environment variable, else a key saved from the
query page. A saved key lives in the user's home directory --
~/.config/filovirus-graph/anthropic_api_key, readable only by its owner --
never inside the repository, so it cannot be committed.
"""

import os
from pathlib import Path

import anthropic

KEY_FILE = Path.home() / ".config" / "filovirus-graph" / "anthropic_api_key"


def current_key() -> tuple[str | None, str | None]:
    """(key, where it came from: 'environment' | 'saved' | None)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"], "environment"
    try:
        key = KEY_FILE.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None, None
    return (key, "saved") if key else (None, None)


def save_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create with owner-only permissions from the start; no window where it's world-readable.
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip() + "\n")
    os.chmod(KEY_FILE, 0o600)


def forget_key() -> bool:
    try:
        KEY_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def check_key(key: str, model: str) -> tuple[anthropic.Anthropic | None, str | None]:
    """
    (client, None) if the key works for `model`, else (None, a message for the
    user). One cheap metadata call, no tokens spent.
    """
    key = (key or "").strip()
    if not key.startswith("sk-ant-"):
        return None, "That doesn't look like an Anthropic API key (they start with 'sk-ant-')."
    client = anthropic.Anthropic(api_key=key)
    try:
        client.models.retrieve(model)
    except anthropic.AuthenticationError:
        return None, "Anthropic rejected this key. Check it was copied whole and hasn't been revoked."
    except anthropic.PermissionDeniedError:
        return None, f"The key works but has no access to {model}. Check billing / credit in the Anthropic console."
    except anthropic.APIConnectionError:
        return None, "Couldn't reach Anthropic to check the key. Are you online?"
    return client, None
