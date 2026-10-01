ARG PYTHON_VER="3.10"

FROM python:${PYTHON_VER}-slim AS diffsync

# Install Poetry manually via its installer script;
# if we instead used "pip install poetry" it would install its own dependencies globally which may conflict with ours.
# https://python-poetry.org/docs/master/#installing-with-the-official-installer
# This also makes it so that Poetry will *not* be included in the "final" image since it's not installed to /usr/local/
ARG POETRY_HOME=/opt/poetry
ARG POETRY_INSTALLER_PARALLEL=true
ARG POETRY_VERSION=2.1.3
ARG POETRY_VIRTUALENVS_CREATE=false
ADD https://install.python-poetry.org /tmp/install-poetry.py
RUN python /tmp/install-poetry.py

# Add poetry install location to the $PATH
ENV PATH="${POETRY_HOME}/bin:${PATH}"

RUN poetry config virtualenvs.create ${POETRY_VIRTUALENVS_CREATE} && \
    poetry config installer.parallel "${POETRY_INSTALLER_PARALLEL}"

# Install redis-server for pytest-redis
RUN apt-get update && \
    apt-get install -y --no-install-recommends redis-server && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /local
COPY . /local

# Install the app
RUN poetry install --all-groups

# Release check stage: runs the same single entry point as CI
# (`inv release-check`, defined once in tasks.py). Build it with:
#   docker build --target release-check .
FROM diffsync AS release-check
RUN inv release-check
