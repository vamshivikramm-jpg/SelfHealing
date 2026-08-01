FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install ryu==4.34 eventlet==0.33.3

WORKDIR /opt/controller
COPY apps/ ./apps/

EXPOSE 6653

ENTRYPOINT ["ryu-manager", "--ofp-tcp-listen-port", "6653"]
CMD ["apps/topology_controller.py", "apps/l2_forwarding.py"]
