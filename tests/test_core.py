import numpy as np
import pytest

from cognitive_twin.consent import ConsentProfile
from cognitive_twin.edge.sensor import NEUTRAL, EdgeProcessor, NeuralBand
from cognitive_twin.env.simulator import SimulatedLearner
from cognitive_twin.rl.bandit import LinUCB
from cognitive_twin.rl.shield import MAX_STEPS_WITHOUT_BREAK, Guardian
from cognitive_twin.rl.teacher import MultiAgentTeacher, SyllabusTeacher
from cognitive_twin.rl.train import evaluate, train
from cognitive_twin.skills import load_track
from cognitive_twin.twin import CognitiveTwin


@pytest.fixture(scope="module")
def track():
    return load_track()


def test_skills_graph_is_acyclic_and_complete(track):
    order = track.topological_order()
    assert set(order) == set(track.skills)
    for s in order:
        for p in track.skills[s].prereqs:
            assert order.index(p) < order.index(s)
    for s in track.skills:
        assert {i.difficulty for i in track.items_for(s)} == {1, 2, 3}


def test_bkt_moves_belief_in_the_right_direction(track):
    twin = CognitiveTwin("t", track)
    p0 = twin.p_known["vectors"]
    assert twin.observe_answer("vec-3", "vectors", 2, "worked_example", True) > 0
    p1 = twin.p_known["vectors"]
    twin.observe_answer("vec-4", "vectors", 2, "worked_example", False)
    assert p1 > p0 and twin.p_known["vectors"] < p1


@pytest.mark.parametrize("consent,expected", [
    (ConsentProfile(24, "IN", neural_opt_in=True), True),
    (ConsentProfile(24, "IN", neural_opt_in=False), False),
    (ConsentProfile(15, "IN", neural_opt_in=True, guardian_consent=True), False),
    (ConsentProfile(24, "EU", neural_opt_in=True), False),
    (ConsentProfile(17, "US", neural_opt_in=True, guardian_consent=False), False),
])
def test_neural_sensing_policy(consent, expected):
    assert consent.neural_sensing_allowed is expected


def test_edge_returns_neutral_features_without_consent(track):
    rng = np.random.default_rng(0)
    learner = SimulatedLearner.sample(track, rng)
    edge = EdgeProcessor(ConsentProfile(14, "IN", True, True), NeuralBand(rng))
    assert edge.sense(learner, "vectors", 2) == NEUTRAL
    assert edge.raw_bytes_exported == 0


def test_edge_features_track_latent_attention(track):
    rng = np.random.default_rng(1)
    learner = SimulatedLearner.sample(track, rng)
    edge = EdgeProcessor(ConsentProfile(30, "IN", True), NeuralBand(rng, noise=0.01))
    fresh = edge.sense(learner, "vectors", 2).attention
    learner.fatigue = 0.95
    tired = edge.sense(learner, "vectors", 2).attention
    assert fresh > tired


def test_linucb_learns_the_better_arm():
    rng = np.random.default_rng(2)
    bandit = LinUCB(2, 1, alpha=0.3)
    ctx = np.ones((2, 1))
    for _ in range(300):
        arm = bandit.select(ctx)
        bandit.update(arm, ctx[arm], float(rng.normal(1.0 if arm == 1 else 0.2, 0.1)))
    assert bandit.select(ctx, explore=False) == 1
    assert bandit.pulls[1] > bandit.pulls[0]


def test_guardian_forces_break_and_blocks_hard_items_for_novices(track):
    twin = CognitiveTwin("g", track)
    g = Guardian()
    twin.steps_since_break = MAX_STEPS_WITHOUT_BREAK
    assert g.pacing_override(twin, "continue", sensing=False) == "break"
    twin.steps_since_break = 1
    assert g.pacing_override(twin, "break", sensing=False) == "continue"
    assert 3 not in g.allowed_difficulties(twin, "vectors")
    assert all(twin.prereq_min(s) >= 0.7 for s in g.allowed_skills(twin))
    assert len(g.log) == 2


def test_rl_teacher_beats_fixed_syllabus_on_held_out_learners(track):
    teacher, _ = train(episodes=150, steps=60, track=track)
    rng = np.random.default_rng(5)
    rl, syllabus = evaluate([teacher, SyllabusTeacher(track, rng)], learners=60, steps=60,
                            track=track)
    assert rl.mean_knowledge > syllabus.mean_knowledge


def test_policy_round_trips_through_json(track, tmp_path):
    teacher, _ = train(episodes=20, steps=30, track=track)
    path = tmp_path / "p.json"
    teacher.save(path)
    loaded = MultiAgentTeacher.load(path, track)
    assert np.allclose(loaded.tutor.model.A, teacher.tutor.model.A)
    assert loaded.pacing.q == teacher.pacing.q
