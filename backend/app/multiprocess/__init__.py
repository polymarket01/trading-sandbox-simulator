"""Process-isolated runtime for the market-making sandbox.

The package is deliberately separate from the legacy in-process FastAPI
runtime so the migration can be exercised and rolled back without changing
the canonical 5174 process.  All children are created with ``spawn``.
"""

from app.multiprocess.protocol import IPC_SCHEMA_VERSION, IPCEnvelope

__all__ = ["IPC_SCHEMA_VERSION", "IPCEnvelope"]
