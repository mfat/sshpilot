#!/bin/bash

# Test script for sshpilot bundle
# Usage: ./test-bundle.sh [bundle_path]

BUNDLE_PATH="${1:-/Users/mfattahi/GitHub/sshpilot/packaging/macos/build/sshPilot.app}"

if [ ! -d "$BUNDLE_PATH" ]; then
    echo "Error: Bundle not found at $BUNDLE_PATH"
    echo "Usage: $0 [bundle_path]"
    exit 1
fi

echo "Testing sshpilot bundle at: $BUNDLE_PATH"
echo "=========================================="

# Test 1: Check if Python executable exists and works
echo "1. Testing Python executable..."
PYTHON_EXEC="$BUNDLE_PATH/Contents/Resources/bin/python3"
if [ -f "$PYTHON_EXEC" ]; then
    echo "   ✓ Python executable found"
    if "$PYTHON_EXEC" -c "import sys; print('   Python version:', sys.version.split()[0])" 2>/dev/null; then
        echo "   ✓ Python executable works"
    else
        echo "   ✗ Python executable failed to run"
    fi
else
    echo "   ✗ Python executable not found"
fi

# Test 2: Check if main executable exists
echo "2. Testing main executable..."
MAIN_EXEC="$BUNDLE_PATH/Contents/MacOS/sshPilot"
if [ -f "$MAIN_EXEC" ]; then
    echo "   ✓ Main executable found"
    echo "   ✓ Executable is: $(file "$MAIN_EXEC" | cut -d: -f2)"
else
    echo "   ✗ Main executable not found"
fi

# Test 3: Check Python framework
echo "3. Testing Python framework..."
PYTHON_FRAMEWORK="$BUNDLE_PATH/Contents/Frameworks/Versions/3.13/Python"
if [ -f "$PYTHON_FRAMEWORK" ]; then
    echo "   ✓ Python framework found"
    echo "   Dependencies:"
    otool -L "$PYTHON_FRAMEWORK" | grep -v "System\|CoreFoundation" | sed 's/^/     /'
else
    echo "   ✗ Python framework not found"
fi

# Test 4: Check bundled libraries
echo "4. Testing bundled libraries..."
LIB_DIR="$BUNDLE_PATH/Contents/Resources/lib"
if [ -d "$LIB_DIR" ]; then
    LIB_COUNT=$(find "$LIB_DIR" -name "*.dylib" | wc -l)
    echo "   ✓ Found $LIB_COUNT bundled libraries"
else
    echo "   ✗ Library directory not found"
fi

# Test 5: Check if bundle is self-contained
echo "5. Testing self-containment..."
echo "   Checking Python dependencies..."
if otool -L "$PYTHON_EXEC" | grep -q "/opt/homebrew"; then
    echo "   ✗ Python still has Homebrew dependencies"
else
    echo "   ✓ Python is self-contained"
fi

echo "=========================================="
echo "Bundle test completed!"
echo ""
echo "To test the app manually:"
echo "  cd $(dirname "$BUNDLE_PATH")"
echo "  ./sshPilot.app/Contents/MacOS/sshPilot --version"
echo ""
echo "If the app hangs, try:"
echo "  ./sshPilot.app/Contents/Resources/bin/python3 -c \"import sshpilot; print('sshpilot imported successfully')\""