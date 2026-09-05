"""사업자 어댑터 레지스트리."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..models import Provider
from .base import RailProvider
from .korail import KorailProvider
from .srt import SRTProvider

__all__ = ["RailProvider", "SRTProvider", "KorailProvider", "build_provider", "PROVIDERS"]

PROVIDERS = {Provider.SRT: SRTProvider, Provider.KORAIL: KorailProvider}


def build_provider(
    provider: Provider | str,
    *,
    interval: float = 3.0,
    data_dir: Path | None = None,
    **kwargs: Any,
) -> RailProvider:
    p = Provider(provider)
    if p is Provider.KORAIL:
        return KorailProvider(interval=interval, data_dir=data_dir, **kwargs)
    return SRTProvider(interval=interval, **kwargs)
