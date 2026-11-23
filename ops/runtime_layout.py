"""Verify the actual minimal Linux image; no model request or gate exemption."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from ops.dependency_audit import check_installed
from ops.os_packages import check_installed as check_os_packages


def verify(root=Path("/app")):
    manifest = json.loads((root / "deploy/runtime-layout.json").read_text())
    if manifest.get("schema") != 1 or sys.prefix != manifest["venv"] or os.getuid() != 10001:
        raise ValueError("运行镜像的虚拟环境或用户不符。")
    remaining = {name: shutil.which(name) for name in manifest["removed_runtime_commands"] if shutil.which(name)}
    if remaining or importlib.util.find_spec("pip") is not None or importlib.util.find_spec("chromadb") is not None:
        raise ValueError("运行镜像仍包含安装工具或 Chroma：" + json.dumps(remaining))
    if Path("/usr/local/lib/python3.12/site-packages/pip").exists() or os.access(sys.prefix, os.W_OK):
        raise ValueError("全局 pip 未移除或运行用户可改写依赖目录。")
    differences = check_installed(root / "deploy/runtime.lock", strict=True)
    if differences:
        raise ValueError("精简镜像的 Python 库存改变：" + json.dumps(differences))
    packages = check_os_packages(root / "deploy/os-security-minimums.json")
    import numpy as np
    import torch
    import sentence_transformers
    import tokenizers
    assert torch.version.cuda is None
    assert float((torch.tensor([[1., 2.]]) @ torch.tensor([[3.], [4.]])).item()) == 11.
    assert float((np.array([[1., 2.]]) @ np.array([[3.], [4.]])).item()) == 11.
    native = {}
    remote = Path(subprocess.check_output(["git", "--exec-path"], text=True).strip()) / "git-remote-https"
    for path in (Path(sys.executable), remote, Path(torch._C.__file__)):
        result = subprocess.run(["ldd", str(path)], capture_output=True, text=True, timeout=15)
        if result.returncode or "not found" in result.stdout + result.stderr:
            raise ValueError("必要原生依赖未完整加载：" + str(path) + result.stdout + result.stderr)
        native[str(path)] = result.stdout
    return {"status": "runtime_layout_verified", "prefix": sys.prefix, "uid": os.getuid(),
        "python_inventory_unchanged": True, "install_commands_absent": manifest["removed_runtime_commands"],
        "removed_os_packages": packages["removed_runtime_packages"], "cpu_native_math_checked": True,
        "sentence_transformers_imported": True, "tokenizers_imported": True,
        "native_linkage": native, "real_model_requests": 0, "real_embedding_model_evaluated": False,
        "public_release_allowed": False}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
