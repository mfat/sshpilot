#!/bin/bash
# sshpilot macOS Launcher Script
# This script sets up the environment and launches sshpilot

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to get the directory where this script is located
get_script_dir() {
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

# Function to check and install dependencies
check_dependencies() {
    print_status "Checking dependencies..."
    
    # Check if Homebrew is installed
    if ! command_exists brew; then
        print_error "Homebrew is not installed. Please install it first:"
        echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        exit 1
    fi
    
    # Check if required packages are installed
    local missing_packages=()
    
    if ! brew list gtk4 >/dev/null 2>&1; then
        missing_packages+=("gtk4")
    fi
    
    if ! brew list libadwaita >/dev/null 2>&1; then
        missing_packages+=("libadwaita")
    fi
    
    if ! brew list pygobject3 >/dev/null 2>&1; then
        missing_packages+=("pygobject3")
    fi
    
    if ! brew list vte3 >/dev/null 2>&1; then
        missing_packages+=("vte3")
    fi
    
    if ! brew list gobject-introspection >/dev/null 2>&1; then
        missing_packages+=("gobject-introspection")
    fi
    
    if ! brew list adwaita-icon-theme >/dev/null 2>&1; then
        missing_packages+=("adwaita-icon-theme")
    fi
    
    if ! brew list sshpass >/dev/null 2>&1; then
        missing_packages+=("sshpass")
    fi
    
    # Install missing packages
    if [ ${#missing_packages[@]} -ne 0 ]; then
        print_warning "Missing packages: ${missing_packages[*]}"
        print_status "Installing missing packages..."
        brew install "${missing_packages[@]}"
        
        # Ensure libadwaita is properly linked
        print_status "Linking libadwaita..."
        brew link --overwrite libadwaita
    else
        print_success "All dependencies are installed"
    fi
}

# Function to set up environment variables
setup_environment() {
    print_status "Setting up environment variables..."
    
    # Get Homebrew prefix and Python version
    export BREW_PREFIX=$(brew --prefix)
    export PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    
    # Set up environment variables for PyGObject and GTK4
    export PYTHONPATH="$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages:$PYTHONPATH"
    export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
    export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib:$DYLD_LIBRARY_PATH"
    export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"
    export XDG_DATA_DIRS="$BREW_PREFIX/share:$XDG_DATA_DIRS"
    
    print_success "Environment variables set"
}

# Function to test the setup
test_setup() {
    print_status "Testing PyGObject and libadwaita availability..."
    
    python3 -c "
import sys
sys.path.insert(0, '$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages')
try:
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
except Exception as e:
    print(f'❌ Setup failed: {e}')
    sys.exit(1)
" || {
        print_error "Setup test failed. Please check the error messages above."
        exit 1
    }
    
    print_success "Setup test passed"
}

# Function to launch the application
launch_application() {
    print_status "Launching sshpilot..."
    
    # Get the script directory
    local script_dir=$(get_script_dir)
    
    # Check if we're in a virtual environment
    if [[ "$VIRTUAL_ENV" != "" ]]; then
        print_status "Using virtual environment: $VIRTUAL_ENV"
        python3 "$script_dir/run.py"
    else
        # Check if run.py exists in the current directory
        if [[ -f "$script_dir/run.py" ]]; then
            python3 "$script_dir/run.py"
        else
            print_error "run.py not found in $script_dir"
            print_status "Please run this script from the sshpilot project directory"
            exit 1
        fi
    fi
}

# Main execution
main() {
    echo "🚀 sshpilot macOS Launcher"
    echo "=========================="
    
    # Check dependencies
    check_dependencies
    
    # Set up environment
    setup_environment
    
    # Test the setup
    test_setup
    
    # Launch the application
    launch_application
}

# Run main function
main "$@"
