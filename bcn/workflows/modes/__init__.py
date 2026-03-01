"""Workflow modes for recurring and ad-hoc briefing publication."""

from bcn.workflows.modes.ad_hoc import MODE as AD_HOC_MODE
from bcn.workflows.modes.regular_daily_briefing import (
    MODE as REGULAR_DAILY_BRIEFING_MODE,
)
from bcn.workflows.modes.regular_monthly_newsletter import (
    MODE as REGULAR_MONTHLY_NEWSLETTER_MODE,
)

ALL_MODES: tuple[str, ...] = (
    REGULAR_DAILY_BRIEFING_MODE,
    AD_HOC_MODE,
    REGULAR_MONTHLY_NEWSLETTER_MODE,
)

