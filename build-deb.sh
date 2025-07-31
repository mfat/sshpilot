#!/bin/bash

# SSHPilot Debian Package Builder
# This script builds a .deb package for SSHPilot

set -e

echo "SSHPilot - Debian Package Builder"
echo "================================="

# Check if we're in the right directory
if [ ! -f "sshpilot.py" ]; then
    echo "Error: sshpilot.py not found. Please run this script from the project root."
    exit 1
fi

# Check if debian directory exists
if [ ! -d "debian" ]; then
    echo "Error: debian/ directory not found. Please create the Debian packaging files first."
    exit 1
fi

# Install build dependencies if needed
echo "Checking build dependencies..."
if ! dpkg-query -W debhelper >/dev/null 2>&1; then
    echo "Installing build dependencies..."
    sudo apt update
    sudo apt install -y debhelper dh-python python3-all python3-setuptools
fi

# Clean any previous builds
echo "Cleaning previous builds..."
rm -rf debian/sshpilot
rm -f ../sshpilot_*.deb ../sshpilot_*.changes ../sshpilot_*.buildinfo

# Build the package
echo "Building Debian package..."
dpkg-buildpackage -us -uc -b

echo ""
echo "Build completed successfully!"
echo ""
echo "Package files created in parent directory:"
ls -la ../sshpilot_*.deb 2>/dev/null || echo "No .deb files found"

echo ""
echo "To install the package:"
echo "sudo dpkg -i ../sshpilot_*.deb"
echo "sudo apt-get install -f  # Fix any dependency issues"