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
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir pytest==8.2.2

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
RUN pip3 install --no-deps -e .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
