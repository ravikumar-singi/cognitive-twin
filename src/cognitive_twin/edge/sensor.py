"""Ambient neural interface: on-device sensing and feature extraction.

The ``NeuralBand`` stands in for a dry-electrode EEG + fNIRS headband. It
produces *raw* band-power frames. ``EdgeProcessor`` runs on the device, turns
a window of frames into three state features, and then discards the raw
window. Only :class:`StateFeatures` crosses the device boundary, so the rest
of the system cannot read raw neural data even by accident.

Non-invasive sensing reads coarse states such as attention and mental load.
It does not read thoughts or content, and nothing here pretends to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..consent import ConsentProfile


@dataclass(frozen=True)
class StateFeatures:
    attention: float       # 0..1
    cognitive_load: float  # 0..1
    signal_quality: float  # 0..1

    def as_dict(self) -> dict[str, float]:
        return {
            "attention": round(self.attention, 3),
            "cognitive_load": round(self.cognitive_load, 3),
            "signal_quality": round(self.signal_quality, 3),
        }


NEUTRAL = StateFeatures(attention=0.5, cognitive_load=0.5, signal_quality=0.0)


class LatentSource(Protocol):
    def latent_attention(self, skill: str, difficulty: int) -> float: ...
    def latent_load(self, skill: str, difficulty: int) -> float: ...


class NeuralBand:
    """Simulated headband: emits noisy raw frames of [alpha, beta, theta, hbo]."""

    def __init__(self, rng: np.random.Generator, noise: float = 0.08):
        self.rng = rng
        self.noise = noise

    def read_window(self, source: LatentSource, skill: str, difficulty: int, n: int = 16):
        att = source.latent_attention(skill, difficulty)
        load = source.latent_load(skill, difficulty)
        frames = np.empty((n, 4))
        frames[:, 0] = (1 - att) + self.rng.normal(0, self.noise, n)          # alpha
        frames[:, 1] = att + self.rng.normal(0, self.noise, n)                # beta
        frames[:, 2] = load + self.rng.normal(0, self.noise, n)               # theta
        frames[:, 3] = 0.5 * load + 0.25 + self.rng.normal(0, self.noise, n)  # fNIRS HbO
        return frames


class EdgeProcessor:
    """Runs on the device. Returns features only; the raw window is dropped."""

    def __init__(self, consent: ConsentProfile, band: NeuralBand | None):
        self.consent = consent
        self.band = band
        self.raw_bytes_exported = 0  # audited in tests: must stay zero

    @property
    def active(self) -> bool:
        return self.band is not None and self.consent.neural_sensing_allowed

    def sense(self, source: LatentSource, skill: str, difficulty: int) -> StateFeatures:
        if not self.active:
            return NEUTRAL
        raw = self.band.read_window(source, skill, difficulty)
        beta, alpha = raw[:, 1].mean(), raw[:, 0].mean()
        attention = float(np.clip(beta / (beta + alpha + 1e-6), 0, 1))
        load = float(np.clip(0.6 * raw[:, 2].mean() + 0.8 * (raw[:, 3].mean() - 0.25), 0, 1))
        quality = float(np.clip(1.0 - raw.std(axis=0).mean() * 2, 0, 1))
        del raw  # never leaves this function
        return StateFeatures(attention, load, quality)
