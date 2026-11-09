"""LinUCB contextual bandit with a shared or per-arm linear model."""

from __future__ import annotations

import numpy as np


class LinUCB:
    """Disjoint LinUCB (Li et al., 2010): one ridge-regression model per arm."""

    def __init__(self, n_arms: int, dim: int, alpha: float = 0.6, ridge: float = 1.0):
        self.n_arms, self.dim, self.alpha = n_arms, dim, alpha
        self.A = np.stack([np.eye(dim) * ridge for _ in range(n_arms)])
        self.b = np.zeros((n_arms, dim))
        self.pulls = np.zeros(n_arms, dtype=int)
        self._cache: tuple[np.ndarray, np.ndarray] | None = None

    def _params(self) -> tuple[np.ndarray, np.ndarray]:
        """Inverses and coefficients, cached until the next update. Serving a fixed
        policy then costs a few dot products per decision, not matrix inversions."""
        if self._cache is None:
            inv = np.linalg.inv(self.A)
            theta = np.einsum("aij,aj->ai", inv, self.b)
            self._cache = (inv, theta)
        return self._cache

    def scores(self, contexts: np.ndarray, explore: bool = True) -> np.ndarray:
        """``contexts`` has shape (n_arms, dim): one context vector per arm."""
        inv, theta = self._params()
        out = np.einsum("ai,ai->a", theta, contexts)
        if explore:
            out = out + self.alpha * np.sqrt(np.einsum("ai,aij,aj->a", contexts, inv, contexts))
        return out

    def select(self, contexts: np.ndarray, allowed: list[int] | None = None,
               explore: bool = True) -> int:
        s = self.scores(contexts, explore)
        if allowed is not None:
            mask = np.full(self.n_arms, -np.inf)
            mask[allowed] = 0.0
            s = s + mask
        return int(np.argmax(s))

    def update(self, arm: int, context: np.ndarray, reward: float) -> None:
        self._cache = None
        self.A[arm] += np.outer(context, context)
        self.b[arm] += reward * context
        self.pulls[arm] += 1

    def to_dict(self) -> dict:
        return {"n_arms": self.n_arms, "dim": self.dim, "alpha": self.alpha,
                "A": self.A.tolist(), "b": self.b.tolist(), "pulls": self.pulls.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> LinUCB:
        m = cls(d["n_arms"], d["dim"], d["alpha"])
        m.A, m.b = np.array(d["A"]), np.array(d["b"])
        m.pulls = np.array(d["pulls"], dtype=int)
        return m
