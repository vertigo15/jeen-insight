"""``python -m src.analytics_service`` — run the sandbox with uvicorn."""

from __future__ import annotations

import os

import uvicorn

from src import metrics
from src.logging_config import configure_logging


def main() -> None:
    configure_logging()
    metrics.serve()
    uvicorn.run(
        "src.analytics_service.app:app",
        host=os.getenv("ANALYTICS_HOST", "0.0.0.0"),
        port=int(os.getenv("ANALYTICS_PORT", "8100")),
        workers=1,
        # Keep the JSON configuration above instead of uvicorn's plain-text one.
        log_config=None,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
