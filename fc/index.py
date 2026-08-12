"""FC Python 3.10 runtime entry point.

The official FC Python runtime invokes HTTP triggers as WSGI
(``environ, start_response``), while FastAPI's ``app`` is ASGI
(``__call__(scope, receive, send)``). ``a2wsgi`` bridges the two:
it adapts the ASGI app into the WSGI callable the runtime expects.
"""

from a2wsgi import ASGIMiddleware

from main import app


# FC Python 3.10 runtime looks for a callable named ``handler``
# and calls it with WSGI semantics for HTTP triggers. a2wsgi's
# ASGIMiddleware adapts an ASGI app (FastAPI) into a WSGI app.
handler = ASGIMiddleware(app)
