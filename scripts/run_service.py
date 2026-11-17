"""Fail closed before starting a single invited-user service process."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.preflight import preflight

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        preflight(ROOT, os.environ.get("CODEBASE_QA_AUDIT_FILE", "/run/config/audit.json"),
                  ROOT / "deploy/runtime.lock", os.environ.get("CODEBASE_QA_PUBLIC_ORIGIN", ""))
        if os.getuid() == 0:
            raise ValueError("服务必须以非 root 用户运行。")
    except Exception as exc:
        # Configuration may contain secrets; don't echo exception contents.
        print("部署检查未通过，请核对依赖审计、登录、密钥和数据目录。", file=sys.stderr)
        sys.exit(1)
    if args.check:
        print("deployment_preflight_passed; real_model_and_oidc_not_tested")
    else:
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "streamlit", "run", "app.py", "--server.address", "0.0.0.0",
            "--server.port", "8501", "--server.headless", "true", "--server.enableCORS", "true", "--server.enableXsrfProtection", "true"])
