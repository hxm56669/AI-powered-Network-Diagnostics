"""Explicit, pinned downloads and a real inference probe. Never invoked at service startup."""
from __future__ import annotations

import importlib.metadata
import platform

import numpy as np

from .embedding import BoundedEmbedding, require_revision
from .reranker import BoundedReranker


def model_specs(settings):
    specs = []
    if settings.embedding.provider == "bge":
        specs.append((settings.embedding, "BAAI/bge-m3", "pytorch_model.bin"))
    if settings.reranker.enabled:
        specs.append((settings.reranker, "BAAI/bge-reranker-v2-m3", "model.safetensors"))
    if not specs:
        raise ValueError("no_local_models_enabled")
    for config, expected, _ in specs:
        if config.model != expected:
            raise ValueError("unsupported_model")
        require_revision(config.revision)
    return specs


def download_models(settings):
    from huggingface_hub import snapshot_download

    results = []
    for config, model, weights in model_specs(settings):
        path = snapshot_download(model, revision=config.revision, max_workers=3,
                                 allow_patterns=["config.json", "tokenizer.json", "tokenizer_config.json",
                                                 "special_tokens_map.json", "sentencepiece.bpe.model", weights])
        results.append({"model": model, "revision": config.revision, "snapshot_path": path})
    return {"status": "downloaded", "models": results, "inference_verified": False}


def runtime_environment():
    result = {"python": platform.python_version(), "platform": platform.platform(), "packages": {}}
    for name in ("torch", "transformers", "huggingface-hub", "faiss-cpu"):
        try:
            result["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][name] = None
    if result["packages"]["torch"]:
        import torch

        result["cuda_runtime"] = torch.version.cuda
        result["cuda_available"] = torch.cuda.is_available()
        result["gpu"] = [{"name": torch.cuda.get_device_name(i),
                          "total_memory_bytes": torch.cuda.get_device_properties(i).total_memory}
                         for i in range(torch.cuda.device_count())]
    return result


def verify_models(settings):
    if settings.embedding.provider != "bge" or not settings.reranker.enabled:
        raise ValueError("real_model_validation_requires_bge_and_reranker")
    model_specs(settings)
    embedding = BoundedEmbedding(settings.embedding.model_dump())
    reranker = BoundedReranker(settings.reranker.model_dump())
    query = "无线网络信号很弱，延迟变高，应该先检查什么？"
    passages = ["无线信号偏弱时，检查客户端与接入点距离、遮挡和信道干扰，并比较有线连接。",
                "TCP 计数器重置时不能将负增量解释成重传率，应丢弃跨重置区间。"]
    try:
        embedding.warmup()
        reranker.warmup()
        vectors = embedding.encode([query, *passages])
        candidates = [{"text": text, "title": "", "metadata": {}} for text in passages]
        scores = reranker.score(query, candidates)
        norms = np.linalg.norm(vectors, axis=1)
        similarities = (vectors[0] @ vectors[1:].T).tolist()
        checks = {"dimension_1024": vectors.shape == (3, 1024),
                  "finite_normalized_vectors": bool(np.isfinite(vectors).all() and np.allclose(norms, 1, atol=1e-5)),
                  "dense_relevant_above_unrelated": similarities[0] > similarities[1],
                  "rerank_relevant_above_unrelated": scores[0]["score"] > scores[1]["score"]}
        return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
                "embedding": embedding.identity, "reranker": reranker.identity,
                "vector_norms": norms.tolist(), "dense_similarities": similarities,
                "rerank_scores": scores, "environment": runtime_environment(),
                "resources": {"embedding": embedding.resources, "reranker": reranker.resources},
                "scope": "local_model_inference_probe; not_dataset_quality_certification"}
    finally:
        embedding.close()
        reranker.close()
