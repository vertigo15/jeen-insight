# jeen-insights-api — the FastAPI service.
#
# Multi-stage: the builder carries compilers and libpq headers so a dependency
# without a wheel can still build; the runtime image carries only the venv and
# the code. OpenShift restricted-v2 compatible: numeric non-root USER, files
# owned 1001:0 with group==owner permissions so the arbitrary UID the SCC
# assigns (always in GID 0) can read everything, read-only root filesystem
# friendly (every writable path is /tmp: HOME, matplotlib cache, sweetviz
# scratch). Package managers are removed from the runtime layer.

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt .
# Fresh pip/setuptools/wheel first: the copies a stock venv ships with are old
# enough to be flagged by image scanners. The lock records exactly what the
# runtime image contains; build-images.sh ships it next to the tarball. pip and
# wheel are then removed from the venv (nothing needs them at runtime;
# setuptools stays because some libraries still import pkg_resources), and the
# tree is handed to 1001:0 with group==owner permissions HERE, in the builder:
# COPY --from preserves ownership and modes, whereas a chown in the runtime
# stage would duplicate the whole venv into a second layer.
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && pip freeze --all > /opt/venv/requirements.lock \
    && pip uninstall -y pip wheel \
    && chown -R 1001:0 /opt/venv \
    && chmod -R g=u /opt/venv


FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/mpl \
    # Set to true to apply db/migrations/insights before the server starts.
    RUN_MIGRATIONS_ON_START=false

WORKDIR /app

# The base image's own pip/setuptools/wheel are the usual scanner findings and
# nothing in the runtime needs them.
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel

COPY --from=builder /opt/venv /opt/venv

# Only what the API needs at runtime: the package, the migration runner and its
# SQL revisions, and the insight prompt template read by src/agent.
COPY --chown=1001:0 src/ ./src/
COPY --chown=1001:0 scripts/ ./scripts/
COPY --chown=1001:0 db/ ./db/
COPY --chown=1001:0 templates/ ./templates/

# Pre-compile so a random UID never needs to write __pycache__, then make the
# (small) application tree 1001:0 with group==owner permissions.
RUN python -m compileall -q src scripts \
    && chmod 0755 scripts/api-entrypoint.sh \
    && chown -R 1001:0 /app \
    && chmod -R g=u /app

USER 1001:0

EXPOSE 8000

ENTRYPOINT ["/app/scripts/api-entrypoint.sh"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
