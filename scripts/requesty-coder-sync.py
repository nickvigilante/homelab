#!/usr/bin/env python3
"""Sync the Requesty model catalog into Coder Agents.

Subcommands:
  check  report drift between Requesty and Coder (read-only)
  apply  make Coder match Requesty (creates and updates, never deletes)

Environment:
  CODER_URL             Coder base URL (default https://coder.vigihome.net)
  CODER_SESSION_TOKEN   Coder API token
  REQUESTY_API_KEY      Requesty key, needed by apply to create providers
  UPTIME_KUMA_PUSH_URL  optional push monitor URL, pinged after check

Exit codes: 0 in sync, 1 drift, 2 error.
"""

# TEMPORARY: the header imports names that later sections of this script use.
# Remove this line once the last section lands.
# ruff: noqa: F401

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

REQUESTY_MODELS_URL = "https://router.requesty.ai/v1/models"
REQUESTY_BASE_URL = "https://router.requesty.ai/v1"
LOGO_BASE_URL = "https://www.requesty.ai/provider_logos/v2/"
FALLBACK_ICON = "https://www.requesty.ai/Requesty_logo.svg"
DEFAULT_CODER_URL = "https://coder.vigihome.net"
PROVIDER_SUFFIX = "-via-requesty"
FREE_PROVIDER_NAME = "free" + PROVIDER_SUFFIX
MIN_ELIGIBLE_MODELS = 100
USER_AGENT = "requesty-coder-sync/1.0"

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2

# Smoke-test outcomes live here, so a fallback is a one-line edit.
# NATIVE_TYPES maps a lab to the Coder provider type that speaks its protocol;
# every other lab uses "openai". BASE_URL_BY_TYPE overrides the base URL for a
# provider type whose client appends its own "/v1".
NATIVE_TYPES = {"anthropic": "anthropic", "google": "google"}
BASE_URL_BY_TYPE: dict[str, str] = {}

LAB_ALIASES = {"moonshotai": "moonshot", "qwen": "alibaba"}

# Snapshot of https://www.requesty.ai/provider_logos/v2/<logo>.png
LAB_LOGOS = {
    "alibaba": "alibaba",
    "anthropic": "anthropic",
    "deepinfra": "deepinfra",
    "deepseek": "deepseek",
    "google": "google",
    "meta": "meta",
    "minimax": "minimaxi",
    "mistral": "mistral",
    "moonshot": "moonshot",
    "nvidia": "nvidia",
    "openai": "openai",
    "sakana": "sakana",
    "thinkingmachines": "thinkingmachines",
    "xai": "xai",
    "xiaomi": "xiaomi",
    "zai": "zai",
}

LAB_NAMES = {
    "alibaba": "Alibaba",
    "anthropic": "Anthropic",
    "bytedance": "ByteDance",
    "deepinfra": "DeepInfra",
    "deepseek": "DeepSeek",
    "google": "Google",
    "gryphe": "Gryphe",
    "inclusionai": "inclusionAI",
    "kwaipilot": "Kwaipilot",
    "meta": "Meta",
    "minimax": "MiniMax",
    "mistral": "Mistral",
    "moonshot": "Moonshot AI",
    "nousresearch": "Nous Research",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "sakana": "Sakana AI",
    "stepfun": "StepFun",
    "tencent": "Tencent",
    "thinkingmachines": "Thinking Machines",
    "xai": "xAI",
    "xiaomi": "Xiaomi",
    "zai": "Z.ai",
}

# input, output, cache read, cache write; micro-dollars per million tokens.
Prices = tuple[int | None, int | None, int | None, int | None]


class SyncError(Exception):
    """A fatal problem; the CLI exits with code 2."""


class ApiError(SyncError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} failed ({status or 'no response'}): {body}")
        self.status = status


@dataclass(frozen=True)
class DesiredProvider:
    name: str
    display_name: str
    type: str
    base_url: str
    icon: str


@dataclass(frozen=True)
class DesiredModel:
    provider: str
    provider_type: str
    model: str
    display_name: str
    context_limit: int
    max_output_tokens: int | None
    prices: Prices


@dataclass
class Desired:
    providers: dict[str, DesiredProvider]
    models: dict[str, DesiredModel]  # keyed by Requesty model ID
    info: list[str]
