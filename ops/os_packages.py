"""Check targeted Debian security updates without replacing the full OS audit."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess


def load_targets(path):
    data = json.loads(Path(path).read_bytes())
    if data.get("schema") != 1 or data.get("distribution") not in ({"id": "debian", "version": "12"}, {"id": "debian", "version": "13"}):
        raise ValueError("系统包修复清单格式或发行版不匹配。")
    packages = data.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise ValueError("系统包修复清单为空。")
    for name, minimum in packages.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) or not isinstance(minimum, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~_-]*", minimum):
            raise ValueError("系统包名或最低版本无效。")
    findings = data.get("targeted_findings")
    if not isinstance(findings, list) or not findings or any(set(row) != {"package", "id"} or row["package"] not in packages or not isinstance(row["id"], str) or not row["id"] for row in findings):
        raise ValueError("系统包待修复公告清单无效。")
    if len({(row["package"], row["id"]) for row in findings}) != len(findings):
        raise ValueError("系统包待修复公告重复。")
    removed = data.get("removed_runtime_packages", [])
    if not isinstance(removed, list) or any(not isinstance(name, str) or name not in packages for name in removed) or len(set(removed)) != len(removed):
        raise ValueError("运行期移除包必须是原修复清单中的唯一包名。")
    return data


def check_installed(path, *, run=subprocess.run, os_release=None, phase="runtime"):
    targets = load_targets(path)
    system = platform.freedesktop_os_release() if os_release is None else os_release
    if (system.get("ID"), system.get("VERSION_ID")) != ("debian", targets["distribution"]["version"]):
        raise ValueError("系统包最低版本检查与清单指定的 Debian 版本不匹配。")
    if phase not in {"runtime", "build"}:
        raise ValueError("未知系统包检查阶段。")
    removed = targets.get("removed_runtime_packages", []) if phase == "runtime" else []
    packages = {name:value for name,value in targets["packages"].items() if name not in removed}
    result = run(["dpkg-query", "--show", "--showformat=${binary:Package}\t${db:Status-Status}\t${Version}\n", *sorted(packages)], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError("系统包查询失败：" + result.stderr)
    installed = {}
    for row in result.stdout.splitlines():
        name, status, version = row.split("\t")
        name = name.split(":")[0]
        if name in installed or status != "installed":
            raise ValueError("系统包未完整安装或查询结果重复。")
        installed[name] = version
    if set(installed) != set(packages):
        raise ValueError("系统包查询未覆盖全部修复目标。")
    if removed:
        inventory = run(["dpkg-query", "--show", "--showformat=${binary:Package}\t${db:Status-Status}\t${Version}\n"], capture_output=True, text=True, timeout=15)
        if inventory.returncode:
            raise ValueError("运行期包库存查询失败：" + inventory.stderr)
        for row in inventory.stdout.splitlines():
            name, status, _ = row.split("\t")
            if name.split(":")[0] in removed and status != "not-installed":
                raise ValueError("构建专用包仍在运行镜像：" + name)
    for name, minimum in packages.items():
        result = run(["dpkg", "--compare-versions", installed[name], "ge", minimum], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError(f"系统包 {name} 未达到最低修复版本 {minimum}，实际 {installed[name]}。" + result.stderr)
    return {"status": "targeted_package_versions_passed", "phase": phase, "removed_runtime_packages": removed,
        "manifest_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "installed": installed, "minimums": packages}


def check_scan(path, scan):
    targets = load_targets(path)
    system = scan.get("Metadata", {}).get("OS", {})
    results = [row for row in scan.get("Results", []) if row.get("Class") == "os-pkgs"]
    inventory = {package["Name"] for row in results for package in row.get("Packages", [])}
    removed = set(targets.get("removed_runtime_packages", []))
    if (system.get("Family") != "debian" or system.get("Name", "").split(".")[0] != targets["distribution"]["version"]
            or not (set(targets["packages"])-removed).issubset(inventory) or removed.intersection(inventory)):
        raise ValueError("系统扫描未覆盖当前发行版及全部修复目标包。")
    findings = [item for row in results for item in row.get("Vulnerabilities", [])]
    keys = {(row["PkgName"], row["VulnerabilityID"]) for row in findings}
    remaining = [row for row in targets["targeted_findings"] if (row["package"], row["id"]) in keys]
    if remaining:
        raise ValueError("指定系统公告仍未修复：" + json.dumps(remaining, ensure_ascii=False))
    return {"status": "targeted_os_findings_cleared", "manifest_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "targeted_findings_cleared": len(targets["targeted_findings"]), "remaining_records": len(findings), "remaining_unique_ids": len({row["VulnerabilityID"] for row in findings}), "release_allowed": not findings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[1] / "deploy/os-security-minimums.json")
    parser.add_argument("--scan", type=Path)
    parser.add_argument("--build", action="store_true", help="Check all build-time minimums before runtime-only packages are removed")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.scan and args.build:
        parser.error("--scan and --build cannot be combined")
    report = check_scan(args.manifest, json.loads(args.scan.read_bytes())) if args.scan else check_installed(args.manifest, phase="build" if args.build else "runtime")
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8") as file:
            file.write(output)
    print(output, end="")
