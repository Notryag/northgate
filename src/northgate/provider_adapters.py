from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from northgate.routing import ResolvedRoute


class AdapterRequestError(Exception):
    pass


class AdapterUnavailableError(Exception):
    pass


class ProviderAdapter(Protocol):
    def validate(self, route: ResolvedRoute, model: str | None) -> None: ...

    def build_request(
        self,
        client: httpx.AsyncClient,
        route: ResolvedRoute,
        *,
        forwarded_headers: dict[str, str],
        body: bytes,
        model: str | None,
    ) -> httpx.Request: ...


@dataclass(frozen=True)
class OpenAICompatibleAdapter:
    def validate(self, route: ResolvedRoute, model: str | None) -> None:
        return None

    def build_request(
        self,
        client: httpx.AsyncClient,
        route: ResolvedRoute,
        *,
        forwarded_headers: dict[str, str],
        body: bytes,
        model: str | None,
    ) -> httpx.Request:
        headers = dict(forwarded_headers)
        headers["authorization"] = f"Bearer {route.api_key}"
        return client.build_request(
            "POST",
            f"{route.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            content=body,
        )


@dataclass(frozen=True)
class AzureOpenAIAdapter:
    def validate(self, route: ResolvedRoute, model: str | None) -> None:
        if not model:
            raise AdapterRequestError("Azure OpenAI routes require a model deployment name")
        api_version = dict(route.adapter_config).get("api_version")
        if not api_version:
            raise AdapterUnavailableError("Azure OpenAI adapter requires api_version")

    def build_request(
        self,
        client: httpx.AsyncClient,
        route: ResolvedRoute,
        *,
        forwarded_headers: dict[str, str],
        body: bytes,
        model: str | None,
    ) -> httpx.Request:
        self.validate(route, model)
        headers = dict(forwarded_headers)
        headers["api-key"] = route.api_key
        api_version = dict(route.adapter_config)["api_version"]
        deployment = quote(model or "", safe="")
        return client.build_request(
            "POST",
            f"{route.base_url.rstrip('/')}/openai/deployments/{deployment}/chat/completions",
            params={"api-version": api_version},
            headers=headers,
            content=body,
        )


_ADAPTERS: dict[str, ProviderAdapter] = {
    "openai_compatible": OpenAICompatibleAdapter(),
    "azure_openai": AzureOpenAIAdapter(),
}


def provider_adapter(name: str) -> ProviderAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise AdapterUnavailableError(f"Unsupported provider adapter: {name}") from exc
