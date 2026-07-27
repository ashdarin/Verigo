from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


module_path = Path(__file__).resolve().parent.parent / "email" / "prospecting_experiment.py"
spec = importlib.util.spec_from_file_location("prospecting_experiment", module_path)
assert spec is not None and spec.loader is not None
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


contact = experiment.Contact(
    domain="https://www.porsche.com/about",
    first_name="Jörg",
    last_name="Müller",
    expected_email="joerg.mueller@porsche.com",
    role="Example only",
)
candidates = experiment.candidates_for_contact(contact, limit=8)
assert candidates[0].email == "jorg.muller@porsche.com"
assert candidates[0].profile_source == "legacy_domain_catalogue"
assert len({candidate.email for candidate in candidates}) == len(candidates)

labeled = experiment.Contact(
    domain="example.com",
    first_name="John",
    last_name="Smith",
    expected_email="john.smith@example.com",
)
unlabeled = experiment.Contact(
    domain="example.org",
    first_name="Jane",
    last_name="Doe",
)
manifest = experiment.build_manifest([labeled, unlabeled], limit_per_contact=4, max_candidates=8)
report = experiment.coverage_report([labeled, unlabeled], manifest)
assert report["contacts"] == 2
assert report["labeled_contacts"] == 1
assert report["matched_expected_emails"] == 1
assert report["coverage"] == 1.0
assert report["top_1_coverage"] == 1.0

reversed_one = experiment.Contact(
    domain="reverse.example",
    first_name="Emma",
    last_name="Reynolds",
    expected_email="reynolds.emma@reverse.example",
)
reversed_two = experiment.Contact(
    domain="reverse.example",
    first_name="Hannah",
    last_name="Graham",
    expected_email="graham.hannah@reverse.example",
)
calibration = experiment.leave_one_out_calibration([reversed_one, reversed_two], 8)
assert calibration["evaluated_contacts"] == 2
assert calibration["top_1_coverage"] == 1.0

with tempfile.TemporaryDirectory() as temp_dir:
    input_path = Path(temp_dir) / "contacts.csv"
    input_path.write_text(
        "domain,first_name,last_name,expected_email,role\n"
        "example.com,John,Smith,john.smith@example.com,\n",
        encoding="utf-8",
    )
    contacts = experiment.read_contacts(input_path)
    assert contacts == [labeled]

try:
    experiment.candidates_for_contact(
        experiment.Contact("example.com", "张", "伟"), limit=4
    )
except ValueError as exc:
    assert "Latin spelling" in str(exc)
else:
    raise AssertionError("A non-romanized name must not be guessed")

print("prospecting experiment smoke: ok")
