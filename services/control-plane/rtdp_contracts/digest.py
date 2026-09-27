"""Canonical digests for contract assets.

Digest = sha256 over canonical JSON (sorted keys, no whitespace ambiguity).
Every runtime artifact references these digests; content is immutable per
(owner_scope, kind, asset_id, version).
"""

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def canonical_digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj)).hexdigest()
