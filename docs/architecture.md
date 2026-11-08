# Architecture

Six layers around one learner model. Privacy narrows at each boundary: raw signals stay on the device, features feed the twin, and only answer evidence reaches the ledger.

| Layer | MVP module | Production direction |
|---|---|---|
| Integration | `api.py` (REST) | LTI 1.3 and OneRoster for LMS/SIS, HRIS connectors, SSO |
| Trust ledger | `ledger/ledger.py` | Layer-2 chain, W3C VC 2.0, zero-knowledge proofs, staked validator network |
| Agent mesh | `rl/` | Same agents, plus an LLM content layer for Socratic dialogue and item generation |
| Cognitive twin | `twin.py` | Skills knowledge graph, richer knowledge tracing (deep KT), consent vault, federated learning |
| Spatial runtime | — | AR scene engine, campus edge rendering, sub-20 ms motion-to-photon |
| Edge sensing | `edge/sensor.py` | Dry-electrode EEG + fNIRS headband, eye tracking, on-device processing |

## Request flow: one teaching step

1. The edge processor turns a window of raw frames into `StateFeatures` and drops the window.
2. The twin smooths the features into its state belief.
3. The pacing agent proposes continue or break; the Guardian may override.
4. The curriculum agent keeps or replaces its current skill from the Guardian's allowed set.
5. The tutor agent picks difficulty and modality from the allowed arms; an item is drawn from the bank.
6. The learner answers; BKT updates the knowledge belief.
7. In training, each agent updates from the reward. In live sessions, the log is kept for offline retraining.

## Credential flow

1. A learner (or educator) requests a credential for a mastered skill.
2. `Evidence.from_twin` builds a package from recent answers only, under a pseudonymous DID, with a salted commitment.
3. Every validator reviews it independently against the rubric: belief of at least 0.95, at least three correct answers at difficulty 2 or above, and recent accuracy of at least 67%.
4. If approving validators hold at least two thirds of total stake, the credential is minted, signed by each approver (Ed25519) and anchored in a new block. Validators who voted against the outcome lose 10% of their stake.
5. Anyone can verify: signatures, the stake recorded at issuance, the ledger anchor and the integrity of the chain.
