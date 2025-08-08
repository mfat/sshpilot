#!/usr/bin/env bash
set -euo pipefail

echo "SSHPilot - Debian Package Builder"
echo "================================="

if [ ! -d "debian" ]; then
  echo "Error: debian/ directory not found."
  exit 1
fi

echo "Cleaning previous builds..."
rm -rf debian/sshpilot || true
rm -f ../sshpilot_*.deb ../sshpilot_*.changes ../sshpilot_*.buildinfo || true

# Get version from __init__.py
VERSION=$(python3 -c "import re, pathlib; t=pathlib.Path('sshpilot/__init__.py').read_text(); print(re.search(r'__version__\\s*=\\s*\"([^\"]+)\"', t).group(1))")
MAINTAINER_NAME="mFat"
MAINTAINER_EMAIL="newmfat@gmail.com"
DATE=$(date -R)
cat > debian/changelog <<EOF
sshpilot (${VERSION}-1) unstable; urgency=medium

  * Automated release build.

 -- ${MAINTAINER_NAME} <${MAINTAINER_EMAIL}>  ${DATE}
EOF

echo "Building Debian package..."
dpkg-buildpackage -us -uc -b

echo "Build completed. Files in parent directory:"
ls -la ../sshpilot_*.deb 2>/dev/null || echo "No .deb files found"

echo "To install: sudo dpkg -i ../sshpilot_*.deb && sudo apt-get -f install"


