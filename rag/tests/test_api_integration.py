"""Primary RAG integration tests; temporary state, local fixtures, no external model calls."""
import json
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from netdiag_rag.api import create_app
from netdiag_rag.indexing import build_index, publish_index
from netdiag_rag.settings import load_settings
from netdiag_rag.storage import Repository, TERMINAL
from netdiag_rag.telemetry import LogSource, export_replay, import_replay, parse_log

ROOT = Path(__file__).parents[1]
KNOWLEDGE_CUTOFF = json.loads((ROOT / "datasets/manifests/fixtures.json").read_text())["knowledge_cutoff"]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    import httpx

    settings = load_settings(ROOT / "configs/default.yaml")
    settings.data_dir = tmp_path / "state"
    # Simulate the external response only: retrieval, HTTP adapter and output
    # validation all use the production path. No no-model runtime mode exists.
    monkeypatch.setenv(settings.generation.api_key_env, "integration-test-key")
    monkeypatch.delenv(settings.generation.api_key_file_env, raising=False)
    client_class = AsyncClient
    def completion(request):
        assert str(request.url) == settings.generation.base_url + "/chat/completions"
        assert request.headers["authorization"] == "Bearer integration-test-key"
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        gate = payload.get("fact_gate", {})
        content = {"outcome": "insufficient_evidence", "hypotheses": []}
        if "fact_gate" not in payload:
            content.update(outcome="diagnosed", observations=[{
                "text": "先核对知识中的适用条件，再按检查步骤排查；仅凭弱信号不能确认其他故障。",
                "knowledge_ids": [payload["knowledge"][0]["chunk_id"]]}])
        elif gate.get("needs_input"):
            content["outcome"] = "needs_input"
        elif gate.get("normal_window"):
            content["outcome"] = "no_anomaly"
        else:
            for cause, facts in gate.get("confirmed_causes", {}).items():
                knowledge = next((k for k in payload["knowledge"] if cause in k["cause_ids"]), None)
                if knowledge:
                    content["hypotheses"].append({"cause_id": cause, "certainty": "supported",
                        "text": "已有观测支持该故障原因。", "evidence_ids": facts,
                        "knowledge_ids": [knowledge["chunk_id"]]})
                    content["outcome"] = "diagnosed"
            if not content["hypotheses"]:
                knowledge = next((k for k in payload["knowledge"] if "wifi_signal_degradation" in k["cause_ids"]), None)
                facts = [e["evidence_id"] for e in payload["facts"] if e["metric"] in ("icmp_rtt_ms", "wifi_rssi_dbm")]
                if knowledge and facts:
                    content["hypotheses"] = [{"cause_id": "wifi_signal_degradation", "certainty": "plausible",
                        "text": "无线信号可能参与异常，需进一步检查。", "evidence_ids": facts,
                        "knowledge_ids": [knowledge["chunk_id"]]}]
        return httpx.Response(200, json={"id": "test-completion", "model": settings.generation.model,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(completion), **kwargs))
    config = settings.model_dump(mode="json")
    cutoff = KNOWLEDGE_CUTOFF
    build_index(config, ROOT / "knowledge", "test", as_of=cutoff)
    publish_index(config, "test")
    return settings


def fixture_app(settings):
    replays = [(ROOT / f"datasets/fixtures/{name}.jsonl", name)
               for name in ("normal", "wifi_weak_signal", "permission_denied", "link_down")]
    return create_app(settings, replays=replays)


def request_for(name):
    return {**json.loads((ROOT / f"datasets/fixtures/{name}.request.json").read_text()), "data_namespace": "replay:"+name}


def wait_result(client, job_id):
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        response = client.get("/v1/diagnoses/"+job_id)
        assert response.status_code == 200
        value = response.json()
        if value["status"] in TERMINAL:
            return value
        time.sleep(.02)
    pytest.fail("diagnosis did not reach terminal state")


def test_runtime_configs_require_generation():
    from pydantic import ValidationError
    from netdiag_rag.settings import Settings

    assert Settings().generation.provider == "dashscope"
    for path in (ROOT / "configs").glob("*.yaml"):
        assert load_settings(path).generation.provider == "dashscope"
    for invalid in ({"generation": {"provider": "none"}}, {"generation": {"provider": "fixture"}},
                    {"fallback": {"allow_facts_only": True}}, {"retrieval": {"mode": "none"}}):
        with pytest.raises(ValidationError):
            Settings.model_validate(invalid)


