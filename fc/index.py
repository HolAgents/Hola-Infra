"""FC Python 3.10 runtime ASGI entry point.

The FC Python runtime passes HTTP requests to this handler as ASGI.
FastAPI's ``app`` is already an ASGI application, so we just forward.
"""

from main import app


# FC Python 3.10 runtime looks for a callable named ``handler``.
# It wraps HTTP trigger requests as ASGI (scope, receive, send).
handler = app
