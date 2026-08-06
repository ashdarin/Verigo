from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

temp_dir = Path(tempfile.mkdtemp(prefix="verigo-prospecting-claim-"))
os.environ["VERIGO_DATABASE_PATH"] = str(temp_dir / "verigo.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.prospecting import ProspectingCandidate
from app.db.prospecting import ProspectingStore


store = ProspectingStore()
candidates = [
    ProspectingCandidate("alice@example.test", "role", "first.last", 1, "smoke", "alice"),
    ProspectingCandidate("bob@example.test", "role", "first.last", 2, "smoke", "bob"),
]

def claim():
    return store.allocate_candidates("example.test", candidates, 2)

with ThreadPoolExecutor(max_workers=2) as pool:
    first, second = [future.result() for future in [pool.submit(claim), pool.submit(claim)]]

assert sorted(len(result[1]) for result in (first, second)) == [0, 2]
assert len({item.email for item in first[1] + second[1]}) == 2
store.release_candidate_claim(first[0])
store.release_candidate_claim(second[0])
print("prospecting claim smoke: ok")
