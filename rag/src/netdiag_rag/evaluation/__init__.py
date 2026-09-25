"""Single-configuration fixture checks, not optimization comparisons or quality certification."""
from __future__ import annotations

import json
import math
import platform
import statistics
import subprocess
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from ..contracts import DiagnosisRequest
from ..diagnosis import DiagnosisService
from ..storage import Repository
from ..telemetry import import_replay
from ..util import Budget, atomic_json, atomic_write, canonical, digest, iso, owner_lock, utcnow


def recall_at_k(ranked, relevant, k):
    ranked = list(dict.fromkeys(ranked))
    relevant = set(relevant)
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else None


def ndcg_at_k(ranked, relevance, k):
    # Stable dedup before ranking so duplicate child chunks cannot inflate evidence recall.
    ranked = list(dict.fromkeys(ranked))
    gains = [2 ** relevance.get(doc, 0) - 1 for doc in ranked[:k]]
    ideal = sorted((2 ** grade - 1 for grade in relevance.values()), reverse=True)[:k]
    normalizer = sum(g / math.log2(i+2) for i, g in enumerate(ideal))
    return sum(g / math.log2(i+2) for i, g in enumerate(gains)) / normalizer if normalizer else None


def grouped_mean(rows, field):
    groups = defaultdict(list)
    for row in rows:
        if row.get(field) is not None:
            groups[row["group_id"]].append(row[field])
    return statistics.mean(statistics.mean(v) for v in groups.values()) if groups else None


def validate_dataset(manifest: dict):
    ids = set()
    group_splits = {}
    origin_splits = {}
    for case in manifest["cases"]:
        if case["case_id"] in ids:
            raise ValueError("duplicate_case_id")
        ids.add(case["case_id"])
        for key, groups in [(case["group_id"], group_splits), (case.get("origin_id", case["group_id"]), origin_splits)]:
            if key in groups and groups[key] != case["split"]:
                raise ValueError("event_group_leaks_across_splits")
            groups[key] = case["split"]
        if case["class"] not in ("A", "N", "U"):
            raise ValueError("invalid_answerability_class")
        if case["class"] == "A" and not case.get("causes"):
            raise ValueError("confirmed_case_requires_causes")
    return True


def score_case(case, job, report):
    report = report or {}
    valid = job["status"] == "succeeded"
    diagnosed = valid and report.get("outcome") == "diagnosed"
    refused = valid and report.get("outcome") in ("insufficient_evidence", "needs_input")
    causes = [h["cause_id"] for h in report.get("hypotheses", [])]
    expected = set(case.get("causes", []))
    ranked = list(dict.fromkeys(c["doc_id"] for c in report.get("retrieval_trace", [])
                               if not c.get("context_expansion_of")))
    relevant = case.get("relevance", {})
    citations = {c["chunk_id"] for c in report.get("citations", [])}
    evidence = {e["evidence_id"] for e in report.get("evidence_bundle", {}).get("evidence", [])}
    claims = report.get("observations", []) + report.get("hypotheses", [])
    resolvable = []
    for claim in claims:
        refs = claim.get("knowledge_ids", []) + claim.get("evidence_ids", [])
        resolvable.extend(ref in citations or ref in evidence for ref in refs)
    return {"case_id": case["case_id"], "group_id": case["group_id"], "class": case["class"],
            "status": job["status"], "outcome": report.get("outcome"),
            "top1": int(diagnosed and bool(causes) and causes[0] in expected) if case["class"] == "A" else None,
            "top3": int(diagnosed and bool(set(causes[:3]) & expected)) if case["class"] == "A" else None,
            "refused": int(refused), "correct_refusal": int(refused) if case["class"] == "U" else None,
            "false_refusal": int(refused) if case["class"] == "A" else None,
            "normal_correct": int(valid and report.get("outcome") == "no_anomaly") if case["class"] == "N" else None,
            "normal_false_refusal": int(refused) if case["class"] == "N" else None,
            "normal_false_alarm": int(diagnosed) if case["class"] == "N" else None,
            "recall40": recall_at_k(ranked, [k for k, v in relevant.items() if v > 0], 40),
            "ndcg5": ndcg_at_k(ranked, relevant, 5),
            "ndcg10": ndcg_at_k(ranked, relevant, 10),
            "citation_resolvable": statistics.mean(resolvable) if resolvable else None,
            "citation_support": None,  # Requires human semantic review, not a heuristic label.
            "degradations": report.get("degradations", []), "usage": report.get("usage", {})}


