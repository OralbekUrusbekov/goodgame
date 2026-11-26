"""Global safety guard: the test suite must never instantiate a network LLM."""
from __future__ import annotations

import os
import tempfile


os.environ["LLM_PROVIDER"] = "mock"
os.environ["LLM_CACHE"] = "0"
os.environ["LLM_FALLBACK"] = ""
os.environ.setdefault("WEB_DATA_DIR", tempfile.mkdtemp(prefix="bcc_web_test_data_"))
os.environ.pop("VALIDATOR_MOCK", None)
