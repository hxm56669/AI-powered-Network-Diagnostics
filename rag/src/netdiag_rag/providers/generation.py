"""Bounded provider adapter. Secrets never enter reports or effective config."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from ..util import CredentialError, read_api_key


class ProviderUnavailable(RuntimeError):
    pass


def generation_key(config):
    """Read a secret at call time; neither its contents nor provider bodies enter errors."""
    key = os.environ.get(config.api_key_env, "").strip()
    filename = os.environ.get(config.api_key_file_env) if not key else None
    try:
        return read_api_key(key, filename=Path(filename).expanduser() if filename else None)
    except (OSError, UnicodeError) as exc:
        raise ProviderUnavailable("generation_key_file_unreadable") from exc
    except CredentialError as exc:
        reason = {"unsafe_key_file": "generation_key_file_requires_owner_only_permissions",
                  "missing_key": "generation_credentials_missing"}.get(str(exc), "generation_credentials_invalid")
        raise ProviderUnavailable(reason) from exc


class GenerationProvider:
    def __init__(self, settings):
        self.settings = settings

    def generate(self, messages, budget):
        # The pipeline is synchronous; the transport uses cancellable asynchronous
        # I/O so cancellation does not wait for a stalled socket timeout.
        # Async callers must offload this sync entry point (as Runtime.worker does).
        return asyncio.run(self._generate(messages, budget))

    async def _generate(self, messages, budget):
        config = self.settings
        key = generation_key(config)
        timeout = min(config.call_timeout_s, budget.check())
        body = {"model": config.model, "messages": messages, "temperature": config.temperature,
                "max_tokens": config.max_output_tokens, "stream": False,
                "response_format": {"type": config.response_format}}
        if config.enable_thinking is not None:
            body["enable_thinking"] = config.enable_thinking
        # HTTP transport has no implicit retries. Caller owns the shared two-call budget.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async def request():
                async with client.stream("POST", config.base_url.rstrip("/") + "/chat/completions",
                               headers={"Authorization": "Bearer " + key}, json=body) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        raise ProviderUnavailable("generation_temporary_failure")
                    if response.status_code != 200:
                        raise ProviderUnavailable("generation_http_" + str(response.status_code))
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        budget.check()
                        data.extend(chunk)
                        if len(data) > 2_000_000:
                            raise ProviderUnavailable("generation_response_too_large")
                    return data

            task = asyncio.create_task(request())
            deadline = time.monotonic() + timeout
            try:
                while True:
                    budget.check()
                    if time.monotonic() >= deadline:
                        raise ProviderUnavailable("generation_temporary_failure")
                    done, _ = await asyncio.wait({task}, timeout=min(.1, budget.check()))
                    if done:
                        data = task.result()
                        break
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        budget.check()
        try:
            value = json.loads(data)
            choice = value["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("invalid_response")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable("generation_invalid_response") from exc
        if choice.get("finish_reason") == "length":
            raise ProviderUnavailable("generation_output_truncated")
        if choice.get("finish_reason") != "stop":
            raise ProviderUnavailable("generation_incomplete_response")
        return content, {
            "provider": "dashscope", "request_id": value.get("id"),
            "model": value.get("model", config.model), "system_fingerprint": value.get("system_fingerprint")}
