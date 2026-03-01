"""Briefing generation sub-components.

This package intentionally exports symbols lazily to avoid import cycles when
submodules (for example ``bcn.briefing.text``) are imported independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

__all__ = ["BriefingSelector", "BriefingQualityGate", "BriefingFactVerifier"]

if TYPE_CHECKING:
    from bcn.briefing.quality import BriefingQualityGate
    from bcn.briefing.selection import BriefingSelector
    from bcn.briefing.verifier import BriefingFactVerifier


def __getattr__(name: str) -> Any:
    if name == "BriefingSelector":
        from bcn.briefing.selection import BriefingSelector

        globals()[name] = BriefingSelector
        return BriefingSelector
    if name == "BriefingQualityGate":
        from bcn.briefing.quality import BriefingQualityGate

        globals()[name] = BriefingQualityGate
        return BriefingQualityGate
    if name == "BriefingFactVerifier":
        from bcn.briefing.verifier import BriefingFactVerifier

        globals()[name] = BriefingFactVerifier
        return BriefingFactVerifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
