"""Compare exact OS package inventories; never equate fewer records to safety."""
from ops.os_triage import summarize, package_version


def inventory(raw):
    return {p["Name"]: package_version(p) for r in raw["Results"] if r.get("Class") == "os-pkgs" for p in r["Packages"]}


def compare(baseline, candidate, removed, updates=None):
    updates = updates or {}
    before, after = summarize(baseline), summarize(candidate)
    old, new = inventory(baseline), inventory(candidate)
    changes = {name: {"before": old.get(name), "after": new.get(name)} for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
    if set(removed) & set(updates):
        raise ValueError("系统包版本变化清单不合法。")
    for change in updates.values():
        if not isinstance(change, dict) or set(change) != {"before", "after"}:
            raise ValueError("系统包版本变化清单不合法。")
        if not all(isinstance(version, str) and version for version in change.values()):
            raise ValueError("系统包版本变化清单不合法。")
    expected = {name: {"before": old.get(name), "after": None} for name in removed}
    expected.update(updates)
    if (before["os"] != after["os"] or changes != expected
            or any(name in new or name not in old for name in removed)
            or any(name not in old or name not in new for name in updates)):
        raise ValueError("镜像对照出现目标以外的系统包或版本变化：" + str(changes))
    old_keys = {(f["package"], f["id"]) for g in before["groups"] for f in g["findings"]}
    new_keys = {(f["package"], f["id"]) for g in after["groups"] for f in g["findings"]}
    return {"schema": 1, "scope": "same_database_image_comparison", "baseline_image_id": before["image_id"],
        "candidate_image_id": after["image_id"], "package_changes": changes,
        "unchanged_os_packages": len(new) - len(updates), "baseline_records": before["record_count"], "candidate_records": after["record_count"],
        "baseline_ids": before["unique_advisory_ids"], "candidate_ids": after["unique_advisory_ids"],
        "removed_findings": [{"package":p,"id":i} for p,i in sorted(old_keys-new_keys)],
        "added_findings": [{"package":p,"id":i} for p,i in sorted(new_keys-old_keys)],
        "candidate_source_groups": after["source_groups"], "public_release_allowed": False}
