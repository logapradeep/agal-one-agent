"""Where the shared contracts are, for the tests that read their default programs and vectors.

The agent lives at ``Agal/daemon/agal-one-agent``, so the contracts are three levels up. A
checkout of the agent on its own (a git worktree, a CI clone) says where they are with
``AGAL_CONTRACTS_DIR`` — e.g. ``AGAL_CONTRACTS_DIR=~/Dev/Agal/contracts python -m pytest tests/``.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = os.path.normpath(os.path.expanduser(
    os.environ.get("AGAL_CONTRACTS_DIR") or os.path.join(_HERE, "..", "..", "..", "contracts")))
DEFAULTS = os.path.join(CONTRACTS, "programs", "defaults")
EXPRESSION_VECTORS = os.path.join(CONTRACTS, "programs", "test-vectors", "expressions.json")
