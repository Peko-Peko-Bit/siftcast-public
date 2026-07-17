import threading
import numpy as np

_model = None
_lock = threading.Lock()

def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                print("[embedder] Loading paraphrase-multilingual-MiniLM-L12-v2 ...")
                _model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
                print("[embedder] Model ready.")
    return _model

def encode(text: str) -> list:
    """Return L2-normalized embedding as a Python list."""
    return get_model().encode(text, normalize_embeddings=True).tolist()

def cosine(a: list, b: list) -> float:
    """Cosine similarity of two normalized vectors (= dot product)."""
    return float(np.dot(a, b))

def preload():
    """Warm up the model in a background thread so the first request is fast."""
    threading.Thread(target=get_model, daemon=True).start()
