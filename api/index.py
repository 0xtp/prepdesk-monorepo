"""Vercel entrypoint: the whole FastAPI app runs as one Python function."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from perpdesk.web.app import create_app

app = create_app()
