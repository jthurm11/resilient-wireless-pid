FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

# Install POSIX routing, tc netem facilities, and Python 3.13 build toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    iputils-ping \
    iperf3 \
    curl \
    ca-certificates \
    build-essential \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Establish isolated virtual environment to isolate third-party wheels from Debian packages
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip setuptools wheel

WORKDIR /opt/resilient-wireless-pid

# Cache third-party dependencies independently of application source code
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy entrypoint wrapper before local source
COPY deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Copy manifests and source tree, then install console scripts in editable mode
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-deps -e .

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bin/bash"]