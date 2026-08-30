"""LazyAF runner agent - Phase 12.6.

A process that runs on a machine the backend does not own, connects to it over
a WebSocket, registers with labels, heartbeats, receives step assignments, and
executes them through a PLUGGABLE orchestrator.

Import rule (wave-5 file ownership, section 8): this package imports NOTHING
from ``backend/app``. A runner host must not need the backend on its
PYTHONPATH. The two places where that costs a deliberate code copy - the
control-file tar builder and the wire constants - are each pinned by a
contract test that reads the backend source (``tests/test_control_archive_parity.py``).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
