from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .util import digest


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ServiceSettings(StrictModel):
    host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    workers: Literal[1] = 1
    queue_capacity: int = Field(default=32, ge=1, le=1024)
    queue_timeout_s: float = Field(default=10, gt=0)
    pipeline_timeout_s: float = Field(default=45, gt=0)
    job_timeout_s: float = Field(default=60, gt=0)
    admin_token_env: str = "NETDIAG_ADMIN_TOKEN"
    shutdown_timeout_s: float = Field(default=10, gt=0, le=10)


class MaintenanceSettings(StrictModel):
    interval_s: float = Field(default=60, ge=1)
    min_free_bytes: int = Field(default=512 * 1024**2, ge=0)
    max_data_bytes: int = Field(default=10 * 1024**3, gt=0)
    warning_fraction: float = Field(default=.85, gt=0, lt=1)
    index_retention_days: int = Field(default=7, ge=0)
    keep_generations: int = Field(default=2, ge=2)
    embedding_cache_max_entries: int = Field(default=100000, ge=0)


class TelemetrySettings(StrictModel):
    source: Literal["logs", "replay"] = "replay"
    executable: Path = Path("../../server/bin/weaknet-dbus-server")
    args: list[str] = Field(default_factory=list)
    host_id: str = Field(default_factory=socket.gethostname, min_length=1)


class WindowSettings(StrictModel):
    default_seconds: int = 60
    min_seconds: int = 10
    max_seconds: int = 600
    allowed_lateness_seconds: int = Field(default=2, ge=0)
    min_required_metric_coverage: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def order(self):
        if not 0 < self.min_seconds <= self.default_seconds <= self.max_seconds:
            raise ValueError("invalid_window_bounds")
        return self


class RuleSettings(StrictModel):
    path: Path | None = None


class TrendSettings(StrictModel):
    min_samples: int = Field(default=3, ge=3)
    min_span_s: float = Field(default=10, gt=0)
    change_db: float = Field(default=5, gt=0)
    abrupt_drop_db: float = Field(default=8, gt=0)
    directional_fraction: float = Field(default=.7, gt=.5, le=1)
    max_gap_intervals: float = Field(default=2.5, ge=1)


class EmbeddingSettings(StrictModel):
    provider: Literal["none", "bge", "fixture"] = "none"
    model: str = "BAAI/bge-m3"
    revision: str = ""
    dimension: int = Field(default=1024, gt=0)
    normalize: Literal[True] = True
    device: str = "cpu"
    dtype: Literal["float32", "float16", "bfloat16"] = "float32"
    max_input_tokens: int = Field(default=512, ge=64)
    batch_size: int = Field(default=8, ge=1)
    timeout_ms: int = Field(default=2000, gt=0)
    load_timeout_s: float = Field(default=120, gt=0)
    local_files_only: bool = True


class RetrievalSettings(StrictModel):
    mode: Literal["bm25", "dense", "hybrid", "all_context"] = "bm25"
    bm25_top_k: int = Field(default=40, gt=0)
    dense_top_k: int = Field(default=40, gt=0)
    bm25_k1: float = Field(default=1.2, gt=0)
    bm25_b: float = Field(default=0.75, ge=0, le=1)
    rrf_k: int = Field(default=60, gt=0)
    query_variants: int = Field(default=1, ge=1, le=2)
    candidate_limit: int = Field(default=120, ge=1)
    rerank_top_n: int = Field(default=30, ge=1)
    final_top_n: int = Field(default=6, ge=1)
    cache_size: int = Field(default=256, ge=0)


class RerankerSettings(StrictModel):
    enabled: bool = False
    model: str = "BAAI/bge-reranker-v2-m3"
    revision: str = ""
    max_pair_tokens: int = Field(default=768, ge=64)
    max_query_tokens: int = Field(default=192, ge=1)
    batch_size: int = Field(default=8, ge=1)
    timeout_ms: int = Field(default=2000, gt=0)
    load_timeout_s: float = Field(default=120, gt=0)
    device: str = "cpu"
    local_files_only: bool = True
    cache_size: int = Field(default=2048, ge=0)


class GenerationSettings(StrictModel):
    provider: Literal["dashscope"] = "dashscope"
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env: str = "DASHSCOPE_API_KEY"
    api_key_file_env: str = "DASHSCOPE_API_KEY_FILE"
    response_format: Literal["json_object", "text"] = "json_object"
    enable_thinking: bool | None = False
    temperature: float = Field(default=0, ge=0, le=2)
    max_output_tokens: int = Field(default=1500, gt=0)
    call_timeout_s: float = Field(default=25, gt=0)
    max_calls_per_job: int = Field(default=2, ge=1, le=2)
    context_limit: int = Field(default=32768, ge=10000)


