"""Component-scoped config surfaces for BCN deployable services."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_settings import BaseSettings

from bcn.common.config import Settings
from bcn.common.settings_sections import BCNSettingsBase
from bcn.common.settings_sections import BriefingLengthSettingsMixin
from bcn.common.settings_sections import CollectionSourceSettingsMixin
from bcn.common.settings_sections import ComfyUISettingsMixin
from bcn.common.settings_sections import CriticPolicySettingsMixin
from bcn.common.settings_sections import DatabaseSettingsMixin
from bcn.common.settings_sections import DeliveryChannelSettingsMixin
from bcn.common.settings_sections import DistributionPolicySettingsMixin
from bcn.common.settings_sections import ReviewServiceEndpointSettingsMixin
from bcn.common.settings_sections import ScrapingSettingsMixin
from bcn.common.settings_sections import ServiceTransportSettingsMixin
from bcn.common.settings_sections import SharedLLMSettingsMixin
from bcn.common.settings_sections import VerifierPolicySettingsMixin
from bcn.common.settings_sections import WriterPolicySettingsMixin

_COMPONENT_SERVICE_URL_FIELDS = {
    "writer": "writer_service_url",
    "critic": "critic_service_url",
    "verifier": "verifier_service_url",
    "collector": "collector_service_url",
    "analyst": "analyst_service_url",
    "distributor": "distributor_service_url",
}

_COMPONENT_DEFAULT_PORTS = {
    "writer": 8081,
    "critic": 8082,
    "verifier": 8083,
    "collector": 8084,
    "analyst": 8085,
    "distributor": 8086,
}

class _BCNComponentSettingsBase(BCNSettingsBase, ServiceTransportSettingsMixin):
    """Shared env-loading behavior for component-local settings."""


class _RoleAwareLLMComponentSettings(_BCNComponentSettingsBase, SharedLLMSettingsMixin):
    """Shared role-aware LLM settings for LLM-backed services."""


class WriterServiceSettings(
    _RoleAwareLLMComponentSettings,
    DatabaseSettingsMixin,
    ReviewServiceEndpointSettingsMixin,
    ComfyUISettingsMixin,
    WriterPolicySettingsMixin,
):
    """Writer-service runtime settings for standalone deployment."""


class CriticServiceSettings(
    _RoleAwareLLMComponentSettings,
    BriefingLengthSettingsMixin,
    CriticPolicySettingsMixin,
):
    """Critic-service runtime settings for standalone deployment."""


class VerifierServiceSettings(_RoleAwareLLMComponentSettings, VerifierPolicySettingsMixin):
    """Verifier-service runtime settings for standalone deployment."""


class AnalystServiceSettings(_RoleAwareLLMComponentSettings, ScrapingSettingsMixin):
    """Analyst-service runtime settings for standalone deployment."""


class CollectorServiceSettings(
    _BCNComponentSettingsBase,
    CollectionSourceSettingsMixin,
    ScrapingSettingsMixin,
):
    """Collector-service runtime settings for standalone deployment."""


class DistributorServiceSettings(
    _BCNComponentSettingsBase,
    ComfyUISettingsMixin,
    DeliveryChannelSettingsMixin,
    DistributionPolicySettingsMixin,
):
    """Distributor-service runtime settings for standalone deployment."""


_COMPONENT_SETTINGS_CLASSES = {
    "writer": WriterServiceSettings,
    "critic": CriticServiceSettings,
    "verifier": VerifierServiceSettings,
    "collector": CollectorServiceSettings,
    "analyst": AnalystServiceSettings,
    "distributor": DistributorServiceSettings,
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


def load_component_service_settings(component: str) -> BaseSettings:
    """Load only the env/config surface needed by one deployable component."""
    normalized = str(component or "").strip().lower()
    settings_class = _COMPONENT_SETTINGS_CLASSES.get(normalized)
    if settings_class is None:
        raise ValueError(f"Unsupported component: {component}")
    return settings_class()


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
    "AnalystServiceSettings",
    "CollectorServiceSettings",
    "CriticServiceSettings",
    "DistributorServiceSettings",
    "ServiceClientSettings",
    "VerifierServiceSettings",
    "WriterServiceSettings",
    "default_service_port",
    "load_component_service_settings",
    "service_client_settings",
]
