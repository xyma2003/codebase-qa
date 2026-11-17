"""Query public OSV data for exact pinned packages; never resolve or upgrade."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import tempfile
import urllib.request

API = "https://api.osv.dev/v1"


def pins(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.+!_-]+)(?:\s*\\)?", line)
        if not match:
            raise ValueError("审计输入必须仅包含精确版本和哈希。")
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in result:
            raise ValueError("审计输入包名重复。")
        result[name] = match[2]
    if not result:
        raise ValueError("审计输入为空。")
    return result


def request(endpoint, payload=None):
    raw = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + endpoint, data=raw, headers={"Content-Type": "application/json", "User-Agent": "codebase-qa-dependency-audit"})
    with urllib.request.urlopen(req, timeout=25) as response:
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("漏洞库响应过大。")
        return json.loads(data)


def audit(path, output, *, transport=request):
    path, output = Path(path), Path(output)
    if output.exists():
        raise ValueError("审计记录已存在，请使用新目录。")
    packages = pins(path)
    queries = [{"package": {"ecosystem": "PyPI", "name": name}, "version": version.split("+")[0]} for name, version in packages.items()]
    output.mkdir(parents=True, mode=0o700)
    report = {"schema": 1, "source": API, "queried_at": datetime.now(timezone.utc).isoformat(),
              "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "packages": packages,
              "complete": False, "findings": [], "requests": [], "error": None}
    try:
        pending = list(enumerate(queries))
        ids = {}
        for page in range(16):
            if not pending:
                break
            payload = {"queries": [query for _, query in pending]}
            response = transport("/querybatch", payload)
            report["requests"].append({"endpoint": "/querybatch", "input": payload, "response": response})
            rows = response.get("results")
            if not isinstance(rows, list) or len(rows) != len(pending):
                raise ValueError("漏洞库未返回全部查询结果。")
            next_pending = []
            for (index, query), row in zip(pending, rows):
                if not isinstance(row, dict) or row.get("error"):
                    raise ValueError("漏洞库返回错误结果。")
                for vulnerability in row.get("vulns", []):
                    identity = vulnerability["id"]
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", identity):
                        raise ValueError("漏洞编号无效。")
                    ids.setdefault(identity, set()).add(index)
                if row.get("next_page_token"):
                    next_pending.append((index, dict(query, page_token=row["next_page_token"])))
            pending = next_pending
        if pending:
            raise ValueError("漏洞库分页未完整读取。")
        names = list(packages)
        for identity in sorted(ids):
            details = transport("/vulns/" + identity)
            report["requests"].append({"endpoint": "/vulns/" + identity, "response": details})
            if details.get("id") != identity:
                raise ValueError("漏洞详情编号不符。")
            if details.get("withdrawn"):
                continue
            report["findings"].append({"id": identity, "packages": [names[index] for index in sorted(ids[identity])],
                "aliases": details.get("aliases", []), "summary": details.get("summary", ""),
                "severity": details.get("severity", []), "references": details.get("references", [])})
        report["complete"] = True
    except Exception as exc:
        # Preserve failure evidence and fail closed. No automatic retry.
        report["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    (output / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def check_installed(path):
    expected = pins(path)
    installed = {re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(): d.version for d in importlib.metadata.distributions()}
    return {name: {"expected": version, "installed": installed.get(name)} for name, version in expected.items() if installed.get(name) != version}


def groups(findings):
    """Merge advisory aliases; retain every source ID and affected package."""
    result = []
    for finding in findings:
        ids = {finding["id"], *finding["aliases"]}
        related = [group for group in result if ids.intersection(group["ids"])]
        packages = set(finding["packages"])
        for group in related:
            ids.update(group["ids"])
            packages.update(group["packages"])
            result.remove(group)
        result.append({"ids": sorted(ids), "packages": sorted(packages)})
    return sorted(result, key=lambda row: row["ids"])


def release_gate(report, lock, *, now=None):
    """An operator may collect findings; deployment must never ignore them."""
    now = now or datetime.now(timezone.utc)
    if (report.get("schema") != 1 or report.get("complete") is not True or report.get("error") or
            report.get("source") != API or report.get("packages") != pins(lock) or
            report.get("input_sha256") != hashlib.sha256(Path(lock).read_bytes()).hexdigest()):
        raise ValueError("依赖审计不完整或与部署锁不符。")
    try:
        age = (now - datetime.fromisoformat(report["queried_at"])).total_seconds()
        if age < -60 or age > 7 * 86400:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("依赖审计记录已过期或时间无效。") from None
    pending = iter(report.get("requests", []))
    def replay(endpoint, payload=None):
        record = next(pending)
        if record["endpoint"] != endpoint or (payload is not None and record.get("input") != payload):
            raise ValueError("审计查询与记录不符。")
        return record["response"]
    with tempfile.TemporaryDirectory() as directory:
        checked = audit(lock, Path(directory) / "replay", transport=replay)
    if (not checked["complete"] or checked["findings"] != report.get("findings") or next(pending, None) is not None):
        raise ValueError("审计响应不完整或摘要被修改。")
    if report.get("findings") != []:
        raise ValueError("依赖仍有未处理公告，禁止上线。")
