# Mock SSH server with 2FA-style keyboard-interactive auth for sshPilot tests.
#
# Login = password ("password") + verification code ("123456"), served as two
# keyboard-interactive prompts via PAM (pam_unix + the pam_mock_otp module
# built here). Deterministic — no TOTP clocks. Complements the plain
# password-only linuxserver/openssh-server demo containers (ports 2221-2224).
#
# Build & run:  docker compose up -d --build   (listens on host port 2225)
# Try it:       ssh -p 2225 testuser@localhost   # password, then 123456
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server gcc libc6-dev libpam0g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pam_mock_otp.c /tmp/pam_mock_otp.c
RUN gcc -fPIC -shared -o "/usr/lib/$(gcc -dumpmachine)/security/pam_mock_otp.so" \
        /tmp/pam_mock_otp.c -lpam && rm /tmp/pam_mock_otp.c

RUN useradd -m -s /bin/bash testuser \
    && echo 'testuser:password' | chpasswd \
    && mkdir -p /run/sshd \
    && ssh-keygen -A

# PAM stack: unix password first, then the mock verification code.
RUN printf '%s\n' \
        'auth required pam_unix.so' \
        'auth required pam_mock_otp.so' \
        'account required pam_unix.so' \
        'session required pam_permit.so' \
    > /etc/pam.d/sshd

# Force keyboard-interactive (the only method), which routes through PAM.
RUN printf '%s\n' \
        'Port 2222' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication yes' \
        'UsePAM yes' \
        'AuthenticationMethods keyboard-interactive' \
        'PermitRootLogin no' \
        'MaxSessions 10' \
        'Subsystem sftp /usr/lib/openssh/sftp-server' \
    > /etc/ssh/sshd_config.d/mock2fa.conf

EXPOSE 2222
CMD ["/usr/sbin/sshd", "-D", "-e"]
