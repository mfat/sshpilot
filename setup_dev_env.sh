#!/bin/bash
# Setup script for sshpilot development environment on macOS

echo "Setting up sshpilot development environment..."

# Check if Homebrew is installed
if ! command -v brew &> /dev/null; then
    echo "❌ Homebrew is not installed. Please install it first:"
    echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
fi

# Install required dependencies
echo "📦 Installing Homebrew dependencies..."
brew install gtk4 libadwaita vte3 adwaita-icon-theme gobject-introspection pygobject3 sshpass

# Ensure libadwaita is properly linked
echo "🔗 Linking libadwaita..."
brew link --overwrite libadwaita

# Get Homebrew prefix and Python version
BREW_PREFIX=$(brew --prefix)
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

echo "🏠 Homebrew prefix: $BREW_PREFIX"
echo "🐍 Python version: $PYTHON_VERSION"

# Set up environment variables
export PYTHONPATH="$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages:$PYTHONPATH"
export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib:$DYLD_LIBRARY_PATH"
export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"

echo "✅ Environment variables set:"
echo "   PYTHONPATH: $PYTHONPATH"
echo "   GI_TYPELIB_PATH: $GI_TYPELIB_PATH"
echo "   DYLD_LIBRARY_PATH: $DYLD_LIBRARY_PATH"
echo "   PKG_CONFIG_PATH: $PKG_CONFIG_PATH"

# Test the setup
echo "🧪 Testing PyGObject and libadwaita availability..."
python3 -c "
import sys
sys.path.insert(0, '$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages')
import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
gi.require_version('Vte', '3.91')
from gi.repository import Adw, Gtk, Vte
print('✅ All components available!')
print('   - PyGObject (gi)')
print('   - libadwaita (Adw)')
print('   - GTK4 (Gtk)')
print('   - VTE (Vte)')
" || {
    echo "❌ Setup failed. Please check the error messages above."
    exit 1
}

echo ""
echo "🎉 Development environment setup complete!"
echo ""
echo "To run the application:"
echo "   python3 run.py"
echo ""
echo "To make these environment variables permanent, add to your ~/.zshrc:"
echo "   export BREW_PREFIX=\$(brew --prefix)"
echo "   export PYTHON_VERSION=\$(python3 -c \"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')\")"
echo "   export PYTHONPATH=\"\$BREW_PREFIX/lib/python\$PYTHON_VERSION/site-packages:\$PYTHONPATH\""
echo "   export GI_TYPELIB_PATH=\"\$BREW_PREFIX/lib/girepository-1.0\""
echo "   export DYLD_LIBRARY_PATH=\"\$BREW_PREFIX/lib:\$DYLD_LIBRARY_PATH\""
echo "   export PKG_CONFIG_PATH=\"\$BREW_PREFIX/lib/pkgconfig:\$PKG_CONFIG_PATH\""