def test_implicit_config_matches_explicit_default_from_any_directory(tmp_path, monkeypatch):
    for override in (None, "overridden-state"):
        if override is None:
            monkeypatch.delenv("NETDIAG_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("NETDIAG_DATA_DIR", override)
        expected = load_settings(ROOT / "configs/default.yaml")
        for directory in (ROOT, ROOT.parent, tmp_path):
            monkeypatch.chdir(directory)
            actual = load_settings()
            assert actual.model_dump() == expected.model_dump()
            assert actual.telemetry.executable == ROOT.parent / "server/bin/weaknet-dbus-server"
    custom = tmp_path / "custom.yaml"
    custom.write_text("knowledge_dir: custom-knowledge\ntelemetry:\n  executable: custom-server\n")
    actual = load_settings(custom)
    assert actual.knowledge_dir == tmp_path / "custom-knowledge"
    assert actual.telemetry.executable == tmp_path / "custom-server"


def test_regenerated_fixtures_keep_reviewed_knowledge_eligible(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("generate_fixtures", ROOT / "scripts/generate_fixtures.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.ROOT = tmp_path
    generator.main()
    manifest = json.loads((tmp_path / "datasets/manifests/fixtures.json").read_text())
    assert manifest["knowledge_cutoff"] == KNOWLEDGE_CUTOFF
    assert len(manifest["cases"]) == 8
    for case in manifest["cases"]:
        request = json.loads((tmp_path / f"datasets/fixtures/{case['case_id']}.request.json").read_text())
        assert request == case["request"] and request["knowledge_cutoff"] == KNOWLEDGE_CUTOFF
    settings = load_settings(ROOT / "configs/default.yaml")
    settings.data_dir = tmp_path / "state"
    index = build_index(settings.model_dump(mode="json"), settings.knowledge_dir, "regenerated",
                        as_of=manifest["knowledge_cutoff"])
    assert index["document_count"] == 16


@pytest.mark.parametrize("failure,expected_failures", [("none", 0), ("partial", 1), ("all", 2), ("timeout", 2)])
def test_evaluation_exit_code_reflects_failed_cases(configured, tmp_path, monkeypatch, capsys, failure, expected_failures):
    from netdiag_rag import cli
    from netdiag_rag.providers.generation import GenerationProvider, ProviderUnavailable

    manifest = json.loads((ROOT / "datasets/manifests/fixtures.json").read_text())
    manifest["cases"] = [case for case in manifest["cases"] if case["case_id"] in {"normal", "permission_denied"}]
    for case in manifest["cases"]:
        case["telemetry"] = str(ROOT / f"datasets/fixtures/{case['case_id']}.jsonl")
    dataset = tmp_path / "manifest.json"
    dataset.write_text(json.dumps(manifest))
    generate = GenerationProvider.generate
    def response(self, messages, budget):
        if failure == "timeout":
            raise TimeoutError("deadline_exceeded")
        gate = json.loads(messages[1]["content"])["fact_gate"]
        if failure == "all" or (failure == "partial" and not gate["normal_window"]):
            raise ProviderUnavailable("generation_http_401")
        return generate(self, messages, budget)
    monkeypatch.setattr(GenerationProvider, "generate", response)
    monkeypatch.setattr(cli, "load_settings", lambda path: configured)
    capsys.readouterr()
    code = cli.main(["evaluate", "--dataset", str(dataset), "--output", str(tmp_path / "evaluation"),
                     "--allow-model-calls"])
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["case_count"] == 2 and metrics["failures"] == expected_failures
    assert "real_model_verification" not in metrics
    assert code == (1 if expected_failures else 0)


def test_missing_generation_key_blocks_startup_and_readiness(configured, monkeypatch):
    from netdiag_rag.providers.generation import ProviderUnavailable

    monkeypatch.delenv(configured.generation.api_key_env)
    with pytest.raises(ProviderUnavailable, match="^generation_credentials_missing$"):
        with TestClient(create_app(configured)):
            pytest.fail("service must not start without generation credentials")
    assert not (configured.data_dir / "knowledge.sqlite").exists()
    monkeypatch.setenv(configured.generation.api_key_env, "integration-test-key")
    with TestClient(fixture_app(configured)) as client:
        assert client.get("/health/ready").status_code == 200
        monkeypatch.delenv(configured.generation.api_key_env)
        assert client.get("/health/ready").status_code == 503


def test_missing_index_rejects_diagnosis_and_publication_restores_readiness(configured, monkeypatch):
    configured.data_dir = configured.data_dir / "empty"
    monkeypatch.setenv(configured.service.admin_token_env, "test-admin")
    with TestClient(fixture_app(configured)) as client:
        response = client.post("/v1/diagnoses", json=request_for("normal"))
        assert response.status_code == 503 and response.json()["detail"] == "knowledge_index_unavailable"
        headers = {"X-Admin-Token": "test-admin"}
        assert client.post("/v1/admin/indexes/publish", json={"label": "missing"}, headers=headers).status_code == 422
        assert client.get("/health/ready").status_code == 503
        build_index(configured.model_dump(mode="json"), ROOT / "knowledge", "repaired", as_of=KNOWLEDGE_CUTOFF)
        assert client.post("/v1/admin/indexes/publish", json={"label": "repaired"}, headers=headers).status_code == 200
        assert client.get("/health/ready").status_code == 200
        runtime = client.app.state.runtime
        runtime.index_error = "previous_failure"
        generation = (configured.data_dir / "indexes/CURRENT").read_text().strip()
        runtime.snapshot(generation)  # A historical read cannot repair CURRENT's status.
        assert runtime.index_error == "previous_failure"
        result = wait_result(client, client.post("/v1/diagnoses", json=request_for("normal")).json()["job_id"])
        assert result["status"] == "succeeded" and client.get("/health/ready").status_code == 200


@pytest.mark.parametrize("status,calls", [(401, 1), (503, 2)])
def test_generation_http_failure_never_returns_a_facts_report(configured, monkeypatch, status, calls):
    import httpx
    from netdiag_rag.providers.generation import GenerationProvider

    # Replace the fixture response while retaining the real HTTP adapter.
    transport_calls = []
    client_class = AsyncClient
    def failure(request):
        transport_calls.append(request)
        return httpx.Response(status, text="private-provider-error")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(failure), **kwargs))
    with TestClient(fixture_app(configured)) as client:
        assert isinstance(client.app.state.runtime.service.provider, GenerationProvider)
        job_id = client.post("/v1/diagnoses", json=request_for("normal")).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "failed" and result["report_id"] is None and "report" not in result
        assert len(transport_calls) == calls
        assert result["diagnostics"]["generation"]["status"] == "provider_failed"
        assert "private-provider-error" not in json.dumps(result)


@pytest.mark.parametrize("reason", ["generation_no_knowledge", "generation_context_budget_exceeded"])
def test_missing_generation_context_fails_without_a_report(configured, monkeypatch, reason):
    if reason == "generation_no_knowledge":
        monkeypatch.setattr("netdiag_rag.retrieval.Retriever.search", lambda *args, **kwargs: [])
    else:
        configured.context.total_tokens = 6000
        configured.context.knowledge_tokens = 1000
    with TestClient(fixture_app(configured)) as client:
        job_id = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal")).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "failed" and result["report_id"] is None and "report" not in result, result
        assert result["diagnostics"]["generation"] == {"status": "skipped", "error": {"code": reason}}


@pytest.mark.parametrize("decision_provider", ["none", "fixture"])
def test_knowledge_answer_also_requires_validated_generation(configured, decision_provider):
    from netdiag_rag.providers.generation import ProviderUnavailable

    class UnavailableGenerator:
        def generate(self, messages, budget):
            raise ProviderUnavailable("generation_http_401")

    configured.decision.provider = decision_provider
    with TestClient(fixture_app(configured)) as client:
        request = {"report_type": "knowledge_answer", "question": "WiFi 弱信号如何排查？",
                   "knowledge_cutoff": KNOWLEDGE_CUTOFF}
        response = client.post("/v1/diagnoses", json=request)
        result = wait_result(client, response.json()["job_id"])
        assert result["status"] == "succeeded", result
        assert result["report"]["citations"] and result["report"]["evidence_bundle"] == {}
        assert result["report"]["usage"]["generation_validated"]
        assert result["report"]["observations"]
        citations = {citation["chunk_id"] for citation in result["report"]["citations"]}
        for claim in result["report"]["observations"]:
            assert claim["text"].strip() and claim["knowledge_ids"] and not claim["evidence_ids"]
            assert set(claim["knowledge_ids"]) <= citations
        client.app.state.runtime.service.provider = UnavailableGenerator()
        response = client.post("/v1/diagnoses", json=request)
        failed = wait_result(client, response.json()["job_id"])
        assert failed["status"] == "failed" and "report" not in failed


@pytest.mark.parametrize("invalid,reason", [("empty", "knowledge_answer_requires_body"),
    ("blank", "knowledge_answer_requires_body"), ("uncited", "uncited_claim"), ("unknown", "unknown_citation")])
def test_knowledge_answer_rejects_missing_body_or_citations(configured, invalid, reason):
    class InvalidAnswer:
        calls = 0
        def generate(self, messages, budget):
            self.calls += 1
            assert "禁止输出observations字段" not in messages[0]["content"]
            schema = json.loads(messages[0]["content"].split("仅返回符合以下schema的JSON：")[1])
            assert "observations" in schema["required"] and schema["properties"]["observations"]["minItems"] == 1
            content = {"outcome": "diagnosed"}
            if invalid != "empty":
                knowledge_id = json.loads(messages[1]["content"])["knowledge"][0]["chunk_id"]
                content["observations"] = [{"text": " \t\n" if invalid == "blank" else "需要核对知识中的适用条件。",
                    "knowledge_ids": [] if invalid == "uncited" else ["unknown" if invalid == "unknown" else knowledge_id]}]
            return json.dumps(content), {}
    with TestClient(fixture_app(configured)) as client:
        provider = InvalidAnswer()
        client.app.state.runtime.service.provider = provider
        request = {"report_type": "knowledge_answer", "question": "WiFi 弱信号如何排查？",
                   "knowledge_cutoff": KNOWLEDGE_CUTOFF}
        result = wait_result(client, client.post("/v1/diagnoses", json=request).json()["job_id"])
        assert result["status"] == "failed" and result["report_id"] is None and "report" not in result
        assert result["diagnostics"]["generation"]["error"]["code"] == reason
        assert provider.calls == 2


def test_knowledge_answer_can_repair_empty_body_with_cited_limitations(configured):
    class RepairableAnswer:
        calls = 0
        def generate(self, messages, budget):
            self.calls += 1
            if self.calls == 1:
                return '{"outcome":"diagnosed"}', {}
            assert "knowledge_answer_requires_body" in messages[2]["content"]
            knowledge_id = json.loads(messages[1]["content"])["knowledge"][0]["chunk_id"]
            return json.dumps({"outcome": "insufficient_evidence", "observations": [{
                "text": "知识说明了弱信号的排查方向，但缺少现场观测，无法确认当前故障原因。",
                "knowledge_ids": [knowledge_id]}]}), {}
    with TestClient(fixture_app(configured)) as client:
        provider = RepairableAnswer()
        client.app.state.runtime.service.provider = provider
        request = {"report_type": "knowledge_answer", "question": "WiFi 弱信号如何排查？",
                   "knowledge_cutoff": KNOWLEDGE_CUTOFF}
        result = wait_result(client, client.post("/v1/diagnoses", json=request).json()["job_id"])
        assert result["status"] == "succeeded" and result["report"]["outcome"] == "insufficient_evidence"
        assert result["report"]["observations"] and provider.calls == 2


def _recovery_worker(connection, config):
    """Real IPC and process lifecycle, without loading model weights."""
    from netdiag_rag.providers.embedding import ModelWorkerError, receive_message, send_message

    try:
        with Path(config["test_starts"]).open("a") as handle:
            handle.write("started\n")
        time.sleep(config.get("test_load_delay", 0))
        send_message(connection, ["ready", {"__identity__": {}, "resources": {}}])
        while True:
            payload = receive_message(connection)
            if payload[0] == "exit":
                return
            if payload[0] == "stall":
                time.sleep(10)
            result = ([[1., 0.] for _ in payload] if config["test_kind"] == "embedding" else
                      [{"score": .75, "truncation": {}} for _ in payload[1]])
            send_message(connection, ["ok", {"__model_result__": result, "resources": {}}])
    except (OSError, ModelWorkerError):
        pass
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["embedding", "reranker"])
@pytest.mark.parametrize("fault", ["stall", "exit"])
def test_model_failure_recovers_once_and_stops_at_failure_limit(tmp_path, kind, fault):
    from netdiag_rag.providers.embedding import BoundedEmbedding, EmbeddingError, EmbeddingTimeout
    from netdiag_rag.providers.reranker import BoundedReranker, RerankerError, RerankerTimeout
    from netdiag_rag.util import Budget

    starts = tmp_path / "starts"
    config = {"device": "cpu", "revision": "0" * 40, "dimension": 2, "timeout_ms": 50,
              "load_timeout_s": 5, "test_starts": str(starts), "test_kind": kind}
    model = (BoundedEmbedding if kind == "embedding" else BoundedReranker)(config)
    model.worker = _recovery_worker
    def invoke(query):
        return (model.encode([query], budget=Budget(5)) if kind == "embedding" else
                model.score(query, [{"text": "evidence"}], budget=Budget(5)))
    def fail_request():
        errors = (EmbeddingTimeout, RerankerTimeout) if fault == "stall" else (EmbeddingError, RerankerError)
        with pytest.raises(errors):
            invoke(fault)
        if fault == "exit":
            model.process.join(timeout=2)
            assert not model.process.is_alive()
        else:
            assert model.process is None
    try:
        model.warmup(Budget(5))
        fail_request()
        assert len(invoke("ok")) == 1
        assert starts.read_text().splitlines() == ["started", "started"]
        assert len(invoke("ok")) == 1
        fail_request()
        with pytest.raises((EmbeddingError, RerankerError), match="^model_circuit_open$"):
            invoke("ok")
        assert starts.read_text().splitlines() == ["started", "started"]
    finally:
        model.close()
    with pytest.raises((EmbeddingError, RerankerError), match="^model_worker_closed$"):
        invoke("ok")


