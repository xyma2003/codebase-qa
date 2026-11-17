"""Copy only the verified public model files; no token files or network calls."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.preflight import verify_embeddings


def seed(source, destination, manifest):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    verify_embeddings(source, manifest)
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    try:
        records = json.loads(Path(manifest).read_text())["files"]
        for name in records:
            target = destination / name
            if not target.absolute().is_relative_to(destination) or ".." in Path(name).parts:
                raise ValueError("模型缓存路径无效。")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
            target.chmod(0o644)
        verify_embeddings(destination, manifest)
        return {"files": len(records), "network_requests": 0, "tokens_copied": False}
    except BaseException:
        shutil.rmtree(destination)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(seed(args.source, args.destination, ROOT / "deploy/embedding-cache.json")))
