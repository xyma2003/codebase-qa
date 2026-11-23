import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ops.os_packages import check_installed, check_scan, load_targets


class OsPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "targets.json"
        self.data = {"schema": 1, "distribution": {"id": "debian", "version": "12"},
                     "packages": {"apt": "2.6.2", "libssl3": "3.0.22-1~deb12u2"},
                     "targeted_findings": [{"package": "apt", "id": "CVE-fixture"}]}
        self.path.write_text(json.dumps(self.data))
        self.system = {"ID": "debian", "VERSION_ID": "12"}
        self.scan = {"Metadata": {"OS": {"Family": "debian", "Name": "12.15"}}, "Results": [
            {"Class": "os-pkgs", "Packages": [{"Name": "apt"}, {"Name": "libssl3"}],
             "Vulnerabilities": [{"PkgName": "libssl3", "VulnerabilityID": "CVE-other"}]}]}

    @staticmethod
    def query(command, **kwargs):
        if command[0] == "dpkg-query":
            return subprocess.CompletedProcess(command, 0, "apt\tinstalled\t2.6.2\nlibssl3:amd64\tinstalled\t3.0.22-1~deb12u2\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_installed_targets_are_all_queried_and_compared_by_dpkg(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return self.query(command, **kwargs)
        report = check_installed(self.path, run=run, os_release=self.system)
        self.assertEqual(report["installed"], self.data["packages"])
        self.assertEqual(commands[1:], [["dpkg", "--compare-versions", version, "ge", version] for version in self.data["packages"].values()])

    def test_old_versions_and_command_errors_cannot_pass(self):
        for code in (1, 2):
            def run(command, **kwargs):
                return self.query(command, **kwargs) if command[0] == "dpkg-query" else subprocess.CompletedProcess(command, code, "", "original comparison error")
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, "original comparison error"):
                check_installed(self.path, run=run, os_release=self.system)

    def test_failed_or_partial_inventory_is_rejected_with_original_error(self):
        for result in [(2, "", "original query error"), (0, "apt\tinstalled\t2.6.2\n", ""),
                       (0, "apt\tconfig-files\t2.6.2\n", "")]:
            def run(command, **kwargs):
                return subprocess.CompletedProcess(command, *result)
            with self.subTest(result=result), self.assertRaises(ValueError):
                check_installed(self.path, run=run, os_release=self.system)

    def test_wrong_distribution_or_invalid_targets_are_rejected(self):
        with self.assertRaises(ValueError):
            check_installed(self.path, os_release={"ID": "ubuntu", "VERSION_ID": "24.04"})
        for change in ({"packages": {}}, {"packages": {"--bad": "1"}},
                       {"targeted_findings": self.data["targeted_findings"] * 2}):
            data = dict(self.data, **change)
            self.path.write_text(json.dumps(data))
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_targets(self.path)

    def test_targeted_clearance_retains_other_findings_and_release_block(self):
        report = check_scan(self.path, self.scan)
        self.assertEqual(report["targeted_findings_cleared"], 1)
        self.assertEqual(report["remaining_records"], 1)
        self.assertFalse(report["release_allowed"])

    def test_scan_must_cover_targets_and_reject_remaining_targeted_findings(self):
        for change in ("missing_inventory", "wrong_os", "still_vulnerable"):
            scan = copy.deepcopy(self.scan)
            if change == "missing_inventory":
                scan["Results"][0]["Packages"].pop()
            elif change == "wrong_os":
                scan["Metadata"]["OS"]["Name"] = "13"
            else:
                scan["Results"][0]["Vulnerabilities"].append({"PkgName": "apt", "VulnerabilityID": "CVE-fixture"})
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_scan(self.path, scan)

    def test_trixie_manifest_requires_trixie_inventory_and_retains_critical_targets(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "deploy/os-security-minimums.json").read_text())
        self.assertEqual(data["distribution"], {"id": "debian", "version": "13"})
        self.assertTrue({("libsqlite3-0", "CVE-2025-7458"), ("zlib1g", "CVE-2023-45853")}.issubset({(r["package"], r["id"]) for r in data["targeted_findings"]}))
        self.path.write_text(json.dumps(data))
        scan = {"Metadata": {"OS": {"Family": "debian", "Name": "13.6"}}, "Results": [{"Class": "os-pkgs", "Packages": [{"Name": n} for n in data["packages"] if n not in data.get("removed_runtime_packages", [])], "Vulnerabilities": []}]}
        self.assertEqual(check_scan(self.path, scan)["targeted_findings_cleared"], 40)
        scan["Metadata"]["OS"]["Name"] = "12.15"
        with self.assertRaises(ValueError):
            check_scan(self.path, scan)

    def test_runtime_requires_absence_and_build_retains_old_minimums(self):
        self.data["removed_runtime_packages"] = ["apt"]
        self.path.write_text(json.dumps(self.data))
        def run(command, **kwargs):
            if command[0] == "dpkg-query":
                return subprocess.CompletedProcess(command, 0, "libssl3\tinstalled\t3.0.22-1~deb12u2\n", "")
            return self.query(command, **kwargs)
        report = check_installed(self.path, run=run, os_release=self.system)
        self.assertEqual(report["removed_runtime_packages"], ["apt"])
        def reintroduced(command, **kwargs):
            if command[0] == "dpkg-query" and len(command) > 3:
                return run(command, **kwargs)
            return self.query(command, **kwargs)
        with self.assertRaisesRegex(ValueError, "构建专用包仍在"):
            check_installed(self.path, run=reintroduced, os_release=self.system)
        self.assertIn("apt", check_installed(self.path, run=self.query, os_release=self.system, phase="build")["installed"])
        with self.assertRaises(ValueError):
            check_scan(self.path, self.scan)
        self.scan["Results"][0]["Packages"] = [{"Name":"libssl3"}]
        self.assertEqual(check_scan(self.path, self.scan)["remaining_records"], 1)

    def test_removal_manifest_cannot_hide_unknown_or_repeated_packages(self):
        for value in (["unknown-package"], ["apt", "apt"], "apt"):
            self.data["removed_runtime_packages"] = value
            self.path.write_text(json.dumps(self.data))
            with self.assertRaises(ValueError): load_targets(self.path)


if __name__ == "__main__":
    unittest.main()
