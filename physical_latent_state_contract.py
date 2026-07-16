"""Canonical physical latent-state vocabulary shared by runtime and training."""

from __future__ import annotations


LATENT_STATES = (
    "warming",
    "slowing_warming",
    "temporary_platform",
    "temporary_cooling",
    "cooling_then_rebound",
    "weather_suppressed",
    "terminal_cooling",
    "final_locked",
)

LATENT_STATE_SET = frozenset(LATENT_STATES)


__all__ = ["LATENT_STATES", "LATENT_STATE_SET"]