def verify_execution(predictions, expected_cases, settings):
    """A successful fallback must not count as execution of a configured real adapter."""
    checks = []
    for prediction in predictions:
        job, report = prediction["job"], prediction.get("report") or {}
        trace = report.get("retrieval_trace", [])
        diagnostics = job.get("diagnostics", {})
        retrieval = diagnostics.get("retrieval", {})
        failures = []
        if job["status"] != "succeeded":
            failures.append("job_not_succeeded")
        if settings.retrieval.mode in ("dense", "hybrid") and not any(
                any(route.startswith("dense") for route in c.get("route_scores", {})) for c in trace) and not retrieval.get("dense_executed"):
            failures.append("dense_inference_not_observed")
        if settings.reranker.enabled and not any("rerank" in c for c in trace) and not retrieval.get("reranker_executed"):
            failures.append("reranker_inference_not_observed")
        usage = report.get("usage", {})
        if (not usage.get("generation_validated") or not usage.get("generation_started")
                or usage.get("provider", {}).get("provider") != "dashscope"):
            failures.append("dashscope_validated_output_not_observed")
        if settings.decision.provider == "jev":
            decision = diagnostics.get("decisions", {})
            if decision.get("provider") != "jev":
                failures.append("real_jev_not_configured")
            passages = decision.get("passage_gate", {}).get("passages", [])
            choice = decision.get("check_selection", {})
            if settings.decision.passage_gate_enabled:
                if not any(p.get("answers") for p in passages):
                    failures.append("jev_passage_output_not_observed")
                if any(p.get("reason", "").startswith("jev_") for p in passages):
                    failures.append("jev_passage_degraded")
            if settings.decision.check_selection_enabled:
                legitimate_skip = choice.get("reason") in {"normal_window", "no_legal_check_candidates"}
                if not legitimate_skip and (choice.get("status") != "adopted" or not choice.get("answer")):
                    failures.append("jev_check_selection_not_adopted")
            if ((any(p.get("answers") for p in passages) or choice.get("answer"))
                    and decision.get("actual_model") != settings.decision.model):
                failures.append("jev_model_not_verified")
        checks.append({"case_id": prediction["case_id"], "passed": not failures, "failures": failures})
    expected = {case["case_id"] for case in expected_cases}
    actual = {c["case_id"] for c in checks}
    return {"status": "passed" if actual == expected and all(c["passed"] for c in checks) else "failed",
            "expected_runs": len(expected), "observed_runs": len(checks),
            "passed_runs": sum(c["passed"] for c in checks),
            "missing_runs": sorted(expected - actual), "checks": checks,
            "scope": "synthetic_engineering_validation; not_real_incident_quality_certification"}


