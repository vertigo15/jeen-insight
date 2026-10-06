"""Gunicorn settings for the Flask UI (``gunicorn -c python:src.ui_gunicorn_conf``).

Server flags (bind, workers, timeouts) stay on the command line in
``Dockerfile.ui``; this module adds what flags cannot express:

* ``logconfig_dict`` — gunicorn's own master and worker lines use the same JSON
  format as the app instead of gunicorn's plain-text handler;
* Prometheus multiprocess mode — workers write samples to
  ``PROMETHEUS_MULTIPROC_DIR``; the master serves the aggregate on
  ``METRICS_PORT``, so counters survive ``--max-requests`` worker recycling.
"""

from __future__ import annotations

import glob
import os

from src.logging_config import logging_dict

os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus-ui")

logconfig_dict = logging_dict()


def on_starting(server) -> None:
    directory = os.environ["PROMETHEUS_MULTIPROC_DIR"]
    os.makedirs(directory, exist_ok=True)
    # Samples from a previous master would be summed into this one's totals.
    for path in glob.glob(os.path.join(directory, "*.db")):
        os.remove(path)


def when_ready(server) -> None:
    from src import metrics

    if metrics.serve():
        server.log.info("metrics: serving /metrics on port %s", metrics.metrics_port())


def child_exit(server, worker) -> None:
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)
