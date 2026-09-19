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
import http.client
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


# ---- catalog selection -----------------------------------------------------


def is_eligible(entry: dict[str, Any]) -> bool:
    return (
        entry.get("api") == "chat"
        and entry.get("supports_tool_calling") is True
        and entry.get("input_price") is not None
        and entry.get("output_price") is not None
        and (entry.get("context_window") or 0) > 0
    )


def is_retiring(entry: dict[str, Any]) -> bool:
    """Requesty lists a retirement date (Unix seconds) for this entry."""
    return entry.get("retires") is not None


def retire_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def is_free(entry: dict[str, Any]) -> bool:
    # An explicit zero, never a missing price.
    return entry.get("input_price") == 0 and entry.get("output_price") == 0


def lab_of(entry: dict[str, Any]) -> str:
    lab = entry.get("model_lab") or "unknown"
    return LAB_ALIASES.get(lab, lab)


def canonical_of(entry: dict[str, Any]) -> str:
    return entry.get("model_canonical_name") or entry["id"]


def is_plain(entry: dict[str, Any]) -> bool:
    """No @region suffix and no :variant (service tier) suffix."""
    tail = entry["id"].rsplit("/", 1)[-1]
    return "@" not in tail and ":" not in tail


def host_of(entry: dict[str, Any]) -> str:
    return entry["id"].split("/", 1)[0]


def pick(entries: list[dict[str, Any]], lab: str) -> dict[str, Any]:
    """First-party plain entry, else cheapest plain, else cheapest overall."""

    def rank(entry: dict[str, Any]) -> tuple[bool, bool, float, str]:
        plain = is_plain(entry)
        first_party = plain and host_of(entry) == lab
        price = entry["input_price"] + entry["output_price"]
        return (not first_party, not plain, price, entry["id"])

    return min(entries, key=rank)


