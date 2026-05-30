# syntax=docker/dockerfile:1.6

# Menvayal Agent container — built for balena fleet deployment.
#
# The image targets every Raspberry Pi that ships in v1 (Zero 2 W / 3 / 4 / 5)
# via `%%BALENA_MACHINE_NAME%%`, which the balena builder substitutes per-device
# at build time. See BALENA.md for the operator workflow.
#
# Hardware access (GPIO / I2C / SPI / 1-Wire / serial) requires the container
# to run privileged — set in docker-compose.yml.

FROM balenalib/%%BALENA_MACHINE_NAME%%-python:3.12-bookworm-run

# Install OS packages needed by the Python wheels and by sensor hardware.
RUN install_packages \
    python3-pip \
    python3-venv \
    libffi-dev \
    libssl-dev \
    i2c-tools \
    && rm -rf /var/lib/apt/lists/*

# Disable the balena base image's "idle" sleep — we want the entrypoint to run.
ENV INITSYSTEM=off
ENV UDEV=on

# Application code is installed in /opt/agal-agent (matches the systemd
# install path so any future fallback to systemd keeps the same layout).
WORKDIR /opt/agal-agent

# Copy dependency manifests first to leverage Docker layer caching.
COPY requirements.txt setup.py ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent source and install in editable mode.
COPY agal_agent ./agal_agent
COPY scripts ./scripts
COPY systemd ./systemd
RUN pip install --no-cache-dir -e . \
    && chmod +x ./scripts/entrypoint.sh ./scripts/render_config.py

# Writable directories that the agent uses at runtime. balena will mount
# named volumes here per docker-compose.yml so they survive image updates.
RUN mkdir -p /var/lib/agal-agent /etc/agal-agent

ENTRYPOINT ["/opt/agal-agent/scripts/entrypoint.sh"]
