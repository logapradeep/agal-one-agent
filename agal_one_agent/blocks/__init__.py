"""Automation-block runtime (ADR-017, contracts v1.5.0).

The node executes a *compiled bundle* (Agal/contracts/schemas/automation-block
.schema.json ``$defs/CompiledBundle``): the Node Automation Block (NAB) plus the
Asset Automation Block (AAB) of every asset on the node, with ports already
resolved to transports by the cloud. The language is specified in
``Agal/contracts/programs/README.md``; :mod:`expr` is the Python port of the
reference parser and must pass the same golden vectors.

Modules
  expr      tokenizer / parser / validator / evaluator for expressions
  clock     wall + monotonic clock abstraction (system + simulated)
  sun       sunrise / sunset for schedules with a ``sunEvent``
  io        port adapters: SimulatedIO (bench on a laptop) and HardwareIO (GPIO)
  runtime   BlockRuntime — compile, evaluate, act, persist, acknowledge
  sync      program store + cloud fetch/ack helpers
  simulate  ``agal-one-agent-sim`` CLI: run a bundle against a scenario file
"""

from .expr import ExprError, parse, validate, evaluate  # noqa: F401
from .runtime import BlockRuntime, CompileError, EventSink  # noqa: F401
from .io import SimulatedIO, HardwareIO  # noqa: F401
from .clock import SystemClock, SimClock  # noqa: F401