def select(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """One entry per canonical model. A canonical name that spans several labs
    stays with the lab holding the most entries (ties go alphabetically)."""
    by_canonical: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for entry in entries:
        labs = by_canonical.setdefault(canonical_of(entry), {})
        labs.setdefault(lab_of(entry), []).append(entry)
    chosen: list[dict[str, Any]] = []
    notes: list[str] = []
    for canonical in sorted(by_canonical):
        labs = by_canonical[canonical]
        counts = {lab: len(items) for lab, items in labs.items()}
        winner = max(sorted(counts), key=counts.__getitem__)
        for lab in sorted(counts):
            if lab != winner:
                notes.append(f"collapsed {canonical}: kept lab {winner}, dropped lab {lab}")
        chosen.append(pick(labs[winner], winner))
    return chosen, notes


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def micro(per_token: float | None) -> int | None:
    """USD per token to micro-dollars per million tokens."""
    if per_token is None:
        return None
    return round(per_token * 1e12)


def icon_for(lab: str) -> str:
    logo = LAB_LOGOS.get(lab)
    return f"{LOGO_BASE_URL}{logo}.png" if logo else FALLBACK_ICON


def lab_provider(lab: str) -> DesiredProvider:
    provider_type = NATIVE_TYPES.get(lab, "openai")
    return DesiredProvider(
        name=slugify(lab) + PROVIDER_SUFFIX,
        display_name=f"{LAB_NAMES.get(lab, lab.title())} via Requesty",
        type=provider_type,
        base_url=BASE_URL_BY_TYPE.get(provider_type, REQUESTY_BASE_URL),
        icon=icon_for(lab),
    )


def free_provider() -> DesiredProvider:
    return DesiredProvider(
        name=FREE_PROVIDER_NAME,
        display_name="Free models via Requesty",
        type="openai",
        base_url=BASE_URL_BY_TYPE.get("openai", REQUESTY_BASE_URL),
        icon=FALLBACK_ICON,
    )


def build_desired(entries: list[dict[str, Any]]) -> Desired:
    candidates = [e for e in entries if is_eligible(e)]
    eligible = [e for e in candidates if not is_retiring(e)]
    if len(eligible) < MIN_ELIGIBLE_MODELS:
        raise SyncError(
            f"catalog has only {len(eligible)} eligible chat models "
            f"(minimum {MIN_ELIGIBLE_MODELS}); refusing to continue"
        )
    info = sorted(
        f"skipped (free, no tool calling): {e['id']}"
        for e in entries
        if e.get("api") == "chat" and e.get("supports_tool_calling") is not True and is_free(e)
    )
    desired = Desired(providers={}, models={}, info=info)
    pools = (
        (True, [e for e in eligible if is_free(e)]),
        (False, [e for e in eligible if not is_free(e)]),
    )
    for free, pool in pools:
        chosen, notes = select(pool)
        desired.info.extend(notes)
        for entry in chosen:
            provider = free_provider() if free else lab_provider(lab_of(entry))
            if not is_plain(entry):
                desired.info.append(f"only a non-plain entry is available: {entry['id']}")
            desired.providers.setdefault(provider.name, provider)
            prices: Prices = (
                (0, 0, 0, 0)
                if free
                else (
                    micro(entry["input_price"]),
                    micro(entry["output_price"]),
                    micro(entry.get("cached_price")),
                    None,
                )
            )
            desired.models[entry["id"]] = DesiredModel(
                provider=provider.name,
                provider_type=provider.type,
                model=entry["id"],
                display_name=canonical_of(entry),
                context_limit=int(entry["context_window"]),
                max_output_tokens=int(entry.get("max_output_tokens") or 0) or None,
                prices=prices,
            )
    registered = {m.display_name for m in desired.models.values()}
    retiring: dict[str, float] = {}
    for entry in candidates:
        name = canonical_of(entry)
        if is_retiring(entry) and name not in registered:
            retiring[name] = min(retiring.get(name, entry["retires"]), entry["retires"])
    desired.info.extend(
        f"skipped (retires {retire_date(timestamp)}): {name}"
        for name, timestamp in sorted(retiring.items())
    )
    return desired


# ---- catalog and Coder API -------------------------------------------------


def fetch_catalog(url: str = REQUESTY_MODELS_URL, timeout: float = 60.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError, http.client.HTTPException) as err:
        raise SyncError(f"fetch {url}: {err}") from err
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SyncError(f"fetch {url}: response has no 'data' list")
    return data


class CoderClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Coder-Session-Token": self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            try:
                detail = err.read().decode(errors="replace")[:500]
            except (OSError, http.client.HTTPException):
                detail = ""
            raise ApiError(method, path, err.code, detail) from err
        except (OSError, http.client.HTTPException) as err:
            raise ApiError(method, path, 0, str(err)) from err
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as err:
            raise ApiError(method, path, 0, f"invalid JSON in response: {err}") from err

    def default_org_id(self) -> str:
        for org in self.request("GET", "/api/v2/organizations"):
            if org.get("is_default"):
                return org["id"]
        raise SyncError("Coder has no default organization")

    def list_providers(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v2/ai/providers")

    def create_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v2/ai/providers", payload)

    def update_provider(self, provider_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/api/v2/ai/providers/{provider_id}", payload)

    def list_models(self, org_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/api/v2/organizations/{org_id}/chats/models")["models"]

    def create_model(self, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v2/organizations/{org_id}/chats/models", payload)

    def update_model(self, org_id: str, model_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/api/v2/organizations/{org_id}/chats/models/{model_id}", payload)

    def list_custom_prices(self) -> list[dict[str, Any]]:
        return self.request("GET", "/api/experimental/ai/model-prices?source=custom")

    def upsert_prices(self, prices: list[dict[str, Any]]) -> None:
        self.request("POST", "/api/experimental/ai/model-prices", {"prices": prices})


@dataclass
class Live:
    org_id: str
    providers: dict[str, dict[str, Any]]  # managed providers by name
    models: list[dict[str, Any]]  # models under managed providers
    prices: dict[tuple[str, str], dict[str, Any]]  # custom prices by (type, model)


def is_managed_name(name: str) -> bool:
    return name.endswith(PROVIDER_SUFFIX)


def load_live(client: CoderClient) -> Live:
    try:
        org_id = client.default_org_id()
        providers = {p["name"]: p for p in client.list_providers() if is_managed_name(p["name"])}
        managed_ids = {p["id"] for p in providers.values()}
        models = [m for m in client.list_models(org_id) if m["ai_provider_id"] in managed_ids]
        prices = {(p["provider"], p["model"]): p for p in client.list_custom_prices()}
    except (KeyError, TypeError, AttributeError) as err:
        raise SyncError(f"unexpected response shape from Coder: {err!r}") from err
    return Live(org_id=org_id, providers=providers, models=models, prices=prices)


# ---- drift detection -------------------------------------------------------

MISSING_PROVIDER = "MISSING_PROVIDER"
MISSING_MODEL = "MISSING_MODEL"
PROVIDER_DRIFT = "PROVIDER_DRIFT"
MODEL_DRIFT = "MODEL_DRIFT"
PRICE_DRIFT = "PRICE_DRIFT"
ORPHAN_MODEL = "ORPHAN_MODEL"
INFO = "INFO"
DRIFT_CATEGORIES = (
    MISSING_PROVIDER,
    MISSING_MODEL,
    PROVIDER_DRIFT,
    MODEL_DRIFT,
    PRICE_DRIFT,
    ORPHAN_MODEL,
)


@dataclass
class Finding:
    category: str
    subject: str
    detail: str = ""
    provider: str = ""
    action: dict[str, Any] | None = None


def provider_changes(want: DesiredProvider, have: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field in ("base_url", "icon", "display_name"):
        if have.get(field) != getattr(want, field):
            changes[field] = getattr(want, field)
    if have.get("enabled") is not True:
        changes["enabled"] = True
    return changes


def model_changes(
    want: DesiredModel, have: dict[str, Any], provider_names: dict[str, str]
) -> tuple[dict[str, Any], list[str], str | None]:
    """Returns the PATCH payload, human-readable notes, and a provider to move to."""
    payload: dict[str, Any] = {}
    notes: list[str] = []
    move_to = None
    current = provider_names.get(have["ai_provider_id"])
    if current != want.provider:
        move_to = want.provider
        notes.append(f"provider {current} -> {want.provider}")
    if have.get("context_limit") != want.context_limit:
        payload["context_limit"] = want.context_limit
        notes.append(f"context_limit {have.get('context_limit')} -> {want.context_limit}")
    config = have.get("model_config") or {}
    if (
        want.max_output_tokens is not None
        and config.get("max_output_tokens") != want.max_output_tokens
    ):
        # Merge, so tuning an operator set in the UI (temperature, ...) survives.
        payload["model_config"] = {**config, "max_output_tokens": want.max_output_tokens}
        notes.append(
            f"max_output_tokens {config.get('max_output_tokens')} -> {want.max_output_tokens}"
        )
    return payload, notes, move_to


def price_payload(want: DesiredModel) -> dict[str, Any]:
    input_price, output_price, cache_read, cache_write = want.prices
    return {
        "provider": want.provider_type,
        "model": want.model,
        "input_price": input_price,
        "output_price": output_price,
        "cache_read_price": cache_read,
        "cache_write_price": cache_write,
    }


def compute_diff(desired: Desired, live: Live) -> list[Finding]:
    findings: list[Finding] = []

    for name in sorted(desired.providers):
        want = desired.providers[name]
        have = live.providers.get(name)
        if have is None:
            detail = f"create ({want.type}, {want.base_url})"
            action = {"op": "create_provider", "provider": want}
            findings.append(Finding(MISSING_PROVIDER, name, detail, name, action))
            continue
        changes = provider_changes(want, have)
        if changes:
            detail = ", ".join(f"{k}: {have.get(k)!r} -> {v!r}" for k, v in changes.items())
            action = {"op": "update_provider", "id": have["id"], "name": name, "payload": changes}
            findings.append(Finding(PROVIDER_DRIFT, name, detail, name, action))
        if not have.get("api_keys"):
            action = {"op": "set_key", "id": have["id"], "name": name}
            findings.append(Finding(PROVIDER_DRIFT, name, "no API key configured", name, action))

    provider_names = {p["id"]: name for name, p in live.providers.items()}
    live_by_model = {m["model"]: m for m in live.models}

    for model_id in sorted(desired.models):
        want = desired.models[model_id]
        have = live_by_model.get(model_id)
        if have is None:
            detail = f"create under {want.provider}"
            action = {"op": "create_model", "model": want}
            findings.append(Finding(MISSING_MODEL, model_id, detail, want.provider, action))
            continue
        payload, notes, move_to = model_changes(want, have, provider_names)
        if notes:
            action = {
                "op": "update_model",
                "id": have["id"],
                "payload": payload,
                "move_to": move_to,
            }
            findings.append(Finding(MODEL_DRIFT, model_id, "; ".join(notes), want.provider, action))

    for have in sorted(live.models, key=lambda m: m["model"]):
        if have["model"] not in desired.models and have.get("enabled"):
            owner = provider_names.get(have["ai_provider_id"], "")
            action = {"op": "disable_model", "id": have["id"]}
            detail = f"no longer selected (under {owner})"
            findings.append(Finding(ORPHAN_MODEL, have["model"], detail, owner, action))

    for model_id in sorted(desired.models):
        want = desired.models[model_id]
        have = live.prices.get((want.provider_type, model_id))
        current = (
            None
            if have is None
            else (
                have.get("input_price"),
                have.get("output_price"),
                have.get("cache_read_price"),
                have.get("cache_write_price"),
            )
        )
        if current != want.prices:
            detail = "absent" if current is None else f"{current} -> {want.prices}"
            action = {"op": "upsert_price", "price": price_payload(want)}
            findings.append(Finding(PRICE_DRIFT, model_id, detail, want.provider, action))

    findings.extend(Finding(INFO, line) for line in desired.info)
    return findings


def has_drift(findings: list[Finding]) -> bool:
    return any(f.category != INFO for f in findings)


def summarize(findings: list[Finding]) -> str:
    drift = [f for f in findings if f.category != INFO]
    if not drift:
        return "in sync"
    counts = Counter(f.category for f in drift)
    parts = [f"{counts[c]} {c.lower().replace('_', ' ')}" for c in DRIFT_CATEGORIES if counts[c]]
    text = "drift: " + ", ".join(parts)
    new_free = sum(
        1
        for f in drift
        if (
            (f.category == MISSING_MODEL and f.provider == FREE_PROVIDER_NAME)
            or (f.category == MODEL_DRIFT and (f.action or {}).get("move_to") == FREE_PROVIDER_NAME)
        )
    )
    if new_free:
        text += f"; {new_free} new free model{'s' if new_free != 1 else ''}"
    return text


def format_report(findings: list[Finding], summary: str) -> str:
    lines: list[str] = []
    for category in (*DRIFT_CATEGORIES, INFO):
        group = [f for f in findings if f.category == category]
        if not group:
            continue
        lines.append(f"{category} ({len(group)})")
        lines.extend(f"  {f.subject}: {f.detail}" if f.detail else f"  {f.subject}" for f in group)
        lines.append("")
    lines.append(summary)
    return "\n".join(lines)
