"""Regression checks for country-name catalogue source hygiene."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


root = Path(__file__).resolve().parent.parent
temp_dir = Path(tempfile.mkdtemp(prefix="verigo-name-catalogue-"))
source = temp_dir / "source"
source.mkdir()
# Keep the test hermetic: the production corpus is deliberately ignored by Git.
(source / "Chinese_Names_Corpus.fixture.txt").write_text(
    "\u738b\u4f1f\n\u674e\u5a1c\n\u5f20\u5b50\u8c6a\n", encoding="utf-8"
)
catalogue = temp_dir / "names.db"
subprocess.run([
    sys.executable, "scripts/build_name_catalog.py", "--source", str(source), "--output", str(catalogue),
    "--report", str(temp_dir / "report.json"),
    "--countries", "CN", "--replace",
], cwd=root, check=True)

with sqlite3.connect(catalogue) as connection:
    values = {row[0] for row in connection.execute("SELECT romanized FROM name_entries")}
    assert {"wang", "li", "zhang"}.issubset(values)
    assert not {"lmm", "pvcedgebanding", "ledmodulemanufacturer", "filtracion"} & values
    name_lengths = {
        row[0] for row in connection.execute(
            "SELECT DISTINCT name_characters FROM name_entries WHERE country='CN' AND kind='given'"
        )
    }
    assert {1, 2}.issubset(name_lengths)
print("name catalogue smoke: ok")
