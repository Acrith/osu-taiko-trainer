"""Player configuration — most importantly, playstyle.

Playstyle determines how objective map features translate into subjective
player difficulty. See memory/user_playstyle.md for the reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlayerStyle(Enum):
    """Which physical fingering the player uses.

    KDDK: outer keys = kats, inner keys = dons.
          Hand alternation (L-R-L-R) is the default; color is decided per-note.
          -> mono streams are easy, bursts execute cheaply, technical rhythm hurts.

    DDKK / KKDD: color maps to a single hand (all Dons -> one hand, all Kats -> other).
          -> mono streams are the stamina test, but technical color patterns are natural.
    """
    KDDK = "kddk"
    DDKK = "ddkk"
    KKDD = "kkdd"

    @property
    def color_maps_to_hand(self) -> bool:
        return self in (PlayerStyle.DDKK, PlayerStyle.KKDD)


@dataclass(frozen=True)
class PlayerConfig:
    style: PlayerStyle = PlayerStyle.KDDK


DEFAULT_CONFIG = PlayerConfig()
