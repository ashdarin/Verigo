from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.discovery import PERSONAL_LOCAL_FORMATS, candidate_emails


assert len(PERSONAL_LOCAL_FORMATS) == 25

candidates = candidate_emails("Jane", "Smith", "example.com")
assert len(candidates) == 25
assert len(candidates) == len(set(candidates))
assert candidates[0] == "jane.smith@example.com"
assert "smith.j@example.com" in candidates
assert "j.s@example.com" in candidates

# The function must gracefully de-duplicate formats that collapse for short
# names without returning the same address twice.
short_name_candidates = candidate_emails("A", "B", "example.com")
assert len(short_name_candidates) == len(set(short_name_candidates))
