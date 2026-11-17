FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    openvswitch-switch \
    openvswitch-common \
    iproute2 \
    iputils-ping \
    mininet \
    python3-yaml \
    python3-pytest \
    python3-setuptools \
    python3-numpy \
    python3-scipy \
    python3-sklearn \
    python3-joblib \
    && rm -rf /var/lib/apt/lists/*

# The live dashboard (viewer, VIEWER_MODE=live) runs in this container next
# to Mininet: FastAPI + uvicorn from PyPI, the numeric stack from apt above.
RUN pip3 install --break-system-packages --no-cache-dir "fastapi==0.115.0" "uvicorn==0.30.6"

# Ubuntu 24.04 refuses pip installs into the system Python (PEP 668), so
# pytest comes from apt and the project is installed with
# --break-system-packages (safe inside this single-purpose container).

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
RUN pip3 install --break-system-packages --no-build-isolation --no-deps -e .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