class ContextSettings(StrictModel):
    total_tokens: int = Field(default=32000, gt=0)
    knowledge_tokens: int = Field(default=4500, gt=0)
    expand_neighbors: bool = True


class DecisionThresholds(StrictModel):
    min_choice_confidence: float = Field(default=.80, ge=0, le=1)
    exclude_relevance_below: float = Field(default=.10, ge=0, le=1)
    retain_counterevidence_at: float = Field(default=.50, ge=0, le=1)


class DecisionSettings(StrictModel):
    provider: Literal["none", "jev", "fixture"] = "none"
    endpoint: Literal["https://api.typesafe.ai/v1/systemone"] = "https://api.typesafe.ai/v1/systemone"
    model: str = Field(default="jev-1.13.0", pattern=r"^jev-\d+\.\d+\.\d+$")
    api_key_env: str = "TYPESAFE_API_KEY"
    api_key_file_env: str = "TYPESAFE_API_KEY_FILE"
    passage_gate_enabled: bool = True
    check_selection_enabled: bool = True
    max_passages: int = Field(default=12, ge=1, le=12)
    max_check_candidates: int = Field(default=16, ge=1, le=16)
    max_concurrency: int = Field(default=2, ge=1, le=2)
    request_timeout_s: float = Field(default=2, gt=0, le=8)
    total_timeout_s: float = Field(default=8, gt=0, le=8)
    max_payload_bytes: int = Field(default=24000, ge=1024, le=24000)
    retry_enabled: Literal[False] = False
    policy_version: Literal["network-decision-v1"] = "network-decision-v1"
    thresholds: DecisionThresholds = Field(default_factory=DecisionThresholds)


class FallbackSettings(StrictModel):
    allow_lexical_only: bool = True


class KnowledgeSettings(StrictModel):
    allow_seed: bool = False


class ChunkingSettings(StrictModel):
    child_tokens: int = Field(default=450, ge=32)
    parent_tokens: int = Field(default=1000, ge=128)
    overlap_tokens: int = Field(default=50, ge=0)

    @model_validator(mode="after")
    def valid_sizes(self):
        if self.overlap_tokens >= self.child_tokens or self.parent_tokens < self.child_tokens:
            raise ValueError("invalid_chunk_sizes")
        return self


class Settings(StrictModel):
    schema_version: Literal[1] = 1
    profile: Literal["lexical-only", "offline-fixture", "cpu-baseline", "gpu-quality"] = "lexical-only"
    data_dir: Path = Path("var/rag")
    knowledge_dir: Path = Path("knowledge")
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    maintenance: MaintenanceSettings = Field(default_factory=MaintenanceSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    window: WindowSettings = Field(default_factory=WindowSettings)
    rules: RuleSettings = Field(default_factory=RuleSettings)
    rssi_trend: TrendSettings = Field(default_factory=TrendSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    decision: DecisionSettings = Field(default_factory=DecisionSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    fallback: FallbackSettings = Field(default_factory=FallbackSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)

    @model_validator(mode="after")
    def compatible(self):
        if "fixture" in (self.embedding.provider, self.decision.provider):
            if self.profile != "offline-fixture":
                raise ValueError("fixture_provider_requires_offline_fixture_profile")
        for config, enabled in [(self.embedding, self.embedding.provider == "bge"),
                                (self.reranker, self.reranker.enabled)]:
            if enabled and (len(config.revision) != 40 or any(c not in "0123456789abcdef" for c in config.revision)):
                raise ValueError("model_requires_immutable_40_character_revision")
        if self.context.total_tokens > self.generation.context_limit:
            raise ValueError("context_exceeds_model_limit")
        if self.context.knowledge_tokens + self.generation.max_output_tokens >= self.context.total_tokens:
            raise ValueError("no_budget_for_facts_and_instructions")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path).resolve() if path is not None else Path(__file__).resolve().parents[2] / "configs/default.yaml"
    base = path.parent
    raw = yaml.safe_load(path.read_text())
    raw = raw or {}
    if "NETDIAG_DATA_DIR" in os.environ:
        raw["data_dir"] = os.environ["NETDIAG_DATA_DIR"]
    settings = Settings.model_validate(raw)
    for name in ("data_dir", "knowledge_dir"):
        value = getattr(settings, name)
        setattr(settings, name, (base / value).resolve())
    if settings.rules.path is not None:
        settings.rules.path = (base / settings.rules.path).resolve()
    settings.telemetry.executable = (base / settings.telemetry.executable).resolve()
    return settings
