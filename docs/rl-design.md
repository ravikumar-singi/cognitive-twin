# Reinforcement learning design

## Problem framing

Teaching is a **partially observable** sequential decision problem. The teacher never sees a learner's knowledge; it sees answers and, optionally, coarse state features from a headband. The cognitive twin holds the belief state:

- **Knowledge belief**: Bayesian Knowledge Tracing per skill, with guess and slip rates that depend on item difficulty.
- **State belief**: exponentially smoothed attention and cognitive load, weighted by signal quality, plus a fatigue estimate from steps since the last break.

## Why several agents

One flat policy over (skill x difficulty x modality x pacing) has 7 x 3 x 3 x 2 = 126 joint actions with sparse, delayed rewards. Splitting it by *timescale* and *concern* makes each agent's problem small and its behaviour explainable to an educator:

| Agent | Timescale | Action space | Method | Why that method |
|---|---|---|---|---|
| Curriculum | 4-step options | skills on the frontier | LinUCB | Skill value depends on context (readiness, current belief); UCB explores without wasting many learners |
| Tutor | every step | 3 difficulties x 3 modalities | LinUCB | Effect of an item is mostly immediate; per-arm linear models learn learner-dependent preferences |
| Pacing | every step | continue / break | Q-learning | A break pays off only in later steps, so it needs bootstrapped value estimates |

The agents cooperate on one team reward, knowledge gain, scaled by 10. The hierarchy follows the options framework: the curriculum agent commits to a skill for a fixed option length (or until mastery) and is credited with the mean gain over that option.

## Rewards: oracle vs belief

- **Training in simulation** uses the simulator's true knowledge gain (`reward="oracle"`), a privileged signal that only exists in simulation.
- **Real learners** offer only the change in belief (`reward="belief"`), which is noisier and biased by the BKT parameters. This is the main sim-to-real gap, and the reason live sessions do not update the shared policy.

## Safety shield

The Guardian computes the allowed action set before each agent acts:

- Curriculum: only skills whose prerequisites look learned (belief of at least 0.7), and which aren't yet mastered.
- Tutor: no difficulty-3 items while belief is below 0.3.
- Pacing: a break is forced after 25 steps, or when sensing shows sustained low attention; no break in the first 3 steps after one.

Shielding keeps exploration safe and leaves an audit log of every time the agents were constrained.

## The simulator

Each simulated learner has hidden knowledge per skill, an aptitude multiplier, an affinity for each teaching modality and a fatigue rate. Learning per step is

```
gain = 0.20 x aptitude x modality_affinity x zone(difficulty, level)
       x readiness(prerequisites) x (1 - fatigue)^1.5 x (1 - knowledge)
```

where `zone` peaks half a level above the learner's current level (desirable difficulty), readiness collapses when prerequisites are weak, and wrong answers still teach at 60% of the rate (productive failure).

## Twin calibration

An over-confident belief is the worst failure mode here: the agents move on too early and the DAO credentials skills the learner doesn't have. With textbook BKT parameters (learn rate 0.12), only 30% of belief-mastered skills had true knowledge of 0.8 or more. Fitting guess, slip and learn rates to the simulator (learn rate 0.05, guess 0.50 / 0.30 / 0.25 by difficulty) raised that to 80%, and retraining the agents on the calibrated twin raised truly mastered skills per learner from 1.5 to 3.1. `cognitive-twin train` reports this as credential precision.

**Calibrating the simulator itself to real learners is the key open problem.** Before a pilot, the simulator's parameters should be fitted to historical interaction logs (for example, public datasets such as ASSISTments or EdNet) and the policy re-validated with off-policy evaluation.

## Reproducing

```bash
cognitive-twin train --episodes 800 --learners 200 --steps 80 --track ml-foundations --out src/cognitive_twin/content/policies/ml-foundations.json --json docs/benchmarks/ml-foundations.json
```

Evaluation uses common random numbers: every teacher meets the same held-out learners in the same order, so differences come from the policy rather than from luck.
