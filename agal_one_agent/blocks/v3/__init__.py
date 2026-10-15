"""Bundle v3 — layout rules, the instruction, the built-ins (contracts v1.15.0, ADR-029; programs/README.md §7).

Modules
  program   check a CompiledBundleV3 and build the node's program: cards, outputs, built-ins, blocks
  runtime   RuntimeV3 — the pass, the bumpless swap, the instruction switch, holds, pause-first, commands
  guard     the motor guard (dry run, overload, phase, voltage, inrush, restart lockout)
  valve     the valve drivers (continuous; single-pulse latching with its FAULT)
  safety    the E-stop latch and power sense

agal_one_agent.blocks.host.ProgramHost runs this beside the language-1 BlockRuntime: the node runs the
bundle the cloud serves.
"""

from .runtime import RuntimeV3  # noqa: F401
from .program import LANGUAGE, RESUME_AFTER_POWER_SECONDS  # noqa: F401
