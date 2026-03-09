"""Narrow configuration views for deployable BCN components."""

from __future__ import annotations

from dataclasses import dataclass

from bcn.common.config import Settings

_COMPONENT_SERVICE_URL_FIELDS = {
    "writer": "writer_service_url",
    "critic": "critic_service_url",
    "verifier": "verifier_service_url",
    "collector": "collector_service_url",
    "analyst": "analyst_service_url",
}

_COMPONENT_DEFAULT_PORTS = {
    "writer": 8081,
    "critic": 8082,
    "verifier": 8083,
    "collector": 8084,
    "analyst": 8085,
}


@dataclass(frozen=True)
class ServiceClientSettings:
    """Remote endpoint settings for one BCN component."""

    component: str
    base_url: str
    timeout_seconds: int
    auth_token: str

    @property
    def configured(self) -> bool:
        """Return whether the component is configured for remote calls."""
        return bool(self.base_url)


def service_client_settings(settings: Settings, component: str) -> ServiceClientSettings:
    """Return a narrow remote-client config view for one component."""
    normalized = str(component or "").strip().lower()
    field_name = _COMPONENT_SERVICE_URL_FIELDS.get(normalized)
    if not field_name:
        raise ValueError(f"Unsupported component: {component}")
    return ServiceClientSettings(
        component=normalized,
        base_url=str(getattr(settings, field_name, "") or "").strip(),
        timeout_seconds=max(1, int(settings.service_request_timeout_seconds)),
        auth_token=str(settings.service_auth_token or "").strip(),
    )


def default_service_port(component: str) -> int:
    """Return the default bind port for one deployable component."""
    normalized = str(component or "").strip().lower()
    if normalized not in _COMPONENT_DEFAULT_PORTS:
        raise ValueError(f"Unsupported component: {component}")
    return _COMPONENT_DEFAULT_PORTS[normalized]


__all__ = [
    "ServiceClientSettings",
    "default_service_port",
    "service_client_settings",
]
