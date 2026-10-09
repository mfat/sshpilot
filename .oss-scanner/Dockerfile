# Build environment for Anthropic's OSS Scanner (https://github.com/anthropics/oss-scanner).
#
# The scanner builds this with the repository checkout as the build context,
# then removes network access, so everything the tests or a reproducer need is
# installed here. Unlike tests/test-environment.Dockerfile, the GTK stack is
# included so the app itself can be run headless under Xvfb, and an OpenSSH
# server is available so connections can be exercised against localhost.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git \
        python3 python3-pip python3-venv \
        python3-gi python3-gi-cairo \
        gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91 \
        gir1.2-gtksource-5 gir1.2-secret-1 \
        openssh-client openssh-server sshpass \
        socat mosh picocom screen telnet \
        xvfb dbus dbus-x11 gnome-keyring \
    && rm -rf /var/lib/apt/lists/*

# scanner contract: the checkout lives inside the image, at /src
COPY . /src
WORKDIR /src

# Same Python dependencies CI installs (.github/workflows/tests.yml), plus the
# optional MCP servers and pexpect for the PTY integration tests.
# --ignore-installed: requirements.txt needs cryptography >= 42, newer than the
# Debian package pip cannot uninstall.
RUN pip3 install --ignore-installed -r requirements-dev.txt pyyaml pexpect mcp \
    && pip3 install --no-deps -e .

# Run the unit suite so the image is known to work, without failing the build.
RUN python3 -m pytest -ra -p no:cacheprovider -n auto \
        -m "not integration and not gui and not e2e and not stress and not property" \
        --basetemp=/tmp/pytest-tmp \
    || echo "WARNING: pytest reported failures"
