"""Entry point: ``python -m kvstore`` starts one node configured from ``KV_*`` env vars."""

from __future__ import annotations

import uvicorn

from kvstore.core.config import get_settings
from kvstore.core.logging import configure_logging
from kvstore.main import create_app


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    # Passing the app object (not an import string) pins uvicorn to a single
    # process, which a stateful in-memory node requires.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.http_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