@pytest.mark.parametrize("limit", ["expired", "cancelled", "loading", "load_timeout"])
def test_model_recovery_obeys_job_budget_and_cancellation(tmp_path, limit):
    from netdiag_rag.providers.embedding import ModelProcess, ModelStageTimeout
    from netdiag_rag.util import Budget

    starts = tmp_path / "starts"
    model = ModelProcess({"device": "cpu", "load_timeout_s": 5,
                          "test_starts": str(starts), "test_kind": "embedding"}, _recovery_worker)
    try:
        model.warmup(Budget(5))
        with pytest.raises(ModelStageTimeout):
            model.request(["stall"], .05)
        model.config["test_load_delay"] = 5
        if limit == "load_timeout":
            model.config["load_timeout_s"] = .2
        budget = Budget(0 if limit == "expired" else .2 if limit == "loading" else 5,
                        cancelled=lambda: limit == "cancelled")
        error = (InterruptedError if limit == "cancelled" else
                 ModelStageTimeout if limit == "load_timeout" else TimeoutError)
        with pytest.raises(error):
            model.request(["ok"], .05, budget)
        assert model.process is None and model.connection is None
        if limit in {"expired", "cancelled"}:
            assert starts.read_text().splitlines() == ["started"]
    finally:
        model.close()


def test_shared_utilities_preserve_knowledge_identity():
    from datetime import datetime
    from netdiag_rag import util
    from netdiag_rag.knowledge import common

    instant = "2026-06-10T08:00:00+08:00"
    payload = {"cutoff": common.iso(instant), "标题": "网络诊断", "values": [1, 1.25, None, True]}
    encoded = '{"cutoff":"2026-06-10T00:00:00Z","values":[1,1.25,null,true],"标题":"网络诊断"}'
    assert util.canonical(payload) == encoded
    assert common.canonical(payload) == encoded.encode("utf-8")
    assert util.digest(payload) == common.digest(payload) == "92fe61980d60f888d0a6bbe599b8f8dc8b1e15b4510efa0360c48832817a0692"
    assert util.iso(instant) == "2026-06-10T00:00:00.000000Z"
    assert common.iso("2026-06-10T08:00:00.123456+08:00") == "2026-06-10T00:00:00.123456Z"
    for value in (None, datetime(2026, 6, 10), "2026-06-10T00:00:00"):
        with pytest.raises(common.KnowledgeError, match="^timestamp_requires_timezone$"):
            common.iso(value)
    with pytest.raises(common.KnowledgeError, match="^invalid_timestamp: invalid$"):
        common.timestamp("invalid")
    with pytest.raises(ValueError, match="^timezone_required$"):
        util.timestamp("2026-06-10T00:00:00")
    for serialize in (util.canonical, common.canonical):
        with pytest.raises(ValueError):
            serialize({"score": float("nan")})


def test_bge_output_isolates_cached_and_duplicate_rows(monkeypatch):
    from collections import OrderedDict
    from threading import RLock
    from types import SimpleNamespace
    import numpy as np
    from netdiag_rag.providers.embedding import BGEEmbedding

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())  # Warm cache needs no model.
    cached = np.array([1, 0], dtype=np.float32)
    other = np.array([0, 1], dtype=np.float32)
    state = SimpleNamespace(dimension=2, batch_size=8, cache_size=1, _lock=RLock(),
                            _cache=OrderedDict(a=cached, b=other))
    result = BGEEmbedding.encode(state, ["a", "b", "a"])
    np.testing.assert_array_equal(result, [[1, 0], [0, 1], [1, 0]])
    assert result.dtype == np.float32 and result.flags.c_contiguous
    result[0, 0] = 99
    assert result[2, 0] == cached[0] == state._cache["a"][0] == 1
    cached[1] = 99
    assert result[2, 1] == 0


def test_vector_cache_deduplicates_reads_and_isolates_results(tmp_path, monkeypatch):
    import sqlite3
    import numpy as np
    from netdiag_rag.indexing.vector_cache import encode_cached
    from netdiag_rag.providers.embedding import FixtureEmbedding

    queries = []
    connect = sqlite3.connect
    def traced_connection(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(lambda sql: queries.append(sql) if sql.startswith("SELECT value,checksum") else None)
        return db
    monkeypatch.setattr(sqlite3, "connect", traced_connection)
    embedding = FixtureEmbedding({"fixture_vectors": {"same": [1, 0]}, "dimension": 2})
    chunks = [{"text": "same", "doc_id": "same"} for _ in range(3)]
    path = tmp_path / "vectors.sqlite"
    for warm in (False, True):
        queries.clear()
        result, stats = encode_cached(embedding, chunks, path)
        assert len(queries) == 1
        assert stats == {"enabled": True, "hits": 3 if warm else 0, "misses": 0 if warm else 3, "invalidated": 0}
        np.testing.assert_array_equal(result, [[1, 0], [1, 0], [1, 0]])
        result[0, 0] = 99
        assert result[1, 0] == result[2, 0] == 1
    with connect(path) as db:
        db.execute("UPDATE vectors SET checksum='corrupt'")
    queries.clear()
    recovered, stats = encode_cached(embedding, chunks, path)
    assert len(queries) == 1 and stats == {"enabled": True, "hits": 0, "misses": 3, "invalidated": 3}
    np.testing.assert_array_equal(recovered, [[1, 0], [1, 0], [1, 0]])


def test_check_bindings_keep_interface_target_scope_and_individual_validation():
    from types import SimpleNamespace
    from netdiag_rag.contracts import DiagnosisRequest
    from netdiag_rag.decisions.checks import build_checks

    definition = {"check_id": "probe", "purpose": "检查目标", "review_status": "approved",
                  "parameters_schema": {"type": "object", "additionalProperties": False,
                      "properties": {"interface": {"type": "string"}, "target": {"type": "string"}},
                      "required": ["interface", "target"]},
                  "parameter_sources": {"interface": "interface", "target": "target"}}
    restricted = {**definition, "check_id": "restricted", "parameters_schema": {
        **definition["parameters_schema"], "properties": {"interface": {"type": "string"},
                                                        "target": {"type": "string", "enum": ["198.51.100.2"]}}}}
    candidates = [{"chunk_id": "known", "metadata": {"review_status": "approved", "checks": [definition, restricted]}}]
    evidence = [SimpleNamespace(metric="icmp_rtt_ms", scope={"kind": "interface_target", "interface": name, "target": target})
                for name, target in (("wlan0", "192.0.2.1"), ("eth0", "198.51.100.2"))]
    request = DiagnosisRequest(host_id="host", question="检查网络", interfaces=["wlan0"])
    bundle = SimpleNamespace(evidence=evidence, missing=[])
    checks, rejected = build_checks(candidates, request, bundle)
    assert len(checks) == 1 and checks[0].parameters == {"interface": "wlan0", "target": "192.0.2.1"}
    assert rejected == [{"check_id": "restricted", "reason": "check_parameter_schema_mismatch"}]
    bundle.missing = ["ambiguous_interface"]
    checks, rejected = build_checks(candidates, request, bundle)
    assert checks == [] and all(item["missing"] == ["interface", "target"] for item in rejected)


def test_duplicate_json_keys_keep_provider_errors(monkeypatch):
    import httpx
    from netdiag_rag.diagnosis.generation_contract import parse_output, validation_receipt
    from netdiag_rag.providers.decision import DecisionProvider
    from netdiag_rag.settings import DecisionSettings
    from netdiag_rag.util import Budget

    client_class = httpx.AsyncClient
    for body in ('{"outcome":"no_anomaly","outcome":"diagnosed"}',
                 '{"nested":{"text":"first","text":"second"}}'):
        with pytest.raises(ValueError) as error:
            parse_output(body, None)
        assert validation_receipt(error.value) == {"code": "duplicate_json_key"}
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))
        provider = DecisionProvider(DecisionSettings())
        result = asyncio.run(provider._evaluate_many([{}], Budget(5), time.monotonic() + 5, "fixture-key"))
        assert result == [{"error": "jev_invalid_response"}]


