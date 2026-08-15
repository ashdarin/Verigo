"""Guard the independent Outlook confirmation requests against serial execution."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.outlook_verifier import verify_outlook_via_microsoft


source = inspect.getsource(verify_outlook_via_microsoft)
assert "ThreadPoolExecutor" in source
assert "pool.submit(_timed_query, _ms_query_getcredtype, email)" in source
assert "pool.submit(_timed_query, _ms_query_odc, email)" in source

print("outlook verifier concurrency smoke: ok")
