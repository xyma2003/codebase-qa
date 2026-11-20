"""CI only: read Bookworm fixture storage on Trixie and restore on the new image."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def verify_records(before, after, restore):
    for record, version in ((before, "12"), (after, "13")):
        distribution = record.get("distribution") or {}
        if record.get("python") != "3.12.13" or record.get("uid") != 10001 or record.get("system") != "Linux" or distribution.get("ID") != "debian" or distribution.get("VERSION_ID") != version:
            raise ValueError("跨底座检查的 Python、用户或发行版与基线不一致。")
    if tuple(map(int, after["sqlite"].split("."))) < (3, 46, 1):
        raise ValueError("新底座的 Python 未使用已检查的 SQLite 版本。")
    required = ("cross_owner_denied", "source_process_exited_before_backup", "original_path_replaced_for_readback", "source_snapshot_checked")
    if restore.get("status") != "stopped_service_restore_passed" or not all(restore.get(key) is True for key in required) or restore.get("native_turns_restored") != 2 or restore.get("vectors_restored") != 1 or restore.get("model_requests") != 0:
        raise ValueError("新底座的恢复或用户隔离检查不完整。")
    return {"status": "storage_platform_compatibility_passed", "before": before, "after": after,
            "old_data_read_on_new_image": True, "new_image_restore": restore, "real_model_requests": 0}


def main(baseline_image, image, output):
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    volume = "qa-storage-" + uuid.uuid4().hex
    containers = []
    volume_created = False

    def run(label, *command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=240)
        (output / (label + ".log")).write_text(result.stdout + result.stderr)
        if result.returncode:
            print(result.stdout + result.stderr, file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result.stdout.strip()

    def worker(label, selected_image, *arguments, script="check_restore.py"):
        identifier = run(label + "-create", "docker", "create", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=256m",
                         "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "python", "-e", "PYTHONPATH=/app",
                         "-v", volume + ":/fixture", "-v", str(ROOT / "scripts/check_restore.py") + ":/validation/check_restore.py:ro",
                         "-v", str(ROOT / "scripts/export_legacy_vectors.py") + ":/validation/export_legacy_vectors.py:ro",
                         "-v", str(ROOT / "scripts/import_vectors.py") + ":/validation/import_vectors.py:ro",
                         selected_image, "/validation/" + script, *arguments)
        containers.append(identifier)
        stdout = run(label, "docker", "start", "--attach", identifier)
        if run(label + "-exit", "docker", "inspect", "--format", "{{.State.ExitCode}}", identifier) != "0":
            raise RuntimeError("跨底座检查进程失败，请查看 " + label + ".log")
        return stdout

    try:
        run("volume", "docker", "volume", "create", volume)
        volume_created = True
        run("prepare", "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "--cap-add", "CHOWN",
            "--security-opt", "no-new-privileges", "--user", "0:0", "--entrypoint", "python", "-v", volume + ":/fixture", baseline_image,
            "-c", "import os; os.mkdir('/fixture/runtime'); os.chown('/fixture/runtime',10001,10001); os.chown('/fixture',10001,10001)")
        worker("seed", baseline_image, "--worker", "seed", "--root", "/fixture/runtime", "--expected", "/fixture/expected.json", "--runtime-output", "/fixture/seed-runtime.json")
        exported = json.loads(worker("legacy-export", baseline_image, "--source", "/fixture/runtime/chroma_db",
            "--output", "/fixture/legacy-vectors.json", "--service-stopped", script="export_legacy_vectors.py"))
        migration = json.loads(worker("sqlite-import", image, "/fixture/legacy-vectors.json", "--destination", "/fixture/runtime/chroma_db",
            "--sha256", exported["sha256"], "--service-stopped", script="import_vectors.py"))
        worker("verify", image, "--worker", "verify", "--root", "/fixture/runtime", "--expected", "/fixture/expected.json", "--runtime-output", "/fixture/verify-runtime.json")
        worker("restore", image, "--output", "/fixture/restore")
    finally:
        failing = sys.exc_info()[0] is not None
        collected = None
        if containers:
            collected = subprocess.run(["docker", "cp", containers[-1] + ":/fixture/.", str(output / "data")], capture_output=True, text=True, timeout=60)
            (output / "collection.log").write_text(collected.stdout + collected.stderr)
        for identifier in reversed(containers):
            result = subprocess.run(["docker", "rm", "-f", identifier], capture_output=True, text=True, timeout=30)
            (output / (identifier + "-cleanup.log")).write_text(result.stdout + result.stderr)
        if volume_created:
            result = subprocess.run(["docker", "volume", "rm", volume], capture_output=True, text=True, timeout=30)
            (output / "volume-cleanup.log").write_text(result.stdout + result.stderr)
        if collected is not None and collected.returncode and not failing:
            raise RuntimeError("跨底座检查记录收集失败：" + collected.stderr)
    data = output / "data"
    report = verify_records(json.loads((data / "seed-runtime.json").read_bytes()), json.loads((data / "verify-runtime.json").read_bytes()), json.loads((data / "restore/report.json").read_bytes()))
    if migration.get("vectors") != 1 or migration.get("exact_float32_readback") is not True or migration.get("embeddings_recomputed") != 0:
        raise ValueError("向量迁移未通过逐条验证。")
    report.update(baseline_image=baseline_image, image=image, migration=migration)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_image")
    parser.add_argument("image")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    main(args.baseline_image, args.image, args.output)