def test_provider_key_source_priority_and_environment_validation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from netdiag_rag.providers.generation import generation_key, ProviderUnavailable
    from netdiag_rag.providers.decision import decision_key, DecisionUnavailable

    config = SimpleNamespace(provider="jev", api_key_env="NETDIAG_TEST_KEY", api_key_file_env="NETDIAG_TEST_KEY_FILE")
    path = tmp_path / "key"
    path.write_text("file-fixture-key\n")
    path.chmod(0o600)
    monkeypatch.setenv(config.api_key_file_env, str(path))
    monkeypatch.setenv(config.api_key_env, " env-fixture-key\n")
    assert generation_key(config) == "env-fixture-key"
    assert decision_key(config) == "file-fixture-key"
    monkeypatch.setenv(config.api_key_env, " \n")
    assert generation_key(config) == decision_key(config) == "file-fixture-key"
    monkeypatch.setenv(config.api_key_env, "invalid key")
    with pytest.raises(ProviderUnavailable, match="^generation_credentials_invalid$"):
        generation_key(config)
    assert decision_key(config) == "file-fixture-key"
    monkeypatch.setenv(config.api_key_env, "env-fixture-key")
    monkeypatch.setenv(config.api_key_file_env, str(tmp_path / "missing"))
    assert generation_key(config) == "env-fixture-key"
    with pytest.raises(DecisionUnavailable, match="^jev_credentials_unavailable$"):
        decision_key(config)
    monkeypatch.delenv(config.api_key_file_env)
    for value in ("x" * 4097, "invalid key", "密钥"):
        monkeypatch.setenv(config.api_key_env, value)
        for reader, error, message in ((generation_key, ProviderUnavailable, "generation_credentials_invalid"),
                                       (decision_key, DecisionUnavailable, "jev_credentials_unavailable")):
            with pytest.raises(error, match=f"^{message}$"):
                reader(config)
    monkeypatch.delenv(config.api_key_env)
    with pytest.raises(ProviderUnavailable, match="^generation_credentials_missing$"):
        generation_key(config)
    monkeypatch.setenv(config.api_key_env, "env-fixture-key")
    config.provider = "none"
    with pytest.raises(DecisionUnavailable, match="^jev_credentials_unavailable$"):
        decision_key(config)


def test_provider_key_files_keep_permissions_and_byte_limits(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from netdiag_rag.providers.generation import generation_key, ProviderUnavailable
    from netdiag_rag.providers.decision import decision_key, DecisionUnavailable

    config = SimpleNamespace(provider="jev", api_key_env="NETDIAG_TEST_KEY", api_key_file_env="NETDIAG_TEST_KEY_FILE")
    monkeypatch.delenv(config.api_key_env, raising=False)
    path = tmp_path / "key"
    monkeypatch.setenv(config.api_key_file_env, str(path))
    def rejected(generation_error):
        with pytest.raises(ProviderUnavailable, match=f"^{generation_error}$"):
            generation_key(config)
        with pytest.raises(DecisionUnavailable, match="^jev_credentials_unavailable$"):
            decision_key(config)
    for contents, mode, error in (
        (b"", 0o600, "generation_credentials_missing"),
        (b" \n", 0o600, "generation_credentials_missing"),
        (b"x" * 4096 + b"\n", 0o600, "generation_credentials_invalid"),
        (b"invalid key", 0o600, "generation_credentials_invalid"),
        ("密钥".encode(), 0o600, "generation_key_file_unreadable"),
        (b"file-fixture-key", 0o644, "generation_key_file_requires_owner_only_permissions"),
    ):
        path.write_bytes(contents)
        path.chmod(mode)
        rejected(error)
    for contents in (b"x" * 4096, b"x" * 4095 + b"\n"):
        path.write_bytes(contents)
        path.chmod(0o600)
        assert generation_key(config) == decision_key(config) == contents.decode().strip()
    with monkeypatch.context() as patch:
        info = path.stat()
        patch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1))
        rejected("generation_key_file_requires_owner_only_permissions")
    link = tmp_path / "link"
    link.symlink_to(path)
    monkeypatch.setenv(config.api_key_file_env, str(link))
    rejected("generation_key_file_unreadable")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    monkeypatch.setenv(config.api_key_file_env, str(fifo))
    rejected("generation_key_file_requires_owner_only_permissions")
    monkeypatch.setenv(config.api_key_file_env, str(tmp_path / "missing"))
    rejected("generation_key_file_unreadable")


@pytest.mark.parametrize("capacity", [0, 1, 8])
def test_rerank_results_keep_candidate_order_and_isolate_nested_mutations(tmp_path, capacity):
    from netdiag_rag.indexing import Snapshot
    from netdiag_rag.retrieval import Retriever

    class Scorer:
        def __init__(self):
            self.inputs = []
            self.outputs = []

        def score(self, query, candidates, *, budget=None):
            texts = [candidate["text"] for candidate in candidates]
            assert len(texts) == len(set(texts))
            self.inputs.append(texts)
            truncation = {"spans": []}
            result = [{"score": 1.0 if text == "same" else 9.0, "truncation": truncation} for text in texts]
            self.outputs.extend(result)
            return result

        def close(self):
            pass

    candidates = [{"chunk_id": str(i), "title": "title", "text": text}
                  for i, text in enumerate(("same", "different", "same"))]
    settings = {"retrieval": {"mode": "bm25"}, "reranker": {"cache_size": capacity}}
    with Retriever(settings, Snapshot(manifest={}, chunks=[], path=tmp_path)) as retriever:
        scorer = retriever.reranker = Scorer()
        first = retriever._rerank("query", candidates)
        assert [item["score"] for item in first] == [1, 9, 1]
        assert scorer.inputs == [["same", "different"]]
        first[0]["score"] = -1
        first[0]["truncation"]["spans"].append("changed")
        assert first[2] == {"score": 1.0, "truncation": {"spans": []}}
        assert all(item["truncation"]["spans"] == [] for item in first[1:])
        second = retriever._rerank("query", [candidates[1], candidates[2], candidates[0]])
        assert [item["score"] for item in second] == [9, 1, 1]
        assert all(item["truncation"]["spans"] == [] for item in second)
        if capacity == 8:
            assert len(scorer.inputs) == 1
        assert len(retriever.rerank_cache) <= capacity
        second[1]["truncation"]["spans"].append("changed again")
        assert second[2]["truncation"]["spans"] == []
        third = retriever._rerank("query", [candidates[0]])
        assert third == [{"score": 1.0, "truncation": {"spans": []}}]
        assert all(item["score"] in {1, 9} and item["truncation"]["spans"] == [] for item in scorer.outputs)


