"""Ensure public preview endpoints never start discovery for private hosts."""
from __future__ import annotations

from fastapi import HTTPException
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.api.routes as routes


original_discover_related = routes.discover_related
original_cached = routes.domain_preview_store.get
original_public_check = routes._has_only_public_addresses
try:
    routes.domain_preview_store.get = lambda _domain: None
    routes._has_only_public_addresses = lambda _domain: False
    routes.discover_related = lambda _domain: (_ for _ in ()).throw(
        AssertionError("private hosts must not reach related-domain discovery")
    )
    try:
        routes.domain_relations("internal.localhost")
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("private hosts must be rejected")
finally:
    routes.discover_related = original_discover_related
    routes.domain_preview_store.get = original_cached
    routes._has_only_public_addresses = original_public_check

print("domain preview SSRF smoke: ok")
