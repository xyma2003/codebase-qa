"""The application uses embedded Chroma only, with no external configuration."""

import os

import chromadb
from chromadb.config import Settings


class _LocalSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # Chroma settings can select importable components. Never accept those
        # selections from the process environment, .env or a secrets directory.
        return (init_settings,)


def local_chroma_client(path):
    settings = _LocalSettings(
        _env_file=None,
        chroma_api_impl="chromadb.api.rust.RustBindingsAPI",
        is_persistent=True,
        persist_directory=os.fspath(path),
        allow_reset=False,
        anonymized_telemetry=False,
        chroma_otel_collection_endpoint="",
        chroma_otel_granularity=None,
    )
    os.makedirs(path, exist_ok=True)
    return chromadb.PersistentClient(path=os.fspath(path), settings=settings)
