#!/bin/bash
# Test script to verify app is self-contained and all modules work

APP_PATH="./dist/SSHPilot.app"

echo "Testing redistributability and module functionality of $APP_PATH"
echo "=================================================================="

if [ ! -d "$APP_PATH" ]; then
    echo "❌ App not found at $APP_PATH"
    exit 1
fi

echo "✅ App bundle exists"

# Get Python executable path
PYTHON_EXEC="$APP_PATH/Contents/MacOS/SSHPilot"
if [ ! -f "$PYTHON_EXEC" ]; then
    echo "❌ Main executable not found at $PYTHON_EXEC"
    exit 1
fi

# Check Python framework
PYTHON_FRAMEWORK="$APP_PATH/Contents/Frameworks/Python"
if [ -f "$PYTHON_FRAMEWORK" ]; then
    echo "✅ Python framework bundled"
    echo "   Python dependencies:"
    otool -L "$PYTHON_FRAMEWORK" | grep -v "$APP_PATH" | grep -v "@rpath" | sed 's/^[[:space:]]*/   /'
else
    echo "❌ No Python framework found"
fi

# Check GTK/GLib libraries
FRAMEWORKS_DIR="$APP_PATH/Contents/Frameworks"
LIB_COUNT=$(find "$FRAMEWORKS_DIR" -name "*.dylib" | wc -l)
echo "✅ Found $LIB_COUNT bundled libraries"

# Check for external dependencies
echo ""
echo "Checking for external dependencies..."
EXTERNAL_DEPS=$(otool -L "$FRAMEWORKS_DIR"/*.dylib 2>/dev/null | \
    grep -v "@rpath\|/System/\|/usr/lib/\|$APP_PATH" | \
    grep -v ":" | \
    sort -u)

if [ -z "$EXTERNAL_DEPS" ]; then
    echo "✅ No external dependencies found - app should be redistributable!"
else
    echo "❌ External dependencies found:"
    echo "$EXTERNAL_DEPS" | sed 's/^/   /'
    echo ""
    echo "Add these to your spec file gtk_libs_patterns:"
    echo "$EXTERNAL_DEPS" | sed 's|.*/||' | sed 's/\..*\.dylib/.*.dylib/' | sed 's/^/    "/' | sed 's/$/"/'
fi

# Check critical resources
RESOURCES_DIR="$APP_PATH/Contents/Resources"
echo ""
echo "Checking critical resources..."

if [ -d "$RESOURCES_DIR/share/glib-2.0/schemas" ]; then
    echo "✅ GSettings schemas found"
else
    echo "❌ Missing GSettings schemas"
fi

if [ -d "$RESOURCES_DIR/share/icons/Adwaita" ]; then
    echo "✅ Adwaita icons found"
else
    echo "❌ Missing Adwaita icons"
fi

if [ -d "$RESOURCES_DIR/girepository-1.0" ]; then
    TYPELIB_COUNT=$(find "$RESOURCES_DIR/girepository-1.0" -name "*.typelib" | wc -l)
    echo "✅ Found $TYPELIB_COUNT GObject Introspection typelibs"
else
    echo "❌ Missing GObject Introspection typelibs"
fi

if [ -f "$RESOURCES_DIR/sshpilot/resources/sshpilot.gresource" ]; then
    echo "✅ GResource bundle found"
else
    echo "❌ Missing GResource bundle"
fi

# Test Python module imports
echo ""
echo "Testing Python module imports..."
echo "================================"

# Test core dependencies
echo "Testing core dependencies..."
CORE_MODULES=("paramiko" "cryptography" "keyring" "psutil" "cairo")
for module in "${CORE_MODULES[@]}"; do
    if "$PYTHON_EXEC" -c "import $module; print('✅ $module imported successfully')" 2>/dev/null; then
        :
    else
        echo "❌ Failed to import $module"
    fi
done

# Test GTK/PyGObject components
echo ""
echo "Testing GTK/PyGObject components..."
GTK_MODULES=("gi.repository.Adw" "gi.repository.Gtk" "gi.repository.Vte" "gi.repository.Gio" "gi.repository.GLib" "gi.repository.GObject" "gi.repository.Pango")
for module in "${GTK_MODULES[@]}"; do
    if "$PYTHON_EXEC" -c "import gi; gi.require_version('${module##*.}', '4.0' if '${module##*.}' == 'Gtk' else '1' if '${module##*.}' == 'Adw' else '3.91' if '${module##*.}' == 'Vte' else '2.0'); from gi.repository import ${module##*.}; print('✅ $module imported successfully')" 2>/dev/null; then
        :
    else
        echo "❌ Failed to import $module"
    fi
done

# Test application-specific modules
echo ""
echo "Testing application modules..."
APP_MODULES=("sshpilot.connection_manager" "sshpilot.terminal" "sshpilot.config" "sshpilot.key_manager" "sshpilot.ssh_utils" "sshpilot.sftp_utils" "sshpilot.port_utils")
for module in "${APP_MODULES[@]}"; do
    if "$PYTHON_EXEC" -c "import $module; print('✅ $module imported successfully')" 2>/dev/null; then
        :
    else
        echo "❌ Failed to import $module"
    fi
done

# Test main application entry point
echo ""
echo "Testing main application entry point..."
if "$PYTHON_EXEC" -c "import sshpilot.main; print('✅ Main application module imported successfully')" 2>/dev/null; then
    echo "✅ Main application module works"
else
    echo "❌ Failed to import main application module"
fi

# Test external tool dependencies
echo ""
echo "Testing external tool dependencies..."
echo "===================================="

# Check if ssh is available
if command -v ssh >/dev/null 2>&1; then
    echo "✅ SSH client available"
else
    echo "❌ SSH client not found"
fi

# Check if sshpass is available (optional)
if command -v sshpass >/dev/null 2>&1; then
    echo "✅ sshpass available"
else
    echo "⚠️  sshpass not found (optional for password authentication)"
fi

# Test basic app functionality
echo ""
echo "Testing basic app functionality..."
echo "================================="

# Test version check
if "$PYTHON_EXEC" --version 2>/dev/null | grep -q "SSHPilot"; then
    echo "✅ App version check works"
else
    echo "❌ App version check failed"
fi

# Test help output
if "$PYTHON_EXEC" --help 2>/dev/null | grep -q "SSHPilot"; then
    echo "✅ App help output works"
else
    echo "❌ App help output failed"
fi

echo ""
echo "=================================================================="
echo "Test complete!"
echo ""
echo "Summary:"
echo "- Infrastructure tests: Bundle structure, libraries, resources"
echo "- Module import tests: Python dependencies, GTK components, app modules"
echo "- Functionality tests: External tools, basic app operations"
echo ""
echo "If no ❌ errors above, your app should be fully redistributable!"