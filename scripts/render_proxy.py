import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def render(hostname):
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", hostname):
        raise ValueError("主机名无效。")
    return (ROOT / "deploy/nginx.conf.template").read_text().replace("__HOSTNAME__", hostname)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("hostname")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as file:
        file.write(render(args.hostname))
