"""Load .env. Kept apart from llm.py so the dashboard can read configuration
without importing (or installing) the Anthropic SDK."""

from __future__ import annotations

import pathlib

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env() -> None:
    load_dotenv(ROOT / ".env")
