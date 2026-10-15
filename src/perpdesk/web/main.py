"""ASGI entry for local runs: `uvicorn perpdesk.web.main:app`."""

from perpdesk.web.app import create_app

app = create_app()
