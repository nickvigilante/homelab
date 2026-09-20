"""Test doubles: catalog entries, an in-memory Coder, and a stub HTTP server."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_entry(
    model_id,
    lab="acme",
    canonical=None,
    inp=1e-6,
    out=2e-6,
    cached=None,
    ctx=100_000,
    maxout=8_000,
    tools=True,
    retires=None,
):
    entry = {
        "id": model_id,
        "api": "chat",
        "model_lab": lab,
        # Region (@) and service-tier (:) suffixes share the plain canonical name.
        "model_canonical_name": canonical or re.split(r"[@:]", model_id.rsplit("/", 1)[-1])[0],
        "input_price": inp,
        "output_price": out,
        "cached_price": cached,
        "context_window": ctx,
        "max_output_tokens": maxout,
        "supports_tool_calling": tools,
    }
    if retires is not None:
        entry["retires"] = retires
    return entry


def small_catalog():
    """A paid Anthropic model, a paid OpenAI model, and a free NVIDIA model."""
    return [
        make_entry(
            "anthropic/claude-a",
            "anthropic",
            inp=3e-6,
            out=15e-6,
            cached=3e-7,
            ctx=200_000,
            maxout=64_000,
        ),
        make_entry("openai/gpt-x", "openai", inp=1e-6, out=4e-6, ctx=400_000, maxout=128_000),
        make_entry("nvidia/free-y", "nvidia", inp=0, out=0, ctx=131_072, maxout=0),
    ]


class FakeCoder:
    """Same interface as CoderClient, kept in memory. It has no delete methods."""

    def __init__(self):
        self.providers = {}
        self.models = {}
        self.prices = {}
        self.calls = []
        self.reads = []  # names of the read methods called, to prove which endpoints a mode used
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    def default_org_id(self):
        return "org-1"

    def list_providers(self):
        self.reads.append("list_providers")
        return [dict(p) for p in self.providers.values()]

    def create_provider(self, payload):
        provider_id = self._id("prov")
        keys = [{"id": self._id("key"), "masked": "****"} for _ in payload.get("api_keys", [])]
        provider = {
            **{k: v for k, v in payload.items() if k != "api_keys"},
            "id": provider_id,
            "api_keys": keys,
        }
        self.providers[provider_id] = provider
        self.calls.append(("create_provider", payload["name"]))
        return dict(provider)

    def update_provider(self, provider_id, payload):
        provider = self.providers[provider_id]
        for key, value in payload.items():
            if key == "api_keys":
                provider["api_keys"] = [{"id": self._id("key"), "masked": "****"} for _ in value]
            else:
                provider[key] = value
        self.calls.append(("update_provider", provider_id))

    def list_models(self, org_id):
        self.reads.append("list_models")
        return [dict(m) for m in self.models.values()]

    def list_models_response(self, org_id):
        """What a narrow token sees: models plus provider descriptors, with no name or base_url."""
        self.reads.append("list_models_response")
        descriptors = [
            {
                "id": p["id"],
                "type": p["type"],
                "display_name": p["display_name"],
                "icon": p["icon"],
                "enabled": p["enabled"],
                "has_api_key": bool(p["api_keys"]),
                "has_effective_api_key": bool(p["api_keys"]),
                "has_user_api_key": False,
                "allow_user_api_key": False,
                "available": True,
            }
            for p in self.providers.values()
        ]
        return {
            "models": [dict(m) for m in self.models.values()],
            "providers": descriptors,
            "unsupported_providers": [],
        }

    def create_model(self, org_id, payload):
        model_id = self._id("model")
        model = {**payload, "id": model_id, "organization_id": org_id, "is_default": False}
        self.models[model_id] = model
        self.calls.append(("create_model", payload["model"]))
        return dict(model)

    def update_model(self, org_id, model_id, payload):
        self.models[model_id].update(payload)
        self.calls.append(("update_model", model_id))

    def list_custom_prices(self):
        self.reads.append("list_custom_prices")
        return [dict(p) for p in self.prices.values()]

    def upsert_prices(self, prices):
        for price in prices:
            self.prices[(price["provider"], price["model"])] = {**price, "source": "custom"}
        self.calls.append(("upsert_prices", len(prices)))


def seed_in_sync(fake, desired):
    """Populate a FakeCoder so that it exactly matches `desired`."""
    ids = {}
    for name, provider in desired.providers.items():
        created = fake.create_provider(
            {
                "type": provider.type,
                "name": name,
                "display_name": provider.display_name,
                "icon": provider.icon,
                "enabled": True,
                "base_url": provider.base_url,
                "api_keys": ["secret-key"],
            }
        )
        ids[name] = created["id"]
    for model in desired.models.values():
        payload = {
            "ai_provider_id": ids[model.provider],
            "model": model.model,
            "display_name": model.display_name,
            "enabled": True,
            "context_limit": model.context_limit,
        }
        if model.max_output_tokens:
            payload["model_config"] = {"max_output_tokens": model.max_output_tokens}
        fake.create_model("org-1", payload)
        first, second, third, fourth = model.prices
        fake.upsert_prices(
            [
                {
                    "provider": model.provider_type,
                    "model": model.model,
                    "input_price": first,
                    "output_price": second,
                    "cache_read_price": third,
                    "cache_write_price": fourth,
                }
            ]
        )
    fake.calls.clear()
    return ids


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.split("?")[0]
        self.server.requests.append(
            {
                "method": self.command,
                "path": path,
                "query": self.path,
                "headers": headers,
                "body": body,
            }
        )
        status, payload = self.server.responses.get(
            (self.command, path), (404, {"message": "not found"})
        )
        raw = (
            payload
            if isinstance(payload, bytes)
            else (b"" if payload is None else json.dumps(payload).encode())
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PATCH = _handle

    def log_message(self, *args):
        pass


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.responses = {}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
