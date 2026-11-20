"""Local SQLite only. No network client, plugin registry, reset or telemetry."""
from pathlib import Path

def local_vector_client(path):
    from indexer.sqlite_vectors import LocalVectorClient
    directory = Path(path)
    if (directory / "chroma.sqlite3").exists() and not (directory / "vectors.sqlite3").exists():
        raise ValueError("旧向量库需要先停服导出并校验迁移，原库不会自动覆盖。")
    return LocalVectorClient(directory)
