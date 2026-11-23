import copy
import unittest
from ops.os_triage import summarize, markdown


class OsTriageTests(unittest.TestCase):
    def setUp(self):
        self.raw = {"Metadata": {"OS": {"Family": "debian", "Name": "13.7"}, "ImageID": "sha256:" + "a"*64},
            "Results": [{"Class": "os-pkgs", "Packages": [
                {"Name": "mount", "SrcName": "util-linux", "Version": "2.41.5"},
                {"Name": "libmount1", "SrcName": "util-linux", "Version": "2.41.5"},
                {"Name": "libc6", "SrcName": "glibc", "Version": "2.41"}],
                "Vulnerabilities": [{"VulnerabilityID": "CVE-fixture", "PkgName": name,
                    "InstalledVersion": "2.41.5", "Severity": "HIGH"} for name in ("mount", "libmount1")]}]}

    def test_one_advisory_in_two_binary_packages_is_one_source_group(self):
        report = summarize(self.raw)
        self.assertEqual((report["record_count"], report["unique_advisory_ids"], report["source_groups"]), (2, 1, 1))
        self.assertEqual(report["groups"][0]["installed_packages"], ["libmount1", "mount"])
        self.assertFalse(report["release_gate_changed"])
        self.assertFalse(report["exploitability_assessed"])
        self.assertEqual(report["groups"][0]["review_status"], "pending")

    def test_unknown_and_fixed_version_remain_review_items(self):
        self.raw["Results"][0]["Vulnerabilities"].append({"VulnerabilityID": "TEMP-fixture", "PkgName": "libc6",
            "InstalledVersion": "2.41", "Severity": "UNKNOWN", "FixedVersion": "2.42"})
        report = summarize(self.raw)
        self.assertEqual(report["record_count"], 3)
        self.assertEqual(report["severity"]["UNKNOWN"], 1)
        self.assertEqual(report["records_with_fixed_version"], 1)
        self.assertEqual(report["groups"][1]["next_action"], "verify_stable_update")
        self.assertIn("UNKNOWN", markdown(report))

    def test_missing_inventory_or_wrong_installed_version_cannot_be_classified(self):
        for change in ("missing", "wrong_version", "ambiguous"):
            data = copy.deepcopy(self.raw)
            row = data["Results"][0]
            if change == "missing": row["Packages"].pop(0)
            elif change == "wrong_version": row["Packages"][0]["Version"] = "9"
            else: row["Packages"].append(row["Packages"][0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                summarize(data)

    def test_invalid_severity_identity_and_source_are_not_silently_discarded(self):
        for field, value in (("Severity", "unrecognized"), ("VulnerabilityID", "bad\nvalue")):
            data = copy.deepcopy(self.raw)
            data["Results"][0]["Vulnerabilities"][0][field] = value
            with self.assertRaises(ValueError): summarize(data)
        self.raw["Results"][0]["Packages"][0].pop("SrcName")
        with self.assertRaises(ValueError): summarize(self.raw)

    def test_incomplete_or_non_os_results_are_rejected(self):
        for data in ({}, dict(self.raw, Results=[]), dict(self.raw, Metadata={})):
            with self.assertRaises(ValueError): summarize(data)

    def test_empty_findings_do_not_authorize_release(self):
        self.raw["Results"][0]["Vulnerabilities"] = []
        report = summarize(self.raw)
        self.assertEqual(report["record_count"], 0)
        self.assertEqual(report["scope"], "review_only")
        self.assertNotIn("release_allowed", report)

    def test_epoch_and_debian_release_are_part_of_the_inventory_binding(self):
        row = self.raw["Results"][0]
        row["Packages"][0].update(Epoch=1, Release="0+deb13u1")
        row["Vulnerabilities"][0]["InstalledVersion"] = "1:2.41.5-0+deb13u1"
        report = summarize(self.raw)
        self.assertEqual(report["groups"][0]["findings"][0]["installed"], "1:2.41.5-0+deb13u1")
