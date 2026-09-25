from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
import time
from pathlib import Path

import httpx

from .contracts import export_schemas
from .settings import load_settings
from .storage import TERMINAL
from .util import canonical, owner_lock


def parser():
    p = argparse.ArgumentParser(prog="netdiag-rag", description="有证据的网络诊断与可重复评测")
    p.add_argument("--config", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    models = sub.add_parser("models").add_subparsers(dest="action", required=True)
    models.add_parser("download")
    verify = models.add_parser("verify")
    verify.add_argument("--output", type=Path, required=True)
    schemas = sub.add_parser("schemas")
    schemas.add_argument("--output", type=Path, default=Path("schemas"))
    knowledge = sub.add_parser("knowledge").add_subparsers(dest="action", required=True)
    validate = knowledge.add_parser("validate")
    validate.add_argument("directory", nargs="?", type=Path)
    index = sub.add_parser("index").add_subparsers(dest="action", required=True)
    index.add_parser("recover")
    gc = index.add_parser("gc")
    gc.add_argument("--dry-run", action="store_true")
    build = index.add_parser("build")
    build.add_argument("--label", default="initial")
    build.add_argument("--as-of")
    publish = index.add_parser("publish")
    publish.add_argument("--label", default="initial")
    publish.add_argument("--online", action="store_true")
    publish.add_argument("--url", default="http://127.0.0.1:8091")
    sub.add_parser("capabilities").add_argument("--url", default="http://127.0.0.1:8091")
    serve = sub.add_parser("serve")
    serve.add_argument("--source", choices=["logs", "replay"])
    serve.add_argument("--replay", type=Path, help="启动时把 JSONL 回放文件读入本次会话内存")
    serve.add_argument("--replay-id", help="回放命名空间，默认使用文件名（不含扩展名）")
    serve.add_argument("--server-executable", type=Path, help="诊断服务可执行文件；从其所在目录启动")
    serve.add_argument("--jev", choices=["on", "off"],
                       help="选择是否启用 Jev 两项决策；未指定时沿用配置，终端下默认关闭时询问")
    diagnosis = sub.add_parser("diagnose")
    diagnosis.add_argument("--request", type=Path, required=True)
    diagnosis.add_argument("--url", default="http://127.0.0.1:8091")
    diagnosis.add_argument("--wait", action="store_true")
    diagnosis.add_argument("--wait-timeout", type=float, default=90)
    diagnosis.add_argument("--idempotency-key")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--dataset", type=Path, default=Path("datasets/manifests/fixtures.json"))
    evaluate.add_argument("--suite", choices=["smoke", "retrieval", "pipeline", "decisions"], default="pipeline",
                          help="单次检查：BM25 冒烟、当前检索配置、完整链路或 Jev 决策；均调用生成模型")
    evaluate.add_argument("--split", choices=["train", "validation", "test"], default="test")
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--allow-model-calls", action="store_true")
    evaluate.add_argument("--require-real-models", action="store_true",
                          help="要求当前配置的真实适配器执行成功；pipeline 还要求 DashScope 输出通过校验")
    return p


def doctor(settings):
    from . import __version__
    from .providers.generation import generation_key, ProviderUnavailable
    from .providers.decision import decision_capabilities
    try:
        generation_key(settings.generation)
        credentials_present, credentials_error = True, None
    except ProviderUnavailable as exc:
        credentials_present, credentials_error = False, str(exc)
    modules = {name: importlib.util.find_spec(name) is not None for name in
               ("fastapi", "pydantic", "bm25s", "jieba", "faiss", "transformers", "torch")}
    return {"version": __version__, "python": platform.python_version(), "platform": platform.platform(),
            "profile": settings.profile, "data_dir": str(settings.data_dir),
            "knowledge_dir": str(settings.knowledge_dir), "config_hash": settings.fingerprint,
            "dependencies": modules, "log_executable_available": os.access(settings.telemetry.executable, os.X_OK),
            "generation_credentials_present": credentials_present, "generation_credentials_error": credentials_error,
            "decision": decision_capabilities(settings.decision),
            "models": {"embedding": settings.embedding.model, "embedding_revision": settings.embedding.revision,
                       "reranker": settings.reranker.model, "reranker_revision": settings.reranker.revision},
            "model_weights_checked": False, "notes": ["模型加载和资源占用由构建/预热验证；doctor 不发送付费请求。"]}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        config = settings.model_dump(mode="json")
        if args.command == "doctor":
            result = doctor(settings)
        elif args.command == "models":
            from .providers.models import download_models, verify_models
            from .util import atomic_json
            if args.action == "download":
                result = download_models(settings)
            else:
                if args.output.exists():
                    raise ValueError("model_verification_output_already_exists")
                result = verify_models(settings)
                atomic_json(args.output, result)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["status"] == "passed" else 1
        elif args.command == "schemas":
            export_schemas(args.output)
            result = {"output": str(args.output.resolve())}
        elif args.command == "knowledge":
            from .knowledge import load_knowledge
            docs = load_knowledge(args.directory or settings.knowledge_dir)
            result = {"documents": len(docs), "status": "valid"}
        elif args.command == "index":
            from .indexing import build_index, publish_index, recover_current, collect_generations
            if args.action == "build":
                result = build_index(config, settings.knowledge_dir, label=args.label, as_of=args.as_of)
            elif args.action in ("gc", "recover"):
                with owner_lock(settings.data_dir):
                    if args.action == "recover":
                        snapshot = recover_current(config)
                        result = {"generation_id": snapshot.generation_id, "recovery": snapshot.recovery}
                    else:
                        result = collect_generations(config, set(), dry_run=args.dry_run)
            elif args.online:
                token = os.environ.get(settings.service.admin_token_env, "")
                response = httpx.post(args.url + "/v1/admin/indexes/publish", json={"label": args.label},
                                      headers={"X-Admin-Token": token}, timeout=60)
                response.raise_for_status()
                result = response.json()
            else:
                with owner_lock(settings.data_dir):
                    result = publish_index(config, args.label)
        elif args.command == "serve":
            from .api import create_app
            from .lifecycle import run_server
            from .observability import configure_logging
            from .startup import configure_jev
            if args.source:
                settings.telemetry.source = args.source
            if args.server_executable:
                settings.telemetry.executable = args.server_executable.resolve()
            if args.replay_id and not args.replay:
                raise ValueError("replay_id_requires_replay_file")
            if args.replay:
                if settings.telemetry.source == "logs" and args.source == "logs":
                    raise ValueError("replay_file_conflicts_with_logs_source")
                settings.telemetry.source = "replay"
            replays = [(args.replay.resolve(), args.replay_id or args.replay.stem)] if args.replay else []
            with configure_jev(settings, args.jev):
                configure_logging()
                run_server(create_app(settings, replays=replays), settings)
            return 0
        elif args.command == "diagnose":
            headers = {"Idempotency-Key": args.idempotency_key} if args.idempotency_key else {}
            with httpx.Client(timeout=30) as client:
                response = client.post(args.url + "/v1/diagnoses", json=json.loads(args.request.read_text()), headers=headers)
                response.raise_for_status()
                result = response.json()
                if args.wait:
                    deadline = time.monotonic() + args.wait_timeout
                    while time.monotonic() < deadline:
                        response = client.get(args.url + "/v1/diagnoses/" + result["job_id"])
                        response.raise_for_status()
                        result = response.json()
                        if result["status"] in TERMINAL:
                            break
                        time.sleep(.2)
                    else:
                        raise TimeoutError("client_wait_timeout_job_still_running")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] not in ("failed", "timed_out", "cancelled") else 1
        elif args.command == "capabilities":
            response = httpx.get(args.url + "/v1/capabilities", timeout=10)
            response.raise_for_status()
            result = response.json()
        else:
            from .evaluation import run_evaluation
            result = run_evaluation(settings, args.dataset, args.output, args.split, args.suite,
                                    args.allow_model_calls, args.require_real_models)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("failures", 0) or result.get("real_model_verification", {}).get("status") == "failed":
            return 1
        return 0
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, ImportError, httpx.HTTPError) as exc:
        print(canonical({"error": type(exc).__name__, "detail": str(exc)[:1200]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
