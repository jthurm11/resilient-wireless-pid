FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

# Install POSIX routing, tc netem facilities, build toolchain
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    iputils-ping \
    iperf3 \
    curl \
    ca-certificates \
    build-essential \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Establish an isolated virtual environment to prevent debian package collision
RUN python3 -m venv /opt/venv && \
    pip install --upgrade pip setuptools wheel

WORKDIR /opt/resilient-wireless-pid

# Copy manifests first to leverage Docker layer caching
COPY requirements.txt pyproject.toml ./
COPY src/ ./src/

# Install dependencies and entrypoint binaries in editable mode
RUN pip install -r requirements.txt && \
    pip install -e .

CMD ["/bin/bash"]