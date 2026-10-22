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
COMMAND_SIGNING_VECTORS = os.path.join(CONTRACTS, "nodes", "vectors", "command-signing.json")
#: contracts v1.14.2 join safety (nodes/README.md §10): the served script's guard, the version rules.
JOIN_VECTORS = os.path.join(CONTRACTS, "nodes", "vectors", "join.json")
IOT_NODE_SCHEMA = os.path.join(CONTRACTS, "schemas", "iot-node.schema.json")
#: contracts v1.16.0 (nodes/README.md §11, agent 0.4.2): /proc/net/wireless samples, signal words, iw / iwconfig, the gap watch;
#: v1.17.0 (§12, agent 0.4.3): ip route / ip neigh / ping / getent, the probes' findings, the decision table, the diagnose gaps.
NETWORK_VECTORS = os.path.join(CONTRACTS, "nodes", "vectors", "network.json")
