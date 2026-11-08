"""Train the multi-agent teacher on a simulated population and benchmark it."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from ..consent import ConsentProfile
from ..edge.sensor import EdgeProcessor, NeuralBand
from ..env.simulator import SimulatedLearner
from ..ledger.ledger import Ledger
from ..skills import Track, load_track
from ..twin import CognitiveTwin
from .teacher import (
    EpisodeResult,
    MultiAgentTeacher,
    RandomTeacher,
    SyllabusTeacher,
    Teacher,
    run_episode,
)

ADULT_WITH_SENSING = ConsentProfile(age=24, region="IN", neural_opt_in=True)
ADULT_NO_SENSING = ConsentProfile(age=24, region="IN", neural_opt_in=False)


def _setup(track: Track, rng: np.random.Generator, consent: ConsentProfile, i: int):
    learner = SimulatedLearner.sample(track, rng)
    twin = CognitiveTwin(f"sim-{i}", track)
    edge = EdgeProcessor(consent, NeuralBand(rng))
    return learner, twin, edge


def train(episodes: int = 800, steps: int = 80, seed: int = 7,
          consent: ConsentProfile = ADULT_WITH_SENSING,
          track: Track | None = None) -> tuple[MultiAgentTeacher, list[float]]:
    track = track or load_track()
    rng = np.random.default_rng(seed)
    teacher = MultiAgentTeacher(track, np.random.default_rng(seed + 1))
    curve: list[float] = []
    for i in range(episodes):
        learner, twin, edge = _setup(track, rng, consent, i)
        teacher.pacing.epsilon = max(0.02, 0.25 * (1 - i / episodes))
        res = run_episode(teacher, learner, twin, edge, steps, learn=True)
        curve.append(res.final_knowledge)
    return teacher, curve


@dataclass
class Benchmark:
    teacher: str
    mean_knowledge: float
    mean_mastered: float
    mean_breaks: float
    mean_accuracy: float


def evaluate(teachers: list[Teacher], learners: int = 200, steps: int = 80, seed: int = 99,
             consent: ConsentProfile = ADULT_WITH_SENSING,
             track: Track | None = None) -> list[Benchmark]:
    """Every teacher meets the *same* held-out learners (common random numbers)."""
    track = track or load_track()
    out = []
    for teacher in teachers:
        rng = np.random.default_rng(seed)
        results: list[EpisodeResult] = []
        for i in range(learners):
            learner, twin, edge = _setup(track, rng, consent, i)
            results.append(run_episode(teacher, learner, twin, edge, steps, learn=False))
        out.append(Benchmark(
            teacher.name,
            round(float(np.mean([r.final_knowledge for r in results])), 3),
            round(float(np.mean([r.mastered_skills for r in results])), 3),
            round(float(np.mean([r.breaks for r in results])), 2),
            round(float(np.mean([r.accuracy for r in results])), 3),
        ))
    return out


def credential_precision(teacher: MultiAgentTeacher, learners: int = 200, steps: int = 80,
                         seed: int = 42, track: Track | None = None) -> dict:
    """How often a DAO-issued credential reflects real (simulated) knowledge."""
    track = track or load_track()
    rng = np.random.default_rng(seed)
    ledger = Ledger.with_default_dao()
    true_k: list[float] = []
    for i in range(learners):
        learner, twin, edge = _setup(track, rng, ADULT_WITH_SENSING, i)
        run_episode(teacher, learner, twin, edge, steps, learn=False)
        for skill in twin.mastered():
            if ledger.request_credential(twin, skill).issued:
                true_k.append(learner.knowledge[skill])
    k = np.array(true_k) if true_k else np.zeros(1)
    return {"credentials": len(true_k), "per_learner": round(len(true_k) / learners, 2),
            "mean_true_knowledge": round(float(k.mean()), 3),
            "share_true_at_least_0.8": round(float((k >= 0.8).mean()), 3),
            "share_true_at_least_0.7": round(float((k >= 0.7).mean()), 3)}


def benchmark_all(episodes: int = 800, learners: int = 200, steps: int = 80,
                  seed: int = 7) -> dict:
    track = load_track()
    teacher, curve = train(episodes, steps, seed, track=track)
    rng = np.random.default_rng(seed + 2)
    untrained = MultiAgentTeacher(track, np.random.default_rng(seed + 3), name="untrained RL")
    results = evaluate([teacher, untrained, SyllabusTeacher(track, rng),
                        RandomTeacher(track, rng)], learners, steps, track=track)
    edges = [e for e in (0, 10, 25, 50, 100, 200, 400, 800, 1600, 3200) if e < episodes]
    edges.append(episodes)
    return {
        "teacher": teacher,
        "learning_curve": [{"episodes": f"{a}-{b}", "mean_knowledge":
                            round(float(np.mean(curve[a:b])), 3)}
                           for a, b in zip(edges, edges[1:], strict=False)],
        "benchmarks": [asdict(b) for b in results],
        "credential_precision": credential_precision(teacher, learners, steps, track=track),
    }
