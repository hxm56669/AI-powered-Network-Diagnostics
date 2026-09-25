"""Bounded TypeSafe HTTP adapter. Usage, response bodies and credentials are never logged."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time

import httpx

from ..util import canonical, read_api_key, unique_object


class DecisionUnavailable(RuntimeError):
    pass


def decision_capabilities(config):
    result = {"provider": config.provider, "model": config.model,
              "policy_version": config.policy_version, "remote_verified": False,
              "passage_gate_enabled": config.provider != "none" and config.passage_gate_enabled,
              "check_selection_enabled": config.provider != "none" and config.check_selection_enabled,
              "credentials_present": None, "credentials_error": None}
    if config.provider == "jev":
        try:
            decision_key(config)
            result["credentials_present"] = True
        except DecisionUnavailable as exc:
            result.update(credentials_present=False, credentials_error=str(exc))
    return result


def decision_key(config):
    if config.provider != "jev":
        raise DecisionUnavailable("jev_credentials_unavailable")
    try:
        return read_api_key(os.environ.get(config.api_key_env, ""), filename=os.environ.get(config.api_key_file_env))
    except (OSError, ValueError) as exc:
        raise DecisionUnavailable("jev_credentials_unavailable") from exc


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid_probability")
    return value


def validate_response(value, payload):
    """Return an allowlisted receipt; discard the provider's usage entirely."""
    try:
        if not isinstance(value, dict):
            raise ValueError("invalid_response")
        if value.get("model") != payload["model"]:
            raise DecisionUnavailable("jev_model_mismatch")
        answers = value["answers"]
        if not isinstance(answers, dict) or set(answers) != set(payload["questions"]):
            raise ValueError("question_mismatch")
        result = {}
        for name, question in payload["questions"].items():
            answer = answers[name]
            if answer["type"] != question["type"]:
                raise ValueError("type_mismatch")
            if question["type"] == "noul":
                if set(answer) != {"type", "noul"}:
                    raise ValueError("invalid_noul")
                result[name] = {"type": "noul", "noul": probability(answer["noul"])}
            elif question["type"] == "choice":
                if set(answer) != {"type", "choice", "probabilities", "confidence"}:
                    raise ValueError("invalid_choice")
                probabilities = answer["probabilities"]
                if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
                    raise ValueError("choice_mismatch")
                if not math.isclose(sum(probability(v) for v in probabilities.values()), 1, abs_tol=1e-5):
                    raise ValueError("invalid_distribution")
                choice = answer["choice"]
                if choice not in probabilities or probabilities[choice] < max(probabilities.values()):
                    raise ValueError("choice_not_argmax")
                result[name] = {"type": "choice", "choice": choice, "probabilities": probabilities,
                                "confidence": probability(answer["confidence"])}
            else:
                raise ValueError("unsupported_question")
        return {"model": value["model"], "answers": result}
    except (ValueError, TypeError, KeyError) as exc:
        raise DecisionUnavailable("jev_invalid_response") from exc


class DecisionProvider:
    def __init__(self, config):
        self.config = config

    def evaluate_many(self, payloads, budget, deadline):
        budget.check()
        if self.config.provider != "jev":
            return [{"error": "jev_unavailable"} for _ in payloads]
        try:
            key = decision_key(self.config)
        except DecisionUnavailable as exc:
            return [{"error": str(exc)} for _ in payloads]
        return asyncio.run(self._evaluate_many(payloads, budget, deadline, key))

    async def _evaluate_many(self, payloads, budget, deadline, key):
        semaphore = asyncio.Semaphore(self.config.max_concurrency)
        stopped = False
        async with httpx.AsyncClient(follow_redirects=False) as client:
            async def request(payload):
                nonlocal stopped
                async with semaphore:
                    budget.check()
                    if stopped or time.monotonic() >= deadline:
                        return {"error": "jev_budget_skipped"}
                    encoded = canonical(payload).encode()
                    if len(encoded) > self.config.max_payload_bytes:
                        return {"error": "jev_input_too_large"}
                    timeout = min(self.config.request_timeout_s, deadline - time.monotonic(), budget.check())
                    try:
                        async with asyncio.timeout(timeout):
                            async with client.stream("POST", self.config.endpoint, content=encoded,
                                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                                    timeout=timeout) as response:
                                if response.status_code in (401, 403):
                                    raise DecisionUnavailable("jev_auth_failed")
                                if response.status_code != 200:
                                    raise DecisionUnavailable("jev_unavailable")
                                body = bytearray()
                                async for part in response.aiter_bytes():
                                    budget.check()
                                    body.extend(part)
                                    if len(body) > 131072:
                                        raise DecisionUnavailable("jev_invalid_response")
                                try:
                                    value = json.loads(body, object_pairs_hook=unique_object)
                                except (ValueError, UnicodeError) as exc:
                                    raise DecisionUnavailable("jev_invalid_response") from exc
                                return validate_response(value, payload)
                    except (httpx.TimeoutException, TimeoutError):
                        budget.check()  # A job deadline is terminal, a provider deadline is optional.
                        stopped = True
                        return {"error": "jev_timeout"}
                    except httpx.HTTPError:
                        stopped = True
                        return {"error": "jev_unavailable"}
                    except DecisionUnavailable as exc:
                        if str(exc) in {"jev_auth_failed", "jev_unavailable", "jev_model_mismatch"}:
                            stopped = True
                        return {"error": str(exc)}
            tasks = [asyncio.create_task(request(payload)) for payload in payloads]
            try:
                while any(not task.done() for task in tasks):
                    budget.check()
                    await asyncio.wait([task for task in tasks if not task.done()], timeout=.05)
                return [task.result() for task in tasks]
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


class FixtureDecisionProvider:
    """Protocol exercise only: conservative fixed answers, never quality evidence."""
    def __init__(self, config):
        if config.provider != "fixture":
            raise ValueError("fixture_provider_not_configured")

    def evaluate_many(self, payloads, budget, deadline):
        result = []
        for payload in payloads:
            budget.check()
            answers = {}
            for name, question in payload["questions"].items():
                if question["type"] == "noul":
                    answers[name] = {"type": "noul", "noul": 0.0 if name == "counterevidence" else 1.0}
                else:
                    choice = "unknown" if name == "prerequisites" else next(iter(question["criteria"]))
                    answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                                    "probabilities": {key: float(key == choice) for key in question["criteria"]}}
            result.append(validate_response({"model": payload["model"], "answers": answers}, payload))
        return result
