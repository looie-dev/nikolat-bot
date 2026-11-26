"""Atomic local JSON state persistence with optional Upstash Redis REST backup."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


def _get_upstash_config() -> Optional[Tuple[str, str]]:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if url and token:
        return url, token
    return None


def _remote_get(key: str) -> Optional[Dict[str, Any]]:
    cfg = _get_upstash_config()
    if not cfg or not key:
        return None
    url, token = cfg
    endpoint = f"{url}/get/{urllib.parse.quote(key, safe='')}"
    req = urllib.request.Request(
        endpoint,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        raw = payload.get("result")
        if not raw:
            return None
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning("Remote state fetch (%s) failed: %s", key, exc)
        return None


def _remote_set(key: str, data: Dict[str, Any]) -> None:
    cfg = _get_upstash_config()
    if not cfg or not key:
        return
    url, token = cfg
    endpoint = f"{url}/set/{urllib.parse.quote(key, safe='')}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        logger.warning("Remote state save (%s) failed: %s", key, exc)


def load_json_state(
    path: Path,
    default_factory: Callable[[], Dict[str, Any]],
    remote_key: str = "",
) -> Dict[str, Any]:
    """Load JSON state from disk, falling back to optional cloud KV then default_factory."""
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except Exception as exc:
            logger.warning("Corrupt state file %s ignored: %s", path, exc)

    if remote_key:
        remote = _remote_get(remote_key)
        if remote is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_name(f"{path.name}.tmp")
                tmp_path.write_text(json.dumps(remote, indent=2, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp_path, path)
            except Exception as exc:
                logger.warning("Failed to cache remote state %s locally: %s", path, exc)
            return remote

    return default_factory()


def save_json_state(path: Path, data: Dict[str, Any], remote_key: str = "") -> None:
    """Atomically write JSON state to disk and optionally mirror to cloud KV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)
    if remote_key:
        _remote_set(remote_key, data)
