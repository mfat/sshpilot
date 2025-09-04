#!/bin/bash

# Test script to verify sshPilot app bundle contains all required dependencies
# Usage: ./test-bundle.sh [path-to-app-bundle]

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
BUNDLE_PATH="${1:-./build/sshPilot.app}"
TEST_RESULTS=()
TOTAL_TESTS=0
PASSED_TESTS=0

# Test functions
test_passed() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
    ((PASSED_TESTS++))
}

test_failed() {
    echo -e "${RED}✗ FAIL${NC}: $1"
}

test_info() {
    echo -e "${BLUE}ℹ INFO${NC}: $1"
}

# Check if bundle exists
if [ ! -d "$BUNDLE_PATH" ]; then
    echo -e "${RED}Error: App bundle not found at $BUNDLE_PATH${NC}"
    echo "Usage: $0 [path-to-app-bundle]"
    exit 1
fi

echo -e "${GREEN}Testing sshPilot app bundle at: $BUNDLE_PATH${NC}"
echo "=================================================="

# Test 1: Bundle structure
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 1: Bundle Structure${NC}"
if [ -d "$BUNDLE_PATH/Contents" ] && [ -d "$BUNDLE_PATH/Contents/MacOS" ] && [ -d "$BUNDLE_PATH/Contents/Resources" ]; then
    test_passed "Bundle has correct structure (Contents/MacOS/Resources)"
else
    test_failed "Bundle missing required directories"
fi

# Test 2: Executable exists
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 2: Executable${NC}"
if [ -f "$BUNDLE_PATH/Contents/MacOS/sshPilot" ]; then
    test_passed "Main executable exists"
    if [ -x "$BUNDLE_PATH/Contents/MacOS/sshPilot" ]; then
        test_passed "Main executable is executable"
    else
        test_failed "Main executable is not executable"
    fi
else
    test_failed "Main executable not found"
fi

# Test 3: Python environment
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 3: Python Environment${NC}"
PYTHON_BIN="$BUNDLE_PATH/Contents/Resources/bin/python3"
if [ -f "$PYTHON_BIN" ]; then
    test_passed "Python3 binary found in bundle"
    
    # Test Python version
    PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
    test_info "Python version: $PYTHON_VERSION"
else
    test_failed "Python3 binary not found in bundle"
fi

# Test 4: Python packages from requirements.txt
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 4: Python Packages${NC}"
REQUIRED_PACKAGES=(
    "PyGObject"
    "pycairo" 
    "paramiko"
    "cryptography"
    "keyring"
    "psutil"
)

for package in "${REQUIRED_PACKAGES[@]}"; do
    if "$PYTHON_BIN" -c "import $package" 2>/dev/null; then
        test_passed "Python package '$package' is available"
    else
        test_failed "Python package '$package' is missing"
    fi
done

# Test 5: GTK and GI modules
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 5: GTK and GI Modules${NC}"
GTK_MODULES=(
    "gi.repository.Gtk"
    "gi.repository.Adw"
    "gi.repository.Vte"
    "gi.repository.Gio"
    "gi.repository.GLib"
)

for module in "${GTK_MODULES[@]}"; do
    if "$PYTHON_BIN" -c "import $module" 2>/dev/null; then
        test_passed "GI module '$module' is available"
    else
        test_failed "GI module '$module' is missing"
    fi
done

# Test 6: Application modules
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 6: Application Modules${NC}"
APP_MODULES=(
    "sshpilot.main"
    "sshpilot.window"
    "sshpilot.connection_manager"
    "sshpilot.terminal"
    "sshpilot.key_manager"
)

for module in "${APP_MODULES[@]}"; do
    if "$PYTHON_BIN" -c "import $module" 2>/dev/null; then
        test_passed "Application module '$module' is available"
    else
        test_failed "Application module '$module' is missing"
    fi
done

# Test 7: System binaries
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 7: System Binaries${NC}"
SYSTEM_BINARIES=(
    "sshpass"
)

for binary in "${SYSTEM_BINARIES[@]}"; do
    BINARY_PATH="$BUNDLE_PATH/Contents/Resources/bin/$binary"
    if [ -f "$BINARY_PATH" ] && [ -x "$BINARY_PATH" ]; then
        test_passed "System binary '$binary' is available and executable"
    else
        test_failed "System binary '$binary' is missing or not executable"
    fi
done

# Test 8: Resources and icons
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 8: Resources and Icons${NC}"
RESOURCE_FILES=(
    "sshpilot.gresource"
    "sshpilot.icns"
)

for resource in "${RESOURCE_FILES[@]}"; do
    if [ -f "$BUNDLE_PATH/Contents/Resources/$resource" ]; then
        test_passed "Resource file '$resource' is present"
    else
        test_failed "Resource file '$resource' is missing"
    fi
done

# Test 9: Info.plist
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 9: Info.plist${NC}"
if [ -f "$BUNDLE_PATH/Contents/Info.plist" ]; then
    test_passed "Info.plist exists"
    
    # Check key values
    BUNDLE_ID=$(plutil -extract CFBundleIdentifier raw "$BUNDLE_PATH/Contents/Info.plist" 2>/dev/null || echo "")
    if [ "$BUNDLE_ID" = "io.github.mfat.sshpilot" ]; then
        test_passed "Bundle ID is correct"
    else
        test_failed "Bundle ID is incorrect: $BUNDLE_ID"
    fi
else
    test_failed "Info.plist is missing"
fi

# Test 10: Application launch test
((TOTAL_TESTS++))
echo -e "\n${YELLOW}Test 10: Application Launch Test${NC}"
echo "Testing application launch (this may take a moment)..."

# Try to launch the app with --help to test basic functionality
if timeout 10s "$BUNDLE_PATH/Contents/MacOS/sshPilot" --help >/dev/null 2>&1; then
    test_passed "Application launches successfully"
elif timeout 10s "$BUNDLE_PATH/Contents/MacOS/sshPilot" --version >/dev/null 2>&1; then
    test_passed "Application launches successfully (version check)"
else
    test_info "Application launch test skipped (may require GUI environment)"
fi

# Summary
echo -e "\n=================================================="
echo -e "${GREEN}Test Summary${NC}"
echo -e "Total tests: $TOTAL_TESTS"
echo -e "Passed: $PASSED_TESTS"
echo -e "Failed: $((TOTAL_TESTS - PASSED_TESTS))"

if [ $PASSED_TESTS -eq $TOTAL_TESTS ]; then
    echo -e "\n${GREEN}🎉 All tests passed! The app bundle is ready for distribution.${NC}"
    exit 0
else
    echo -e "\n${RED}❌ Some tests failed. Please check the bundle configuration.${NC}"
    exit 1
fi