def test_console_logs_reach_diagnosis_and_can_be_replayed(configured, tmp_path):
    from datetime import timedelta
    from netdiag_rag.util import iso, utcnow

    lines = [
        "[rtt] RTT_MONITOR: wlan0 | RTT: 240ms | Quality: 3 | Using: YES | Target: 223.5.5.5",
        "[weak_mgr] iface=wlan0 rtt=999ms quality=3 state=1",
        "[rssi] RSSI_MONITOR: wlan0 | RSSI: -82dBm | Quality: 3 | Using: YES",
        "[tcp_loss] TCP_LOSS_MONITOR: interface=wlan0 rate=2.5% delta_sent=40 delta_retrans=1 level=poor",
        "[weak_mgr] TRAFFIC_MONITOR: Total=2.5MB/s, Flows=15, PPS=1200, Interface=wlan0",
        "[weak_mgr] 网络质量变化: POOR (分数: 42.0, 接口: wlan0)",
        "[rtt] RTT_MONITOR: eth0 | RTT: 1ms | Quality: 1 | Using: YES | Target: 223.5.5.5",
    ]
    script = tmp_path / "console.py"
    script.write_text("import sys\n" + f"lines = {lines!r}\n" +
                      "for line in lines: print(line, flush=True)\n" +
                      "print('libbpf: failed to load', file=sys.stderr, flush=True)\n")
    configured.telemetry.source = "logs"
    configured.telemetry.executable = Path(sys.executable)
    configured.telemetry.args = ["-u", str(script)]
    configured.telemetry.host_id = "console-host"
    with TestClient(fixture_app(configured)) as client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            health = client.get("/v1/capabilities").json()["source_health"].get("logs", {})
            if health.get("status") in ("exited", "failed"):
                break
            time.sleep(.01)
        assert health["status"] == "exited", health
        assert health["lines"] == 8 and health["parsed_lines"] == 7
        end = utcnow()
        payload = {"host_id": "console-host", "interfaces": ["wlan0"], "question": "无线延迟为什么升高？",
                   "window_start": iso(end - timedelta(seconds=60)), "window_end": iso(end),
                   "knowledge_cutoff": KNOWLEDGE_CUTOFF}
        response = client.post("/v1/diagnoses", json=payload)
        assert response.status_code == 202, response.text
        result = wait_result(client, response.json()["job_id"])
        assert result["status"] == "succeeded", result
        report = result["report"]
        bundle = report["evidence_bundle"]
        evidence = {item["metric"]: item for item in bundle["evidence"]}
        assert evidence["icmp_rtt_ms"]["summary"]["p95"] == 240
        assert evidence["icmp_rtt_ms"]["summary"]["samples"] == 1  # no duplicate summary
        assert evidence["wifi_rssi_dbm"]["summary"]["median"] == -82
        assert evidence["wifi_rssi_dbm"]["summary"]["trend"]["direction"] == "unknown"
        assert evidence["tcp_retransmission_log"]["summary"]["median"] == 2.5
        assert evidence["service_log"]["summary"]["error_lines"] == 1
        assert {"high_rtt", "low_rssi", "tcp_retransmission"} <= set(bundle["symptoms"])
        assert "tcp" in report["provenance"]["query_plan"]["filters"]["protocols"]
        assert report["outcome"] == "insufficient_evidence"
        assert "icmp_probe_results" in bundle["missing"]
        assert all(e["coverage"] == 0 and not e["sample_ids"] and e["event_ids"] for e in bundle["evidence"])
        assert lines[0] in {e["details"]["raw"] for e in bundle["events"]}
        assert all(e["scope"].get("interface") != "eth0" for e in bundle["events"])
        assert any("日志" in observation["text"] for observation in report["observations"])
        repository = client.app.state.runtime.repository
        replay_file = tmp_path / "capture.jsonl"
        assert export_replay(repository, "live", replay_file)["records"] == 8
        assert import_replay(repository, replay_file, "capture")["inserted"] == 8
        again = client.post("/v1/diagnoses", json={**payload, "data_namespace": "replay:capture"})
        replayed = wait_result(client, again.json()["job_id"])
        assert replayed["status"] == "succeeded", replayed
        assert [e["summary"] for e in replayed["report"]["evidence_bundle"]["evidence"]] == [
            e["summary"] for e in bundle["evidence"]]


@pytest.mark.parametrize("line,metric,value", [
    ("[weak_mgr] iface=eth0 rtt=12ms quality=1 state=1", "icmp_rtt_ms", 12),
    ("[weak_mgr] iface=wlan0 rssi=-79", "wifi_rssi_dbm", -79),
    ("[weak_mgr] iface=eth0 tcp_loss_rate=1.5 tcp_loss_level=poor", "tcp_retransmission_log", 1.5),
    ("RSSI_MONITOR: wlan0 | RSSI: -1000dBm | Quality: 0 | Using: NO", "wifi_rssi_dbm", None),
    ("RTT_MONITOR: eth0 | RTT: -1ms | Quality: 0 | Using: NO | Target: 223.5.5.5", "icmp_rtt_ms", None),
    ("INACTIVE: eth0 | RTT: -1ms | Quality: 0 | RSSI: -1000dBm | TCP Loss: -1% ()", "tcp_retransmission_log", None),
])
def test_original_log_formats_and_sentinels(line, metric, value):
    scope, facts, _ = parse_log(line)
    fact = next(f for f in facts if f["metric"] == metric)
    assert fact["value"] == value
    assert fact["status"] == ("unavailable" if value is None else "ok")
    assert scope.interface in ("eth0", "wlan0")
    assert all(f["metric"] != "link_state" for f in facts)  # INACTIVE is usage, not link down


def test_capture_shutdown_reaps_silent_child_even_when_sigterm_is_ignored(tmp_path):
    from netdiag_rag.settings import TelemetrySettings

    async def exercise():
        marker = tmp_path / "started"
        script = tmp_path / "silent.py"
        script.write_text("import signal, time\nfrom pathlib import Path\n"
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                          f"Path({str(marker)!r}).touch()\n"
                          "while True: time.sleep(1)\n")
        repository = Repository()
        source = LogSource(TelemetrySettings(executable=Path(sys.executable), args=[str(script)]), repository)
        task = asyncio.create_task(source.run())
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            assert marker.exists()
        finally:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            repository.close()
        assert source.process.returncode is not None
        assert await source.process.wait() == source.process.returncode

    asyncio.run(exercise())


def test_capture_failures_and_bounded_line_reader(tmp_path):
    from netdiag_rag.settings import TelemetrySettings
    from netdiag_rag.telemetry import _console_lines, MAX_LOG_LINE_BYTES

    async def exercise():
        repository = Repository()
        try:
            source = LogSource(TelemetrySettings(executable=tmp_path / "missing"), repository)
            await source.run()
            health = repository.source_health()["logs"]
            assert health["status"] == "failed" and health["error"] == "FileNotFoundError"
            failed = LogSource(TelemetrySettings(executable=Path(sys.executable), args=["-c", "raise SystemExit(7)"]), repository)
            await failed.run()
            assert repository.source_health()["logs"]["exit_code"] == 7
            stream = asyncio.StreamReader()
            stream.feed_data(b"x" * (MAX_LOG_LINE_BYTES + 1000) + b"\n" + "完整中文尾行".encode())
            stream.feed_eof()
            rows = [row async for row in _console_lines(stream)]
            assert rows == [("x" * MAX_LOG_LINE_BYTES, True), ("完整中文尾行", False)]
        finally:
            repository.close()

    asyncio.run(exercise())


def test_structured_logs_preserve_batches_and_reject_bad_lines(tmp_path):
    from datetime import timedelta
    from netdiag_rag.contracts import DiagnosisRequest, TelemetryBatch
    from netdiag_rag.evidence import EvidenceBuilder
    from netdiag_rag.settings import TelemetrySettings
    from netdiag_rag.telemetry import STRUCTURED_LOG_PREFIX
    from netdiag_rag.util import iso, timestamp, utcnow

    payload = json.loads((ROOT / "datasets/fixtures/normal.jsonl").read_text().splitlines()[0])["batch"]
    # More than the old 64 KiB cap: valid stdout JSON must survive concurrent stderr.
    first = payload["observations"][0]
    sampled_at = timestamp(first["observed_at"])
    payload["emitted_at"] = iso(sampled_at + timedelta(seconds=30))
    payload["events"] = [{"event_id": "long-event", "event_type": "collector_note",
                          "observed_at": first["observed_at"], "monotonic_ns": first["monotonic_ns"],
                          "scope": {"kind": "host_netns"}, "source": "fixture", "details": {"note": "x" * 70000}}]
    line = STRUCTURED_LOG_PREFIX + json.dumps(payload)
    conflict = json.loads(json.dumps(payload))
    next(o for o in conflict["observations"] if o["metric"] == "icmp_rtt_ms")["value"] += 1
    TelemetryBatch.model_validate(conflict)
    conflicts = [STRUCTURED_LOG_PREFIX + json.dumps(conflict)]
    conflict["sequence"] += 100  # Different batch ID, conflicting sample ID.
    conflicts.append(STRUCTURED_LOG_PREFIX + json.dumps(conflict))
    following = json.loads((ROOT / "datasets/fixtures/normal.jsonl").read_text().splitlines()[1])["batch"]
    later_line = STRUCTURED_LOG_PREFIX + json.dumps(following)
    script = tmp_path / "structured.py"
    script.write_text("import sys, threading\n" + f"line = {line!r}\n" +
                      "worker = threading.Thread(target=lambda: [print('stderr note', file=sys.stderr, flush=True) for _ in range(30)])\n"
                      "worker.start()\nprint(line, flush=True)\nprint('NETDIAG_TELEMETRY {invalid', flush=True)\n"
                      f"[print(item, flush=True) for item in {conflicts!r}]\n"
                      f"print({later_line!r}, flush=True)\nprint(line, flush=True)\nworker.join()\n")
    repository = Repository()
    try:
        settings = TelemetrySettings(executable=Path(sys.executable), args=[str(script)])
        asyncio.run(LogSource(settings, repository).run())
        health = repository.source_health()["logs"]
        assert health["status"] == "exited", health
        assert health["parsed_lines"] == 3 and health["rejected_lines"] == 3
        reasons = {r[0] for r in repository.db.execute("SELECT reason FROM quarantine")}
        assert {"batch_id_payload_conflict", "sample_id_payload_conflict"} <= reasons
        assert health["host_id"] == payload["host_id"] and health["timestamp_source"] == "service_time"
        records = repository.export_batches("live")
        structured = [r["batch"] for r in records if r["batch"]["observations"]]
        assert len(structured) == 2  # Conflicts are skipped; exact repeats remain idempotent.
        assert structured[1]["sequence"] == following["sequence"]
        assert structured[0]["observations"] == TelemetryBatch.model_validate(payload).model_dump(mode="json")["observations"]
        references = [e for e in structured[0]["events"] if e["event_type"] == "service_log"]
        assert references[0]["details"]["raw"] == line
        events = [e for r in records for e in r["batch"]["events"]]
        assert sum(e["details"].get("raw") == "stderr note" for e in events) == 30
        assert any(e["details"].get("parse_error") for e in events)
        request = DiagnosisRequest(host_id=payload["host_id"], question="核查日志引用",
                                   window_start=sampled_at - timedelta(seconds=1),
                                   window_end=sampled_at + timedelta(seconds=10))
        bundle = EvidenceBuilder(repository).build(request, repository.revision(), utcnow())
        # The raw batch was emitted outside the requested sample window.
        assert all(references[0]["event_id"] in e.event_ids for e in bundle.evidence if e.sample_ids)
        assert any(e["event_id"] == references[0]["event_id"] for e in bundle.events)
    finally:
        repository.close()


