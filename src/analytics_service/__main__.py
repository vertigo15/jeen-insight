"""``python -m src.analytics_service`` — run the sandbox with uvicorn."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "src.analytics_service.app:app",
        host=os.getenv("ANALYTICS_HOST", "0.0.0.0"),
        port=int(os.getenv("ANALYTICS_PORT", "8100")),
        workers=1,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
