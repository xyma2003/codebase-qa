"""Experimental supplementation only; the production chunker is unchanged."""

from collections import defaultdict
import hashlib

from indexer.chunker import CodeChunk, _sliding_window
from indexer.source_reader import read_source_bytes


def module_supplements(snapshot, chunks):
    files = defaultdict(list)
    for chunk in chunks:
        if chunk.file_path.endswith(".py"):
            files[chunk.file_path].append(chunk)
    additional = []
    for name, existing in sorted(files.items()):
        _, data = read_source_bytes(snapshot.root, name)
        if hashlib.sha256(data).hexdigest() != snapshot.files[name]["sha256"]:
            raise ValueError("Source changed while supplementing module chunks")
        lines = data.decode("utf-8", errors="ignore").splitlines()
        covered = set(number for chunk in existing for number in range(chunk.start_line, chunk.end_line + 1))
        line = 1
        while line <= len(lines):
            if line in covered:
                line += 1
                continue
            start = line
            while line <= len(lines) and line not in covered:
                line += 1
            content = "\n".join(lines[start-1:line-1])
            if not content.strip():
                continue
            for chunk in _sliding_window(content, name):
                if chunk.content.strip():
                    additional.append(CodeChunk(chunk.content, name, start+chunk.start_line-1, start+chunk.end_line-1,
                                                "module", name))
    return additional