@pytest.mark.skipif(os.environ.get("NETDIAG_RUN_CPP_LOG_TEST") != "1",
                    reason="run in a private bus: dbus-run-session -- env NETDIAG_RUN_CPP_LOG_TEST=1 .venv/bin/pytest")
def test_current_repository_cpp_logs_reach_rag(configured, tmp_path):
    from datetime import timedelta
    from netdiag_rag.util import iso, utcnow

    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "private session bus required"
    assert configured.telemetry.executable == ROOT.parent / "server/bin/weaknet-dbus-server"
    assert configured.telemetry.executable.is_file(), "build with make -C server ENABLE_BPF=0"
    fixture = tmp_path / "system"
    files = {
        "proc/net/snmp": "Tcp: OutSegs RetransSegs\nTcp: 1000 40\n",
        "proc/net/dev": "wlan0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n",
        "proc/net/route": "Iface Destination Gateway Flags RefCnt Use Metric Mask\n",
        "sys/class/net/wlan0/ifindex": "3\n", "sys/class/net/wlan0/iflink": "3\n",
        "sys/class/net/wlan0/type": "1\n", "sys/class/net/wlan0/flags": "0x1002\n",
    }
    for name, value in files.items():
        path = fixture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (fixture / "sys/class/net/wlan0/wireless").mkdir()
    configured.telemetry.source = "logs"
    configured.telemetry.args = ["--fixture-root", str(fixture), "--interval-ms", "100", "--run-seconds", "2"]
    with TestClient(fixture_app(configured)) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            health = client.get("/v1/capabilities").json()["source_health"].get("logs", {})
            if health.get("status") in ("exited", "failed"):
                break
            time.sleep(.02)
        assert health.get("status") == "exited" and health.get("exit_code") == 0, health
        assert health["parsed_lines"] >= 2 and health["rejected_lines"] == 0, health
        assert health["timestamp_source"] == "service_time"
        end = utcnow()
        request = {"host_id": health["host_id"], "interfaces": ["wlan0"], "question": "接口链路为何断开？",
                   "window_start": iso(end - timedelta(seconds=10)), "window_end": iso(end),
                   "knowledge_cutoff": KNOWLEDGE_CUTOFF}
        response = client.post("/v1/diagnoses", json=request)
        assert response.status_code == 202, response.text
        result = wait_result(client, response.json()["job_id"])
        assert result["status"] == "succeeded", result
        report = result["report"]
        evidence = {e["metric"]: e for e in report["evidence_bundle"]["evidence"]}
        assert {"tcp_segments", "netdev_bytes", "wifi_rssi_dbm", "icmp_rtt_ms", "icmp_probe_result", "link_state"} <= evidence.keys()
        assert evidence["link_state"]["summary"]["state"] == "down"
        assert evidence["netdev_bytes"]["summary"]["effective_seconds"] > 0
        assert evidence["netdev_bytes"]["summary"]["rx_bytes_per_s"] == 0
        assert evidence["icmp_probe_result"]["summary"]["attempts"] == 0  # --fixture-root disables real probes
        assert evidence["wifi_rssi_dbm"]["summary"]["valid_samples"] == 0  # no invented RSSI
        assert report["outcome"] == "diagnosed", report["hypotheses"]
        assert any(h["cause_id"] == "interface_link_down" for h in report["hypotheses"])
        raw = {e["event_id"] for e in report["evidence_bundle"]["events"] if e["event_type"] == "service_log"}
        assert raw and all(set(e["event_ids"]) & raw for e in evidence.values())
        assert "service_log" not in evidence  # JSON provenance is not a duplicate metric or error
        replay_file = tmp_path / "current-server.jsonl"
        repository = client.app.state.runtime.repository
        export_replay(repository, "live", replay_file)
        import_replay(repository, replay_file, "current-server")
        response = client.post("/v1/diagnoses", json={**request, "data_namespace": "replay:current-server"})
        repeated = wait_result(client, response.json()["job_id"])
        assert repeated["status"] == "succeeded", repeated
        assert repeated["report"]["outcome"] == report["outcome"]


def test_diagnosis_is_session_only_and_idempotent(configured):
    with TestClient(fixture_app(configured)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        payload = request_for("wifi_weak_signal")
        first = client.post("/v1/diagnoses", json=payload, headers={"Idempotency-Key": "example"})
        assert first.status_code == 202, first.text
        second = client.post("/v1/diagnoses", json=payload, headers={"Idempotency-Key": "example"})
        assert second.json()["job_id"] == first.json()["job_id"]
        result = wait_result(client, first.json()["job_id"])
        assert result["status"] == "succeeded", result
        report = result["report"]
        assert report["outcome"] == "insufficient_evidence"
        assert report["citations"] and report["retrieval_trace"]
        assert all("context_selected" in item for item in report["retrieval_trace"])
        metrics = client.get("/v1/metrics").json()
        assert metrics["queue"]["succeeded"] == 1
        assert "queue_wait_ms" not in metrics["distributions"]
        assert metrics["disk"]["status"] == "ok" and metrics["indexes"]
        assert metrics["resources"]["service_rss_peak_bytes"] > 0
        assert report["hypotheses"][0]["cause_id"] == "wifi_signal_degradation"
        assert report["usage"]["generation_started"] and report["usage"]["generation_validated"]
        assert result["diagnostics"]["generation"]["status"] == "validated"
        conflict = client.post("/v1/diagnoses", json={**payload, "question": "changed"}, headers={"Idempotency-Key": "example"})
        assert conflict.status_code == 409
        report_id = report["report_id"]
    with TestClient(create_app(configured)) as client:
        assert client.get("/v1/reports/"+report_id).status_code == 404
        assert client.get("/v1/diagnoses/"+result["job_id"]).status_code == 404
        assert client.app.state.runtime.repository.export_batches("replay:wifi_weak_signal") == []
        assert client.get("/v1/capabilities").json()["runtime_storage"] == "memory"
    assert not (configured.data_dir / "facts.sqlite").exists()
    import sqlite3
    with sqlite3.connect(configured.data_dir / "knowledge.sqlite") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"revocations", "control"}


def test_normal_window_and_missing_authority(configured):
    with TestClient(fixture_app(configured)) as client:
        response = client.post("/v1/diagnoses", json=request_for("normal"))
        result = wait_result(client, response.json()["job_id"])
        assert result["status"] == "succeeded", result
        assert result["report"]["outcome"] == "no_anomaly"
        assert client.post("/v1/admin/knowledge/revoke", json={"key": "anything"}).status_code == 403
        assert client.post("/v1/diagnoses", json={"question": "missing host"}).status_code == 422
        assert client.post("/v1/diagnoses", content=b" "*65537, headers={"Content-Type": "application/json"}).status_code == 413


