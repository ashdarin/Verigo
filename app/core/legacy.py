from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any

_MODULE_NAME = "验证8"
_module: ModuleType | None = None
_module_lock = threading.Lock()


def load_legacy_module() -> ModuleType:
    """Load the existing verifier without executing its CLI entry point."""
    global _module
    if _module is not None:
        return _module

    with _module_lock:
        if _module is not None:
            return _module
        module = importlib.import_module(_MODULE_NAME)
        _module = module
        return module


def create_verifier(max_workers: int) -> Any:
    module = load_legacy_module()
    verifier = module.DistributedEmailVerifier()
    verifier.set_max_processes(max_workers)
    return verifier


def load_persistent_cache() -> None:
    load_legacy_module().load_persistent_cache()


def save_persistent_cache() -> None:
    if _module is not None:
        _module.save_persistent_cache()
