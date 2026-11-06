"""Shared test import setup for the FastAPI application."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE_PATH = ROOT / "llm-project" / "serve"
if str(SERVE_PATH) not in sys.path:
    sys.path.insert(0, str(SERVE_PATH))