@pytest.mark.parametrize("metric,status,error,normal", [
    ("wifi_rssi_dbm", "not_applicable", "not_wifi", True),
    ("flow_top_bytes_per_s", "unavailable", "flow_not_enabled", True),
    ("wifi_rssi_dbm", "unavailable", "rssi_unavailable", False),
    ("flow_top_bytes_per_s", "permission_denied", "bpf_permission_denied", False),
    ("icmp_rtt_ms", "unavailable", "probe_disabled", False),
    ("icmp_rtt_ms", "not_applicable", "loopback_interface", False),
])
def test_coverage_distinguishes_unused_metrics_from_failed_collection(metric, status, error, normal):
    from contextlib import closing
    from netdiag_rag.contracts import DiagnosisRequest, TelemetryBatch
    from netdiag_rag.diagnosis import fact_outcome
    from netdiag_rag.evidence import EvidenceBuilder

    request = DiagnosisRequest.model_validate(request_for("normal"))
    with closing(Repository()) as repository:
        for line in (ROOT / "datasets/fixtures/normal.jsonl").read_text().splitlines():
            record = json.loads(line)
            for sample in list(record["batch"]["observations"]):
                if sample["metric"] != ("wifi_rssi_dbm" if metric == "flow_top_bytes_per_s" else metric):
                    continue
                if metric == "flow_top_bytes_per_s":
                    sample = json.loads(json.dumps(sample))
                    sample.update(sample_id=sample["sample_id"] + "-flow", metric=metric, unit="B/s")
                    sample["scope"].update(kind="flow_subset", direction="tx", protocol="tcp", top_n=10,
                                           hook="test", ifindex=2, interface_generation=1)
                    record["batch"]["observations"].append(sample)
                sample.update(status=status, value=None, error_code=error)
            repository.append(TelemetryBatch.model_validate(record["batch"]), request.data_namespace, record["received_at"])
        # An ordinary log line cannot turn otherwise complete telemetry into missing samples.
        scope, facts, _ = parse_log("RTT_MONITOR: wlan0 | RTT: 20ms | Quality: 4 | Using: YES | Target: 8.8.8.8")
        repository.append(TelemetryBatch(host_id=request.host_id, boot_id="log", collector_session_id="log",
            netns_id="log", stream_id="console", sequence=1, emitted_at=request.window_start,
            events=[dict(event_id="log", event_type="service_log", observed_at=request.window_start,
                         monotonic_ns=0, scope=scope, source="console_log_v1",
                         details={"line_number": 1, "facts": facts, "raw": "ordinary log"})]),
            request.data_namespace, request.as_of_received_at)
        bundle = EvidenceBuilder(repository).build(request, repository.revision(), request.as_of_received_at)
        metric_evidence = next(e for e in bundle.evidence if e.metric == metric and e.sample_ids)
        assert metric_evidence.coverage == 0  # Never fabricate valid coverage for an unused metric.
        assert (fact_outcome(bundle, [], {}) == "no_anomaly") is normal
        if normal:
            assert "incomplete_telemetry" not in bundle.symptoms
        elif status != "not_applicable":
            assert "incomplete_telemetry" in bundle.symptoms


@pytest.mark.parametrize("text,repair", [("测得时延为 60 ms。", False), ("测得RTT60ms。", False),
                                       ("测得时延为六十毫秒。", True)])
def test_generated_measurements_cannot_reuse_sample_counts(configured, text, repair):
    class Generator:
        calls = 0
        def generate(self, messages, budget):
            self.calls += 1
            payload = json.loads(messages[1]["content"])
            fact = next(e for e in payload["facts"] if e["metric"] == "icmp_rtt_ms")
            assert fact["summary"]["p95"] == 20 and fact["summary"]["samples"] == 60
            if self.calls > 1:
                assert "incident_narrative_contains_numbers" in messages[-1]["content"]
                if repair:
                    return '{"outcome":"no_anomaly"}', {}
            knowledge = next(k for k in payload["knowledge"] if k["cause_ids"])
            return json.dumps({"outcome": "insufficient_evidence", "hypotheses": [{
                "text": text, "certainty": "plausible", "cause_id": knowledge["cause_ids"][0],
                "evidence_ids": [fact["evidence_id"]], "knowledge_ids": [knowledge["chunk_id"]]}]}), {}
    with TestClient(fixture_app(configured)) as client:
        generator = client.app.state.runtime.service.provider = Generator()
        result = wait_result(client, client.post("/v1/diagnoses", json=request_for("normal")).json()["job_id"])
        assert generator.calls == 2
        if repair:
            assert result["status"] == "succeeded"
            assert any("20 ms" in c["text"] for c in result["report"]["observations"])
        else:
            assert result["status"] == "failed" and result["report_id"] is None
            assert result["diagnostics"]["generation"]["error"]["code"] == "incident_narrative_contains_numbers"


def test_retrieval_cache_reuses_equivalent_times_but_obeys_expiry_and_revocation(configured):
    from datetime import timedelta
    from netdiag_rag.indexing import load_snapshot, HistoricalKnowledgeUnavailable
    from netdiag_rag.retrieval import Retriever
    from netdiag_rag.util import timestamp, iso

    config = configured.model_dump(mode="json")
    snapshot = load_snapshot(config)
    cutoff = timestamp(KNOWLEDGE_CUTOFF)
    expiry = iso(cutoff + timedelta(minutes=1))
    doc = next(c for c in snapshot.chunks if "RTT" in c["title"])
    for chunk in snapshot.chunks:
        if chunk["doc_id"] == doc["doc_id"]:
            chunk["metadata"]["valid_until"] = expiry
    with Retriever(config, snapshot) as retriever:
        first = retriever.search(doc["title"], {"knowledge_cutoff": KNOWLEDGE_CUTOFF})
        assert not retriever.last_trace["cache_hit"] and any(c["doc_id"] == doc["doc_id"] for c in first)
        filters = {"knowledge_cutoff": iso(cutoff + timedelta(seconds=1))}
        assert retriever.search(doc["title"], filters) == first and retriever.last_trace["cache_hit"]
        expired = retriever.search(doc["title"], {**filters, "valid_at": expiry})
        assert not retriever.last_trace["cache_hit"] and all(c["doc_id"] != doc["doc_id"] for c in expired)
        revoked = retriever.search(doc["title"], filters, revoked={doc["doc_id"]})
        assert not retriever.last_trace["cache_hit"] and all(c["doc_id"] != doc["doc_id"] for c in revoked)
        with pytest.raises(HistoricalKnowledgeUnavailable):
            retriever.search(doc["title"], {"knowledge_cutoff": iso(cutoff - timedelta(seconds=1))})


def test_idempotent_default_window_reuses_original_job_and_pinned_input(configured, monkeypatch):
    from datetime import timedelta
    from netdiag_rag.util import utcnow

    with TestClient(fixture_app(configured)) as client:
        payload = {"host_id": "h", "question": "分析网络"}
        headers = {"Idempotency-Key": "default-window"}
        first = client.post("/v1/diagnoses", json=payload, headers=headers)
        assert first.status_code == 202
        original = client.get(first.json()["status_url"]).json()
        later = utcnow() + timedelta(seconds=60)
        monkeypatch.setattr("netdiag_rag.api.utcnow", lambda: later)
        second = client.post("/v1/diagnoses", json=payload, headers=headers)
        assert second.status_code == 202 and second.json()["job_id"] == original["job_id"]
        repeated = client.get(second.json()["status_url"]).json()
        assert repeated["request"] == original["request"]
        assert repeated["pinned"] == original["pinned"]
        explicit = {**payload, "window_start": original["request"]["window_start"],
                    "window_end": original["request"]["window_end"]}
        assert client.post("/v1/diagnoses", json=explicit, headers=headers).status_code == 409


def test_directly_observed_causes_have_evidence(configured):
    with TestClient(fixture_app(configured)) as client:
        for name, cause in [("permission_denied", "collector_permission_denied"), ("link_down", "interface_link_down")]:
            response = client.post("/v1/diagnoses", json=request_for(name))
            result = wait_result(client, response.json()["job_id"])
            assert result["status"] == "succeeded", result
            assert result["report"]["outcome"] == "diagnosed", result["report"]
            hypothesis = result["report"]["hypotheses"][0]
            assert hypothesis["cause_id"] == cause
            assert hypothesis["evidence_ids"] and hypothesis["knowledge_ids"]


def test_invalid_generator_has_one_shared_repair_budget(configured):
    configured.context.total_tokens = 32000
    class BadGenerator:
        calls = 0
        def generate(self, messages, budget):
            self.calls += 1
            return '{"outcome":"diagnosed","observations":[{"text":"fake","evidence_ids":["unknown"]}]}', {}
    app = fixture_app(configured)
    with TestClient(app) as client:
        provider = BadGenerator()
        app.state.runtime.service.provider = provider
        response = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal"))
        result = wait_result(client, response.json()["job_id"])
        assert result["status"] == "failed", result
        assert provider.calls == 2
        assert "report" not in result and result["report_id"] is None
        receipt = result["diagnostics"]["generation"]
        assert receipt["status"] == "rejected"
        assert receipt["error"]["code"] == "output_schema_invalid"
        assert "generation_failed:output_schema_invalid" in result["error"]


def test_failed_generation_receipts_are_session_only_without_response_text(configured):
    configured.context.total_tokens = 32000
    class BadGenerator:
        def generate(self, messages, budget):
            return '{"outcome":"diagnosed","observations":[{"text":"private-model-text"}]}', {"prompt_tokens": 11, "completion_tokens": 7}
    app = fixture_app(configured)
    with TestClient(app) as client:
        app.state.runtime.service.provider = BadGenerator()
        job_id = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal")).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "failed" and "output_schema_invalid" in result["error"]
        assert "private-model-text" not in json.dumps(result)
        receipt = result["diagnostics"]["generation"]
        assert receipt["status"] == "rejected" and receipt["provider"] == {}
        assert not {"attempt", "duration_ms", "started_at"}.intersection(receipt)
        assert result["diagnostics"]["retrieval"]["candidate_count"] > 0
    with TestClient(create_app(configured)) as client:
        assert client.get("/v1/diagnoses/" + job_id).status_code == 404


