import sys
import threading
import time
from unittest.mock import MagicMock, patch
import pytest
from src.dao.emb.embedder import LocalEmbedder, TFIDFSparseEncoder


def test_local_embedder_ensure_model_concurrency(tmp_config, monkeypatch):
    """Mock SentenceTransformer constructor to take 100ms.
    Launch 10 threads calling _ensure_model simultaneously,
    and assert that SentenceTransformer.__init__ is only called exactly 1 time.
    """
    init_call_count = 0
    init_lock = threading.Lock()

    # Create a mock SentenceTransformer class
    class MockSentenceTransformer:
        def __init__(self, model_name):
            nonlocal init_call_count
            with init_lock:
                init_call_count += 1
            # Sleep 100ms to simulate slow model loading
            time.sleep(0.1)

        def encode(self, text, *args, **kwargs):
            return [0.1] * 384

    # Patch SentenceTransformer in embedder module
    monkeypatch.setattr("src.dao.emb.embedder.SentenceTransformer", MockSentenceTransformer)
    monkeypatch.setattr("src.dao.emb.embedder._HAS_SENTENCE_TRANSFORMERS", True)

    embedder = LocalEmbedder(dense_model="mock-model", dimension=384)

    # Launch 10 threads
    threads = []
    models = [None] * 10

    def worker(i):
        models[i] = embedder._ensure_model()

    for i in range(10):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Assert __init__ was called exactly once
    assert init_call_count == 1
    # Assert all threads got the exact same model instance
    assert all(m is models[0] for m in models)
    assert models[0] is not None


def test_tfidf_sparse_encoder_concurrency():
    """Simulate multiple threads doing fit + update + encode on TFIDFSparseEncoder
    simultaneously, ensuring there is no race condition, deadlock, or lost updates.
    """
    encoder = TFIDFSparseEncoder()

    # Thread A: fits a corpus
    # Thread B: updates online
    # Thread C: encodes
    # Let's run 15 threads calling fit, update, and encode concurrently.
    threads = []
    errors = []

    def worker_fit():
        try:
            for _ in range(5):
                encoder.fit(["hello world", "test corpus data", "concurrency test"])
                time.sleep(0.01)
        except Exception as e:
            errors.append(f"fit error: {e}")

    def worker_update():
        try:
            for _ in range(10):
                encoder.update("concurrent update text")
                time.sleep(0.01)
        except Exception as e:
            errors.append(f"update error: {e}")

    def worker_encode():
        try:
            for _ in range(10):
                encoder.encode("test query text")
                time.sleep(0.01)
        except Exception as e:
            errors.append(f"encode error: {e}")

    for _ in range(5):
        threads.append(threading.Thread(target=worker_fit))
        threads.append(threading.Thread(target=worker_update))
        threads.append(threading.Thread(target=worker_encode))

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # Assert no errors occurred during concurrent operations
    assert len(errors) == 0, f"Errors occurred during concurrency: {errors}"
    # Assert updates were correctly accumulated
    # 5 fit threads * 5 iterations * 3 docs = 75 docs
    # 5 update threads * 10 iterations * 1 doc = 50 docs
    # Total doc count = 125
    assert encoder.n_docs == 125
