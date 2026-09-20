"""
Shared Anthropic client + model config.

Lives in its own module so that app.py and visualization.py can both import
it without either importing the other. This is what fixes the double-import
bug: visualization.py used to do `from app import client, MODEL`, but under
`python app.py` the running module is `__main__`, so `app` was imported a
SECOND time as a separate module — building a second Flask app, a second
Anthropic client, and registering the blueprint twice. (The giveaway was the
"ANTHROPIC_API_KEY is not set" warning printing twice on startup.)
"""

import logging
import os
from pathlib import Path

import anthropic

# Load .env from THIS file's directory, not the current working directory.
# load_dotenv() with no argument searches upward from CWD, so launching the
# app from anywhere other than the project root silently found no .env.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("founders_file")

API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Dateless IDs are canonical pinned versions for the 4.6 generation and later
# (they are NOT evergreen aliases). "claude-sonnet-4-6" is valid; bump this to
# a newer model when you want to.
MODEL = "claude-sonnet-4-6"

client = anthropic.Anthropic(api_key=API_KEY)

if not API_KEY:
    logger.warning(
        "ANTHROPIC_API_KEY is not set (checked environment and %s). "
        "Every Anthropic API call will fail and Sentiment Score will fall back "
        "to neutral defaults (score 50) instead of a real analysis.",
        Path(__file__).resolve().parent / ".env",
    )
else:
    logger.info("ANTHROPIC_API_KEY loaded (…%s), model=%s", API_KEY[-4:], MODEL)
