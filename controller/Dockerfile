# Ryu 4.34 (last release, 2019) needs an older toolchain:
# - its setup.py uses setuptools.command.easy_install.get_script_args, which
#   modern setuptools removed  -> pin setuptools 57 and disable build isolation
# - ryu-manager imports eventlet.wsgi.ALREADY_HANDLED, removed in eventlet 0.31
#   -> eventlet 0.30.2, which supports Python 3.9
FROM python:3.9-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "setuptools==57.5.0" wheel \
    && pip install --no-build-isolation ryu==4.34 eventlet==0.30.2 \
    && pip install "PyYAML==6.0.1"

WORKDIR /opt/controller
COPY apps/ ./apps/

# 6653 OpenFlow, 8080 REST API (/sh/...)
EXPOSE 6653 8080

ENTRYPOINT ["ryu-manager", "--ofp-tcp-listen-port", "6653"]
CMD ["--wsapi-port", "8080", "apps/selfhealing_app.py"]
