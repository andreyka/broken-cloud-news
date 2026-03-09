"""Application settings loaded from environment variables."""

from __future__ import annotations

from bcn.common.settings_sections import BCNSettingsBase
from bcn.common.settings_sections import CollectionSourceSettingsMixin
from bcn.common.settings_sections import ComfyUISettingsMixin
from bcn.common.settings_sections import DatabaseSettingsMixin
from bcn.common.settings_sections import DeliveryChannelSettingsMixin
from bcn.common.settings_sections import DistributionPolicySettingsMixin
from bcn.common.settings_sections import RemoteComponentEndpointSettingsMixin
from bcn.common.settings_sections import SchedulingSettingsMixin
from bcn.common.settings_sections import ScrapingSettingsMixin
from bcn.common.settings_sections import ServiceTransportSettingsMixin
from bcn.common.settings_sections import SharedLLMSettingsMixin
from bcn.common.settings_sections import WriterPolicySettingsMixin


class Settings(
    BCNSettingsBase,
    DatabaseSettingsMixin,
    SharedLLMSettingsMixin,
    RemoteComponentEndpointSettingsMixin,
    ServiceTransportSettingsMixin,
    ComfyUISettingsMixin,
    CollectionSourceSettingsMixin,
    SchedulingSettingsMixin,
    ScrapingSettingsMixin,
    WriterPolicySettingsMixin,
    DeliveryChannelSettingsMixin,
    DistributionPolicySettingsMixin,
):
    """BCN control-plane settings backed by ``BCN_`` environment variables."""


__all__ = ["Settings"]