def run_evaluation(settings, dataset: Path, output: Path, split="test", suite="pipeline", allow_model_calls=False,
                   require_real_models=False):
    from ..indexing import build_index, load_snapshot, publish_index
    from ..providers.generation import generation_key

    if suite not in {"smoke", "retrieval", "pipeline", "decisions"}:
        raise ValueError("unknown_evaluation_suite")
    manifest = json.loads(dataset.read_text())
    validate_dataset(manifest)
    cases = [c for c in manifest["cases"] if c["split"] == split]
    if not cases:
        raise ValueError("empty_evaluation_split")
    settings = settings.model_copy(deep=True)
    if suite == "smoke":
        settings.embedding.provider = "none"
        settings.retrieval.mode = "bm25"
        settings.reranker.enabled = False
    if suite in {"smoke", "retrieval"}:
        settings.decision.provider = "none"
    if suite == "decisions" and (settings.decision.provider == "none" or not (
            settings.decision.passage_gate_enabled or settings.decision.check_selection_enabled)):
        raise ValueError("decision_suite_requires_jev_or_fixture_provider_and_enabled_stage")
    if settings.decision.provider == "jev" and not allow_model_calls:
        raise ValueError("external_decision_requires_allow_model_calls")
    if not allow_model_calls:
        raise ValueError("external_generation_requires_allow_model_calls")
    generation_key(settings.generation)
    if require_real_models:
        from ..providers.decision import decision_key
        from ..providers.models import model_specs

        if suite == "smoke":
            raise ValueError("real_model_validation_requires_retrieval_pipeline_or_decisions_suite")
        if suite == "decisions":
            if settings.decision.provider != "jev":
                raise ValueError("real_decision_validation_requires_jev")
        elif settings.embedding.provider != "bge" or settings.retrieval.mode not in {"dense", "hybrid"}:
            raise ValueError("real_retrieval_requires_bge_and_dense_or_hybrid")
        if settings.embedding.provider == "bge" or settings.reranker.enabled:
            model_specs(settings)
        if suite == "pipeline" and settings.retrieval.mode != "hybrid":
            raise ValueError("real_pipeline_requires_hybrid_and_dashscope")
        if settings.decision.provider == "jev":
            decision_key(settings.decision)
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("evaluation_output_must_be_new_or_empty")
    output.mkdir(parents=True, exist_ok=True)
    config = settings.model_copy(deep=True)
    config.data_dir = output / "state"
    results, predictions, traces = [], [], []
    with (owner_lock(config.data_dir),
          closing(Repository()) as repository,
          closing(DiagnosisService(config, repository)) as service):
        cutoff = manifest.get("knowledge_cutoff")
        index = build_index(config.model_dump(mode="json"), config.knowledge_dir, label="evaluation", as_of=cutoff)
        publish_index(config.model_dump(mode="json"), "evaluation")
        snapshot = load_snapshot(config.model_dump(mode="json"))
        for case in cases:
            source = (dataset.parent / case["telemetry"]).resolve()
            import_replay(repository, source, case["case_id"], True)
        service.prepare(snapshot)
        for case in cases:
            request = DiagnosisRequest.model_validate({**case["request"], "data_namespace": "replay:" + case["case_id"]})
            bounds = repository.namespace_bounds(request.data_namespace)
            pinned = {"ingest_revision": repository.revision(), "as_of_received_at": iso(request.as_of_received_at or bounds[1]),
                      "generation_id": snapshot.manifest["generation_id"], "knowledge_cutoff": cutoff or iso(utcnow())}
            queued = repository.submit(request, pinned, 32)
            job = repository.claim(60)
            try:
                service.run(job, snapshot, Budget(config.service.pipeline_timeout_s))
            except TimeoutError:
                repository.finish_error(job["job_id"], "timed_out", "deadline_exceeded")
            except Exception as exc:
                repository.finish_error(job["job_id"], "failed", type(exc).__name__ + ":" + str(exc)[:250])
            job = repository.job(queued["job_id"])
            report = repository.report(job["report_id"]) if job["report_id"] else None
            results.append(score_case(case, job, report))
            predictions.append({"case_id": case["case_id"], "job": job, "report": report})
            traces.append({"case_id": case["case_id"], "trace": report.get("retrieval_trace", []) if report else []})
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    environment = {"python": platform.python_version(), "platform": platform.platform(), "processor": platform.processor(),
                   "code_commit": commit, "config_hash": config.fingerprint}
    if require_real_models and (config.embedding.provider == "bge" or config.reranker.enabled):
        from ..providers.models import runtime_environment
        environment["model_runtime"] = runtime_environment()
    source_root = Path(__file__).parents[1]
    environment["python_source_hash"] = digest({str(path.relative_to(source_root)): path.read_text()
                                                for path in sorted(source_root.rglob("*.py"))})
    try:
        environment["git_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    except (OSError, subprocess.SubprocessError):
        environment["git_dirty"] = None
    fields = ("top1", "top3", "recall40", "ndcg5", "ndcg10", "correct_refusal", "false_refusal",
              "normal_correct", "normal_false_refusal", "normal_false_alarm", "citation_resolvable")
    metrics = {"purpose": manifest.get("purpose", "unverified_dataset"), "split": split, "suite": suite,
               "synthetic": manifest.get("synthetic", False), "quality_acceptance": "not_certified",
               "dataset_hash": digest(manifest), "case_count": len(results),
               "failures": sum(row["status"] != "succeeded" for row in results),
               "metrics": {field: grouped_mean(results, field) for field in fields}}
    if require_real_models:
        metrics["real_model_verification"] = verify_execution(predictions, cases, config)
    for name, value in [("metrics.json", metrics), ("environment.json", environment),
                        ("dataset_manifest.json", manifest), ("index_manifest.json", index)]:
        atomic_json(output / name, value)
    import yaml
    atomic_write(output / "config.resolved.yaml", yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True).encode())
    for name, rows in [("predictions.jsonl", predictions), ("retrieval_traces.jsonl", traces), ("case_metrics.jsonl", results)]:
        atomic_write(output / name, "".join(canonical(r)+"\n" for r in rows).encode())
    return {"output": str(output), **metrics}
