from __future__ import annotations

import re
from typing import Any


def is_temporary_smtp_452(result: dict[str, Any]) -> bool:
    detail = " ".join(
        str(result.get(field) or "") for field in ("smtp_result", "message")
    )
    return bool(re.search(r"\b452\b", detail))
