"""Regression checks for country-name catalogue source hygiene."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


root = Path(__file__).resolve().parent.parent
catalogue = Path(tempfile.mkdtemp(prefix="verigo-name-catalogue-")) / "names.db"
subprocess.run([
    sys.executable, "scripts/build_name_catalog.py", "--output", str(catalogue),
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
