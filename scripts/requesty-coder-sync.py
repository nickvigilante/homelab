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
    try:
        return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(timestamp)


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


HOST_ALIASES = {"minimaxi": "minimax"}


def host_of(entry: dict[str, Any]) -> str:
    host = entry["id"].split("/", 1)[0]
    return HOST_ALIASES.get(host, host)


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

    def _get_list(self, path: str) -> list[dict[str, Any]]:
        result = self.request("GET", path)
        if not isinstance(result, list):
            raise ApiError("GET", path, 0, f"expected a JSON list, got {type(result).__name__}")
        return result

    def default_org_id(self) -> str:
        for org in self._get_list("/api/v2/organizations"):
            if org.get("is_default"):
                return org["id"]
        raise SyncError("Coder has no default organization")

    def list_providers(self) -> list[dict[str, Any]]:
        return self._get_list("/api/v2/ai/providers")

    def create_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v2/ai/providers", payload)

    def update_provider(self, provider_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/api/v2/ai/providers/{provider_id}", payload)

    def list_models(self, org_id: str) -> list[dict[str, Any]]:
        path = f"/api/v2/organizations/{org_id}/chats/models"
        models = self.request("GET", path)["models"]
        if not isinstance(models, list):
            raise ApiError("GET", path, 0, f"expected a JSON list, got {type(models).__name__}")
        return models

    def create_model(self, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/v2/organizations/{org_id}/chats/models", payload)

    def update_model(self, org_id: str, model_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/api/v2/organizations/{org_id}/chats/models/{model_id}", payload)

    def list_custom_prices(self) -> list[dict[str, Any]]:
        return self._get_list("/api/experimental/ai/model-prices?source=custom")

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
        if have.get("enabled") is False:
            findings.append(Finding(INFO, f"selected but disabled in Coder: {model_id}"))

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


# ---- apply -----------------------------------------------------------------


def apply_changes(
    client: CoderClient,
    live: Live,
    findings: list[Finding],
    api_key: str | None,
    rotate_key: bool,
    log: Callable[[str], None],
) -> None:
    """Runs the findings' actions in dependency order. Never deletes anything."""
    actions = [f.action for f in findings if f.action]

    def of(op: str) -> list[dict[str, Any]]:
        return [a for a in actions if a["op"] == op]

    needs_key = rotate_key or bool(of("create_provider")) or bool(of("set_key"))
    if needs_key and not api_key:
        raise SyncError("REQUESTY_API_KEY is required for the pending changes")

    provider_ids = {name: p["id"] for name, p in live.providers.items()}
    names_by_id = {p["id"]: name for name, p in live.providers.items()}

    for action in of("create_provider"):
        provider = action["provider"]
        created = client.create_provider(
            {
                "type": provider.type,
                "name": provider.name,
                "display_name": provider.display_name,
                "icon": provider.icon,
                "enabled": True,
                "base_url": provider.base_url,
                "api_keys": [api_key],
            }
        )
        provider_ids[provider.name] = created["id"]
        log(f"created provider {provider.name}")

    for action in of("update_provider"):
        client.update_provider(action["id"], action["payload"])
        log(f"updated provider {action['name']}")

    rekey = {a["id"] for a in of("set_key")}
    if rotate_key:
        rekey |= set(names_by_id)
    for provider_id in sorted(rekey):
        client.update_provider(provider_id, {"api_keys": [{"api_key": api_key}]})
        log(f"set API key on provider {names_by_id[provider_id]}")

    created_models = 0
    for action in of("create_model"):
        model = action["model"]
        payload: dict[str, Any] = {
            "ai_provider_id": provider_ids[model.provider],
            "model": model.model,
            "display_name": model.display_name,
            "enabled": True,
            "context_limit": model.context_limit,
        }
        if model.max_output_tokens:
            payload["model_config"] = {"max_output_tokens": model.max_output_tokens}
        client.create_model(live.org_id, payload)
        created_models += 1
    if created_models:
        log(f"created {created_models} model(s)")

    updated_models = 0
    for action in of("update_model"):
        payload = dict(action["payload"])
        if action["move_to"]:
            payload["ai_provider_id"] = provider_ids[action["move_to"]]
        client.update_model(live.org_id, action["id"], payload)
        updated_models += 1
    if updated_models:
        log(f"updated {updated_models} model(s)")

    disabled = of("disable_model")
    for action in disabled:
        client.update_model(live.org_id, action["id"], {"enabled": False})
    if disabled:
        log(f"disabled {len(disabled)} orphaned model(s)")

    prices = [a["price"] for a in of("upsert_price")]
    if prices:
        client.upsert_prices(prices)
        log(f"upserted {len(prices)} price(s)")


# ---- command line ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--catalog-file", help="read the Requesty catalog from this JSON file, not the network"
    )
    parser = argparse.ArgumentParser(
        prog="requesty-coder-sync.py", description="Sync the Requesty catalog into Coder Agents."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", parents=[common], help="report drift (read-only)")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    apply = commands.add_parser("apply", parents=[common], help="make Coder match Requesty")
    apply.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    apply.add_argument(
        "--disable-orphans",
        action="store_true",
        help="disable models that Requesty no longer selects (never deletes)",
    )
    apply.add_argument(
        "--rotate-key",
        action="store_true",
        help="replace the API key on every managed provider with REQUESTY_API_KEY",
    )
    return parser


def emit(findings: list[Finding], summary: str, as_json: bool, out: TextIO) -> None:
    if as_json:
        payload = {
            "summary": summary,
            "drift": has_drift(findings),
            "findings": [
                {
                    "category": f.category,
                    "subject": f.subject,
                    "detail": f.detail,
                    "provider": f.provider,
                }
                for f in findings
            ],
        }
        json.dump(payload, out, indent=2)
        out.write("\n")
    else:
        out.write(format_report(findings, summary) + "\n")


def push_heartbeat(url: str, status: str, message: str) -> None:
    query = urllib.parse.urlencode({"status": status, "msg": message[:200], "ping": ""})
    separator = "&" if "?" in url else "?"
    try:
        request = urllib.request.Request(
            f"{url}{separator}{query}", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except OSError as err:
        print(f"warning: heartbeat failed: {err}", file=sys.stderr)
    except (ValueError, http.client.HTTPException) as err:
        # The message can embed the push URL, which carries a token: name the class only.
        print(f"warning: heartbeat failed: {type(err).__name__}", file=sys.stderr)


def default_catalog_loader(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.catalog_file:
        return fetch_catalog()
    with open(args.catalog_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["data"] if isinstance(payload, dict) else payload


def run_apply(
    args: argparse.Namespace,
    env: Any,
    client: CoderClient,
    desired: Desired,
    live: Live,
    findings: list[Finding],
    confirm: Callable[[str], str],
    out: TextIO,
) -> int:
    todo = [
        f for f in findings if f.action and (f.category != ORPHAN_MODEL or args.disable_orphans)
    ]
    if not todo and not args.rotate_key:
        out.write("Nothing to apply.\n")
        return EXIT_OK
    if not args.yes and confirm("Apply? [y/N] ").strip().lower() not in ("y", "yes"):
        out.write("Aborted.\n")
        return EXIT_DRIFT
    apply_changes(
        client,
        live,
        todo,
        env.get("REQUESTY_API_KEY"),
        args.rotate_key,
        lambda m: out.write(m + "\n"),
    )
    remaining = [
        f
        for f in compute_diff(desired, load_live(client))
        if f.category not in (INFO, ORPHAN_MODEL)
    ]
    if remaining:
        out.write("Drift remains after apply:\n")
        out.write(format_report(remaining, summarize(remaining)) + "\n")
        return EXIT_DRIFT
    out.write("Applied.\n")
    return EXIT_OK


def run(
    args: argparse.Namespace,
    env: Any,
    client_factory: Callable[[str, str], Any],
    load_catalog: Callable[[argparse.Namespace], list[dict[str, Any]]],
    confirm: Callable[[str], str],
    out: TextIO,
) -> tuple[int, str]:
    token = env.get("CODER_SESSION_TOKEN")
    if not token:
        raise SyncError("CODER_SESSION_TOKEN is not set")
    desired = build_desired(load_catalog(args))
    client = client_factory(env.get("CODER_URL", DEFAULT_CODER_URL), token)
    live = load_live(client)
    findings = compute_diff(desired, live)
    summary = summarize(findings)
    if args.command == "check":
        emit(findings, summary, args.json, out)
        return (EXIT_DRIFT if has_drift(findings) else EXIT_OK), summary
    emit(findings, summary, False, out)
    return run_apply(args, env, client, desired, live, findings, confirm, out), summary


def main(
    argv: list[str] | None = None,
    env: Any = None,
    *,
    client_factory: Callable[[str, str], Any] = CoderClient,
    load_catalog: Callable[[argparse.Namespace], list[dict[str, Any]]] = default_catalog_loader,
    confirm: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = build_parser().parse_args(argv)
    try:
        code, summary = run(args, env, client_factory, load_catalog, confirm, out)
    except SyncError as err:
        print(f"error: {err}", file=sys.stderr)
        code, summary = EXIT_ERROR, f"error: {err}"
    except Exception as err:
        message = f"unexpected {type(err).__name__}: {err}"
        print(f"error: {message}", file=sys.stderr)
        code, summary = EXIT_ERROR, f"error: {message}"
    push_url = env.get("UPTIME_KUMA_PUSH_URL")
    if args.command == "check" and push_url:
        push_heartbeat(push_url, "up" if code == EXIT_OK else "down", summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
