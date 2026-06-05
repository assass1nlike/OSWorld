from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import dotenv

from desktop_env.providers.base import VMManager

dotenv.load_dotenv()


def _snapshot_env_suffix(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def _load_image_map() -> dict[str, Any]:
    raw = os.getenv("VOLCENGINE_IMAGE_ID_MAP") or os.getenv("VOLCENGINE_IMAGE_ID_MAP_JSON")
    if not raw:
        return {}

    if raw.lstrip().startswith("{"):
        text = raw
    else:
        possible_path = Path(raw)
        if possible_path.exists():
            text = possible_path.read_text(encoding="utf-8")
        else:
            text = raw

    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("VOLCENGINE_IMAGE_ID_MAP must be a JSON object or a path to one")
    return data


class VolcengineVMManager(VMManager):
    """Resolve Engiworld task snapshots to Volcengine ECS image ids.

    Supported resolution order:
      1. snapshot already looks explicit: image-xxx, image:xxx, or volcengine:xxx
      2. VOLCENGINE_IMAGE_ID_MAP JSON, with either {"snapshot": "image-xxx"} or
         {"Windows": {"snapshot": "image-xxx"}, "Ubuntu": {...}}
      3. per-snapshot env var, e.g. VOLCENGINE_IMAGE_ID_EAGLE_7_7_0=image-xxx
      4. VOLCENGINE_IMAGE_ID fallback for single-image experiments
    """

    def resolve_image(self, snapshot: str, os_type: str, **kwargs) -> str:
        snapshot = str(snapshot)
        os_type = str(os_type or "")

        for prefix in ("image:", "volcengine:"):
            if snapshot.startswith(prefix):
                return snapshot[len(prefix):]
        if snapshot.startswith("image-"):
            return snapshot

        image_map = _load_image_map()
        for os_key in (os_type, os_type.lower(), os_type.upper()):
            nested = image_map.get(os_key)
            if isinstance(nested, dict) and snapshot in nested:
                return str(nested[snapshot])

        for key in (
            snapshot,
            f"{os_type}:{snapshot}",
            f"{os_type.lower()}:{snapshot}" if os_type else "",
        ):
            if key and key in image_map:
                return str(image_map[key])

        suffixes = [
            _snapshot_env_suffix(snapshot),
            _snapshot_env_suffix(f"{os_type}_{snapshot}") if os_type else "",
        ]
        for suffix in suffixes:
            if not suffix:
                continue
            value = os.getenv(f"VOLCENGINE_IMAGE_ID_{suffix}")
            if value:
                return value

        fallback = os.getenv("VOLCENGINE_IMAGE_ID")
        if fallback:
            return fallback

        raise EnvironmentError(
            f"Cannot resolve Volcengine image id for snapshot={snapshot!r}, os_type={os_type!r}. "
            "Set VOLCENGINE_IMAGE_ID_MAP, VOLCENGINE_IMAGE_ID_<SNAPSHOT>, or VOLCENGINE_IMAGE_ID."
        )