@pytest.mark.parametrize("decision_provider", ["none", "fixture"])
def test_repair_stays_within_reserved_context_and_can_succeed(configured, monkeypatch, decision_provider):
    import netdiag_rag.diagnosis as diagnosis
    from netdiag_rag.diagnosis.generation_contract import REPAIR_MESSAGE_BYTES
    original = diagnosis.generation_messages
    original_schema = diagnosis.output_schema
    schema_modes = []
    def counted_schema(bundle, program_checks=False):
        schema_modes.append(program_checks)
        return original_schema(bundle, program_checks)
    monkeypatch.setattr(diagnosis, "output_schema", counted_schema)
    configured.decision.provider = decision_provider
    configured.context.total_tokens = 20000
    candidates_seen = []
    def nearly_full_messages(request, bundle, candidates, *args, **kwargs):
        messages = original(request, bundle, candidates, *args, **kwargs)
        candidates_seen.append(len(candidates))
        if len(candidates_seen) == 1:
            padding = (configured.context.total_tokens - configured.generation.max_output_tokens
                       - sum(len(m["content"].encode()) for m in messages) - 1)
            assert padding > 0
            messages[0]["content"] += " " * padding
        return messages
    monkeypatch.setattr(diagnosis, "generation_messages", nearly_full_messages)
    class RepairableGenerator:
        calls = 0
        def generate(self, messages, budget):
            self.calls += 1
            size = sum(len(m["content"].encode()) for m in messages)
            assert size + configured.generation.max_output_tokens < configured.context.total_tokens
            if self.calls == 1:
                assert size + configured.generation.max_output_tokens + REPAIR_MESSAGE_BYTES < configured.context.total_tokens
                return '{"outcome":"no_anomaly","observations":[]}', {}
            assert len(messages) == 3 and "output_schema_invalid" in messages[2]["content"]
            return '{"outcome":"no_anomaly"}', {}
    app = fixture_app(configured)
    with TestClient(app) as client:
        provider = RepairableGenerator()
        app.state.runtime.service.provider = provider
        job_id = client.post("/v1/diagnoses", json=request_for("normal")).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "succeeded", result
        assert candidates_seen[1] < candidates_seen[0]
        assert schema_modes == ([False, True] if decision_provider == "fixture" else [False])
        assert provider.calls == 2 and result["report"]["usage"]["generation_validated"]
        assert result["diagnostics"]["generation"]["status"] == "validated"
        metadata = json.dumps({"usage": result["report"]["usage"], "diagnostics": result["diagnostics"],
                               "metrics": client.get("/v1/metrics").json()})
        for field in ("input_tokens", "output_tokens", "completion_tokens", "prompt_tokens", "duration_ms",
                      "timings", "elapsed_ms", "generation_calls", "queue_wait_ms", '"calls"', '"attempts"'):
            assert field not in metadata


def test_deadline_keeps_generation_attempt_diagnostics(configured):
    configured.context.total_tokens = 32000
    class StalledGenerator:
        def generate(self, messages, budget):
            raise TimeoutError("deadline_exceeded")
    app = fixture_app(configured)
    with TestClient(app) as client:
        app.state.runtime.service.provider = StalledGenerator()
        job_id = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal")).json()["job_id"]
        result = wait_result(client, job_id)
        assert result["status"] == "timed_out"
        assert result["diagnostics"]["generation"]["error"]["code"] == "deadline_exceeded"


def test_revocation_survives_restart_but_session_reports_do_not(configured, monkeypatch):
    monkeypatch.setenv(configured.service.admin_token_env, "integration-test-token")
    with TestClient(fixture_app(configured)) as client:
        response = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal"))
        before = wait_result(client, response.json()["job_id"])["report"]
        cited_id = before["hypotheses"][0]["knowledge_ids"][0]
        doc_id = next(c["doc_id"] for c in before["citations"] if c["chunk_id"] == cited_id)
        response = client.post("/v1/admin/knowledge/revoke", json={"key": doc_id}, headers={"X-Admin-Token": "integration-test-token"})
        assert response.status_code == 200
        old = client.get("/v1/reports/" + before["report_id"]).json()
        assert next(c for c in old["citations"] if c["doc_id"] == doc_id)["revoked"]
        response = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal"))
        after = wait_result(client, response.json()["job_id"])["report"]
        assert all(c["doc_id"] != doc_id for c in after["citations"])
    with TestClient(fixture_app(configured)) as client:
        assert doc_id in client.app.state.runtime.repository.revocations()[1]
        assert client.get("/v1/reports/" + before["report_id"]).status_code == 404
        response = client.post("/v1/diagnoses", json=request_for("wifi_weak_signal"))
        restarted = wait_result(client, response.json()["job_id"])["report"]
        assert all(c["doc_id"] != doc_id for c in restarted["citations"])


def test_memory_window_cleanup_pins_active_inputs_and_limits_finished_results():
    from datetime import timedelta
    from netdiag_rag.contracts import DiagnosisRequest, TelemetryBatch
    from netdiag_rag.util import utcnow

    repository = Repository()
    try:
        now = utcnow()
        batch = TelemetryBatch.model_validate(json.loads((ROOT / "datasets/fixtures/normal.jsonl").read_text().splitlines()[0])["batch"])
        repository.append(batch, received_at=now - timedelta(seconds=900))
        request = DiagnosisRequest(host_id=batch.host_id, question="分析窗口", window_start=now - timedelta(seconds=1000),
                                   window_end=now - timedelta(seconds=940))
        active = repository.submit(request, {}, 1)
        repository.prune(600)
        assert repository.export_batches("live")
        repository.cancel(active["job_id"])
        repository.prune(600)
        assert not repository.export_batches("live")
        assert repository.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        job_ids = []
        for number in range(70):
            repository.submit(DiagnosisRequest(report_type="knowledge_answer", question="说明网络指标"), {}, 1)
            job = repository.claim(10)
            job_ids.append(job["job_id"])
            repository.commit_report(job["job_id"], {"report_id": str(number), "citations": []}, 0, lambda: None)
        repository.prune(600)
        assert repository.job(job_ids[0]) is None
        assert repository.report("0") is None and repository.report("69") is not None
        assert repository.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 64
    finally:
        repository.close()


def test_abrupt_exit_leaves_no_log_or_report_file(tmp_path):
    import subprocess

    state = tmp_path / "knowledge.sqlite"
    script = """
import os, sys
from pathlib import Path
from netdiag_rag.storage import Repository
repository = Repository(Path(sys.argv[1]))
repository.quarantine('invalid', 'session-private-log-marker')
with repository.transaction() as db:
    db.execute("INSERT INTO reports VALUES('report', 'job', 'now', 'session-private-report-marker')")
repository.revoke('revoked-knowledge')
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(state)], check=True, timeout=10)
    assert b"session-private" not in state.read_bytes()
    repository = Repository(state)
    try:
        assert repository.report("report") is None
        assert repository.db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 0
        assert repository.revocations() == (1, {"revoked-knowledge"})
    finally:
        repository.close()
    assert {p.name for p in tmp_path.iterdir()} == {"knowledge.sqlite"}


def test_publication_keeps_queued_generation_then_releases_it(configured, monkeypatch):
    from netdiag_rag.api import Runtime
    from netdiag_rag.contracts import DiagnosisRequest
    repository = Repository(configured.data_dir / "knowledge.sqlite")
    import_replay(repository, ROOT / "datasets/fixtures/normal.jsonl", "normal")
    runtime = Runtime(configured, repository)
    try:
        old = runtime.snapshot().generation_id
        queued = runtime.submit(DiagnosisRequest.model_validate(request_for("normal")))
        built = build_index(configured.model_dump(mode="json"), ROOT / "knowledge", "new", as_of=KNOWLEDGE_CUTOFF)
        assert built["generation_id"] != old
        replace = os.replace
        def interrupted_replace(source, destination):
            if Path(destination).name == "CURRENT":
                raise OSError("publication_interrupted")
            return replace(source, destination)
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupted_replace)
            with pytest.raises(OSError, match="publication_interrupted"):
                runtime.publish("new")
        assert (configured.data_dir / "indexes/CURRENT").read_text().strip() == old
        assert runtime.snapshot().generation_id == old
        assert not list((configured.data_dir / "indexes").glob(".pending-*"))
        runtime.publish("new")
        assert old in runtime.service.retrievers
        assert runtime.snapshot(old) is runtime.service.retrievers[old].snapshot
        repository.cancel(queued["job_id"])
        runtime.reap()
        assert old not in runtime.service.retrievers
        assert built["generation_id"] in runtime.service.retrievers
    finally:
        runtime.service.close()
        repository.close()
