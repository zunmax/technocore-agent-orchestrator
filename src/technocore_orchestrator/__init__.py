"""Technocore Agent Orchestrator."""

import sys

if sys.platform != "win32":
    raise RuntimeError("Technocore Agent Orchestrator supports native Windows only")

__version__ = "0.1.0"
