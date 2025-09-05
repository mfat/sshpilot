#!/bin/bash
# Test script to verify app is self-contained

APP_PATH="./dist/SSHPilot.app"

echo "Testing redistributability of $APP_PATH"
echo "================================================"

if [ ! -d "$APP_PATH" ]; then
    echo "❌ App not found at $APP_PATH"
    exit 1
fi

echo "✅ App bundle exists"

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

if [ -f "$RESOURCES_DIR/sshpilot/sshpilot.gresource" ]; then
    echo "✅ GResource bundle found"
else
    echo "❌ Missing GResource bundle"
fi

echo ""
echo "Test complete. If no ❌ errors above, your app should be redistributable!"