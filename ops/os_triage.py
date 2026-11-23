"""Group the complete OS scan for review. This module cannot grant release."""
from collections import Counter
import re

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


def package_version(package, prefix=""):
    epoch = package.get(prefix + "Epoch", 0)
    version, release = package.get(prefix + "Version", ""), package.get(prefix + "Release", "")
    return (str(epoch) + ":" if epoch else "") + version + ("-" + release if release else "")


def summarize(raw):
    system = raw.get("Metadata", {}).get("OS", {})
    image_id = raw.get("Metadata", {}).get("ImageID", "")
    results = [row for row in raw.get("Results", []) if row.get("Class") == "os-pkgs"]
    if system.get("Family") != "debian" or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or not results:
        raise ValueError("缺少完整 Debian 镜像扫描。")
    groups, records = {}, []
    for result in results:
        inventory = {}
        for package in result.get("Packages", []):
            name, source = package.get("Name", ""), package.get("SrcName", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", source) or not package.get("Version"):
                raise ValueError("系统包库存缺少包名、源包或版本。")
            if name in inventory:
                raise ValueError("系统包库存存在重复名称。")
            inventory[name] = package
        if not inventory:
            raise ValueError("系统包库存为空，不能完成影响归类。")
        for finding in result.get("Vulnerabilities", []):
            name = finding.get("PkgName")
            package = inventory.get(name)
            severity = finding.get("Severity")
            identity = finding.get("VulnerabilityID")
            if package is None or package_version(package) != finding.get("InstalledVersion"):
                raise ValueError("漏洞命中与实际包库存不一致。")
            if severity not in SEVERITIES or not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", identity):
                raise ValueError("漏洞命中缺少有效编号或级别。")
            source = package["SrcName"]
            row = {"id": identity, "package": name, "source_package": source,
                "installed": package_version(package), "source_version": package_version(package, "Src"),
                "severity": severity, "fixed_version": finding.get("FixedVersion", ""),
                "status": finding.get("Status", "unknown"), "primary_url": finding.get("PrimaryURL", ""),
                "title": finding.get("Title", ""), "exploitability": "not_assessed"}
            records.append(row)
            groups.setdefault(source, []).append(row)
    grouped = []
    for source, rows in groups.items():
        counts = Counter(row["severity"] for row in rows)
        fixes = [row for row in rows if row["fixed_version"]]
        action = "verify_stable_update" if fixes else "review_runtime_exposure"
        if source == "apt":
            action = "assess_removing_installation_tools"
        if source == "util-linux":
            action = "verify_privilege_isolation_and_vendor_patch"
        grouped.append({"source_package": source, "record_count": len(rows),
            "unique_ids": sorted({row["id"] for row in rows}),
            "installed_packages": sorted({row["package"] for row in rows}),
            "severity": {key: counts[key] for key in SEVERITIES},
            "records_with_fixed_version": len(fixes), "next_action": action,
            "review_status": "pending", "findings": rows})
    grouped.sort(key=lambda row: (-row["severity"]["CRITICAL"], -row["severity"]["HIGH"], -row["record_count"], row["source_package"]))
    counts = Counter(row["severity"] for row in records)
    return {"schema": 1, "scope": "review_only", "image_id": image_id, "os": system,
        "scan_created_at": raw.get("CreatedAt"), "record_count": len(records),
        "unique_advisory_ids": len({row["id"] for row in records}), "source_groups": len(grouped),
        "severity": {key: counts[key] for key in SEVERITIES},
        "records_with_fixed_version": sum(bool(row["fixed_version"]) for row in records),
        "exploitability_assessed": False, "release_gate_changed": False, "groups": grouped}


def markdown(report):
    lines = ["# 系统公告归类", "", f"镜像：`{report['image_id']}`", "",
        f"完整保留 {report['record_count']} 条命中、{report['unique_advisory_ids']} 个公告编号，归为 {report['source_groups']} 组源包。",
        "实际可利用性尚未逐项评估；无自动豁免，发布规则保持。UNKNOWN 项保留待核查。", "",
        "| 来源组件 | 命中条数 | 编号数 | High / Critical | 扫描给出修复版本的记录 | 下一步 |",
        "| --- | ---: | ---: | ---: | ---: | --- |"]
    actions = {"verify_stable_update": "核对稳定修复包", "review_runtime_exposure": "核对实际调用与触发条件",
        "assess_removing_installation_tools": "评估移除安装工具", "verify_privilege_isolation_and_vendor_patch": "复核权限隔离及官方补丁"}
    for group in report["groups"]:
        lines.append(f"| {group['source_package']} | {group['record_count']} | {len(group['unique_ids'])} | {group['severity']['HIGH']} / {group['severity']['CRITICAL']} | {group['records_with_fixed_version']} | {actions[group['next_action']]} |")
    return "\n".join(lines) + "\n"
