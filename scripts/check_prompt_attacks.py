"""Offline forced-attack boundary tests, with a machine-readable receipt."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_prompt_attacks import CASE_FILE, CASES, PromptAttackTests


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []
    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id().split(".")[-1].split("_")[1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose an independent check directory")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PromptAttackTests)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    complete = result.wasSuccessful() and set(result.passed) == set(CASES) and result.testsRun == len(CASES)
    report = {"status":"application_boundaries_passed" if complete else "failed", "case_count":len(CASES),
              "passed_cases":sorted(result.passed), "real_model_requests":0, "model_attack_resistance_rate":None,
              "fixture_sha256":hashlib.sha256(CASE_FILE.read_bytes()).hexdigest(),
              "test_sha256":hashlib.sha256((ROOT/"tests/test_prompt_attacks.py").read_bytes()).hexdigest(),
              "scope":"Forced unsafe model choices through the actual SDK/tools; not a model obedience or quality measurement."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        file.write(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if complete else 1)
