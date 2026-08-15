from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db.jobs import JobStore


original_limit = settings.qq_worker_max_workers
object.__setattr__(settings, "qq_worker_max_workers", 6)
try:
    assert JobStore._scheduler_mx_key("first@qq.com") == "qq"
    assert JobStore._scheduler_mx_key("second@vip.qq.com") == "qq"
    assert JobStore._scheduler_mx_key("third@foxmail.com") == "qq"
    assert JobStore._scheduler_key_for_mx_host("mx1.qq.com.") == "qq"
    assert JobStore._scheduler_key_for_mx_host("mx2.foxmail.com.") == "qq"
    assert JobStore._scheduler_mx_capacity("qq") == 6
    assert JobStore._scheduler_profile_bounds("qq") == (1, 6)
finally:
    object.__setattr__(settings, "qq_worker_max_workers", original_limit)

print("QQ scheduler smoke: ok")
