"""Entry point for hosts that look for a Flask `app` at the project root
(Vercel). Running locally is unchanged: `python -m webapp.app`."""
from webapp.app import app

__all__ = ["app"]
