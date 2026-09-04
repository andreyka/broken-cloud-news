"""Shared workflow mode identifiers used across control-plane and services."""

AD_HOC_MODE = "ad_hoc"
REGULAR_DAILY_BRIEFING_MODE = "regular_daily_briefing"
REGULAR_MONTHLY_NEWSLETTER_MODE = "regular_monthly_newsletter"
WEEKLY_FLAGSHIP_MODE = "weekly_flagship"

ALL_WORKFLOW_MODES: tuple[str, ...] = (
    REGULAR_DAILY_BRIEFING_MODE,
    AD_HOC_MODE,
    REGULAR_MONTHLY_NEWSLETTER_MODE,
    WEEKLY_FLAGSHIP_MODE,
)


__all__ = [
    "AD_HOC_MODE",
    "ALL_WORKFLOW_MODES",
    "REGULAR_DAILY_BRIEFING_MODE",
    "REGULAR_MONTHLY_NEWSLETTER_MODE",
    "WEEKLY_FLAGSHIP_MODE",
]
