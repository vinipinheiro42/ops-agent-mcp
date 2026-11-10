"""Model providers. `offline` is the default and needs no key; real providers are opt-in via env vars."""

from __future__ import annotations

import os

from ops_agent.agent.providers.base import ModelClient, ProviderConfigError
from ops_agent.agent.providers.offline import OfflineHeuristicModel


def make_model(provider: str | None = None) -> ModelClient:
    name = (provider or os.environ.get("OPS_AGENT_PROVIDER") or "offline").lower()
    if name == "offline":
        return OfflineHeuristicModel()
    model = os.environ.get("OPS_AGENT_MODEL")
    if not model:
        raise ProviderConfigError("set OPS_AGENT_MODEL to the model id to use with a real provider")
    if name == "anthropic":
        from ops_agent.agent.providers.anthropic import AnthropicModel

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderConfigError("ANTHROPIC_API_KEY is not set")
        return AnthropicModel(api_key=key, model=model)
    if name == "openai":
        from ops_agent.agent.providers.openai import OpenAIModel

        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderConfigError("OPENAI_API_KEY is not set")
        return OpenAIModel(api_key=key, model=model, base_url=os.environ.get("OPENAI_BASE_URL"))
    raise ProviderConfigError(f"unknown provider {name!r} (offline | anthropic | openai)")


__all__ = ["ModelClient", "ProviderConfigError", "make_model"]
