"""Global safety guard: the test suite must never instantiate a network LLM."""
from __future__ import annotations

import os


os.environ["LLM_PROVIDER"] = "mock"
os.environ["LLM_CACHE"] = "0"
os.environ["LLM_FALLBACK"] = ""
os.environ.pop("VALIDATOR_MOCK", None)
