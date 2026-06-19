# Test environment for sshPilot's protocol plugins.
#
# Provides the real CLI tools the terminal-native protocol backends spawn
# (mosh, kubectl, kind, docker, socat — plus picocom/screen for serial and
# openssh-client/sshpass for ssh) so the pytest suite — including the
# `integration`-marked tests that actually run those binaries — can verify, on
# every PR, that argument parsing and the PTY launch keep working.
#
# The GTK stack is intentionally NOT installed: tests stub gi.repository
# (tests/conftest.py), so the image stays small. The repo is mounted at run
# time (-v "$PWD":/work), not COPYed, so code edits don't rebuild the image.
#
# Build:  docker build -f test-environment.Dockerfile -t sshpilot-test-env .
# Run:    docker run --rm -v "$PWD":/work -w /work \
#                 -v /var/run/docker.sock:/var/run/docker.sock \
#                 sshpilot-test-env pytest -ra
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1

# System tools the protocol backends invoke, + python/test prerequisites.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        python3 python3-pip \
        openssh-client sshpass \
        socat mosh picocom screen \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

# kubectl + kind are not in the Ubuntu repos — install the release binaries.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) k_arch=amd64 ;; arm64) k_arch=arm64 ;; *) k_arch=amd64 ;; esac; \
    kver="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; \
    curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${kver}/bin/linux/${k_arch}/kubectl"; \
    curl -fsSL -o /usr/local/bin/kind \
        "https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-${k_arch}"; \
    chmod +x /usr/local/bin/kubectl /usr/local/bin/kind; \
    kubectl version --client; kind version

# Python test dependencies (mirrors .github/workflows/tests.yml + pexpect for the
# PTY integration tests and wakeonlan so its xfail test passes). PyGObject is
# omitted on purpose — gi is stubbed by the test suite.
RUN pip3 install \
        paramiko cryptography keyring psutil certifi \
        pytest pytest-cov pexpect wakeonlan

WORKDIR /work
CMD ["pytest", "-ra"]
