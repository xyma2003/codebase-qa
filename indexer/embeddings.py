"""Application-owned embedding choice; collection metadata never selects code."""
from functools import lru_cache
import numpy as np

from config import EMBED_MODEL


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name_or_path=EMBED_MODEL, device="cpu", trust_remote_code=False, token=False)


class SentenceTransformerEmbeddingFunction:
    def __init__(self, model_name=EMBED_MODEL):
        if model_name != EMBED_MODEL:
            raise ValueError("Embedding model is fixed by the application")

    def __call__(self, input):
        return [np.asarray(row, dtype=np.float32) for row in _model().encode(
            list(input), convert_to_numpy=True, normalize_embeddings=False)]

    def get_config(self):
        return {"model_name": EMBED_MODEL, "device": "cpu", "normalize_embeddings": False, "kwargs": {}}
