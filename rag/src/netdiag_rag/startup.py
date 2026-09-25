"""Optional Jev selection for CLI service startup; secrets stay out of settings."""
from __future__ import annotations

import getpass
import os
import sys
import warnings
from contextlib import contextmanager

from .providers.decision import DecisionUnavailable, decision_key


def interactive_terminal():
    return sys.stdin.isatty() and sys.stderr.isatty()


def _choose_jev():
    while True:
        print("是否启用 Jev（知识适用性判断、下一步检查选择）？[y/N] ",
              end="", file=sys.stderr, flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer in ("", "n", "no", "否"):
            return "off"
        if answer in ("y", "yes", "是"):
            return "on"
        print("请输入 y 或 n；直接回车表示不启用。", file=sys.stderr)


def _read_key():
    # getpass normally falls back to echoed input if it cannot control the TTY.
    # Treat that warning as an error before it reads any credential.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass("请输入 TypeSafe API Key（隐藏输入，留空跳过 Jev）：",
                                   stream=sys.stderr).strip()
    except (getpass.GetPassWarning, EOFError) as exc:
        raise ValueError("无法安全读取 TypeSafe API Key；请配置私有凭据文件，或使用 --jev off。") from exc


@contextmanager
def configure_jev(settings, choice=None):
    """Resolve startup choice and keep an entered key only for the server lifetime."""
    interactive = interactive_terminal()
    config = settings.decision
    if choice is None and config.provider == "none" and interactive:
        choice = _choose_jev()
    if choice is not None:
        config.provider = "jev" if choice == "on" else "none"
        config.passage_gate_enabled = config.check_selection_enabled = choice == "on"
    if config.provider != "jev" or not (config.passage_gate_enabled or config.check_selection_enabled):
        if interactive and choice == "off":
            print("Jev 未启用，继续使用基础 RAG，无需 TypeSafe API Key。", file=sys.stderr)
        yield
        return

    try:
        decision_key(config)
        configured = True
    except DecisionUnavailable:
        configured = False
    if configured:
        yield
        return
    if not interactive:
        raise ValueError("启用 Jev 需要有效的 TypeSafe API Key；请配置 "
                         f"{config.api_key_file_env} 或 {config.api_key_env}，或使用 --jev off。")

    print("尚无可用的 TypeSafe 凭据。输入的密钥仅用于本次服务运行。", file=sys.stderr)
    key = _read_key()
    if not key:
        config.provider = "none"
        config.passage_gate_enabled = config.check_selection_enabled = False
        print("已跳过 Jev，继续使用基础 RAG。", file=sys.stderr)
        yield
        return

    previous = {name: os.environ.get(name) for name in (config.api_key_env, config.api_key_file_env)}
    try:
        # An explicitly entered key replaces a missing/invalid file for this run.
        os.environ.pop(config.api_key_file_env, None)
        os.environ[config.api_key_env] = key
        try:
            decision_key(config)
        except DecisionUnavailable as exc:
            raise ValueError("TypeSafe API Key 格式无效；请重新启动并输入密钥，或使用 --jev off。") from exc
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
