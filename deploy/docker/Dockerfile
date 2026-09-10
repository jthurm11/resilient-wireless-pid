FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

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

WORKDIR /opt/resilient-wireless-pid

# Copy manifests first to leverage Docker layer caching
COPY requirements.txt pyproject.toml ./
COPY src/ ./src/

# Install dependencies and entrypoint binaries
RUN python3 -m pip install --break-system-packages --upgrade pip setuptools wheel && \
    python3 -m pip install --break-system-packages -r requirements.txt && \
    python3 -m pip install --break-system-packages -e .

CMD ["/bin/bash"]