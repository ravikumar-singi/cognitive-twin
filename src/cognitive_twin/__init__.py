"""Cognitive Twin: a cognition-native learning platform (MVP).

Three pillars on one learner model:
  1. Agentic AI tutors that co-create the curriculum (``cognitive_twin.agents``)
  2. An ambient neural interface whose raw signals never leave the device
     (``cognitive_twin.edge``)
  3. A Proof-of-Capability ledger validated by a validator DAO
     (``cognitive_twin.ledger``)
"""

import os as _os

# One BLAS thread per request: the service already runs requests in parallel, and
# nested BLAS threads oversubscribe the CPU badly under load.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    _os.environ.setdefault(_var, "1")

__version__ = "0.2.0"
