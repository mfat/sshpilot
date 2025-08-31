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

# Function to check and install Homebrew
check_homebrew() {
    print_status "Checking Homebrew installation..."
    
    if ! command_exists brew; then
        print_warning "Homebrew is not installed. Installing now..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        
        # Add Homebrew to PATH for current session
        if [[ -f "/opt/homebrew/bin/brew" ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -f "/usr/local/bin/brew" ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        
        print_success "Homebrew installed successfully"
    else
        print_success "Homebrew is already installed"
    fi
}

# Function to check and install system dependencies
check_system_dependencies() {
    print_status "Checking system dependencies..."
    
    # List of required Homebrew packages
    local required_packages=(
        "gtk4"
        "libadwaita"
        "pygobject3"
        "py3cairo"
        "vte3"
        "gobject-introspection"
        "adwaita-icon-theme"
        "pkg-config"
        "glib"
        "graphene"
        "icu4c"
        "sshpass"
    )
    
    # Get all installed packages at once (much faster)
    local installed_packages=$(brew list --formula 2>/dev/null || brew list)
    local missing_packages=()
    
    # Check each required package
    for package in "${required_packages[@]}"; do
        if ! echo "$installed_packages" | grep -q "^${package}$"; then
            missing_packages+=("$package")
        fi
    done
    
    # Install missing packages
    if [ ${#missing_packages[@]} -ne 0 ]; then
        print_warning "Missing packages: ${missing_packages[*]}"
        print_status "Installing missing packages..."
        brew install "${missing_packages[@]}"
        
        # Ensure libadwaita is properly linked
        print_status "Linking libadwaita..."
        brew link --overwrite libadwaita 2>/dev/null || true
        
        print_success "All system dependencies installed"
    else
        print_success "All system dependencies are already installed"
    fi
}

# Function to check and install Python dependencies
check_python_dependencies() {
    print_status "Checking Python dependencies..."
    
    # Get the script directory to find requirements.txt
    local script_dir=$(get_script_dir)
    local requirements_file="$script_dir/requirements.txt"
    
    if [[ ! -f "$requirements_file" ]]; then
        print_error "requirements.txt not found in $script_dir"
        exit 1
    fi
    
    # Check if we're in a virtual environment
    if [[ "$VIRTUAL_ENV" != "" ]]; then
        print_status "Using virtual environment: $VIRTUAL_ENV"
        pip install -r "$requirements_file"
    else
        # Check if key packages are installed
        local missing_python_packages=()
        
        # Check key packages that are essential
        if ! python3 -c "import paramiko" 2>/dev/null; then
            missing_python_packages+=("paramiko")
        fi
        
        if ! python3 -c "import cryptography" 2>/dev/null; then
            missing_python_packages+=("cryptography")
        fi
        
        if ! python3 -c "import keyring" 2>/dev/null; then
            missing_python_packages+=("keyring")
        fi
        
        if ! python3 -c "import psutil" 2>/dev/null; then
            missing_python_packages+=("psutil")
        fi
        
        if [ ${#missing_python_packages[@]} -ne 0 ]; then
            print_warning "Missing Python packages: ${missing_python_packages[*]}"
            print_status "Installing Python dependencies..."
            pip3 install -r "$requirements_file"
            print_success "Python dependencies installed"
        else
            print_success "All Python dependencies are already installed"
        fi
    fi
}

# Function to check dependencies
check_dependencies() {
    check_homebrew
    check_system_dependencies
    check_python_dependencies
}

# Function to set up environment variables
setup_environment() {
    print_status "Setting up environment variables..."
    
    # Get Homebrew prefix and Python version
    export BREW_PREFIX=$(brew --prefix)
    export PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    
    # Set up environment variables for PyGObject and GTK4
    export PATH="$BREW_PREFIX/bin:$PATH"
    export PYTHONPATH="$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages:$PYTHONPATH"
    export GI_TYPELIB_PATH="$BREW_PREFIX/lib/girepository-1.0"
    export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib:$DYLD_LIBRARY_PATH"
    export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"
    export XDG_DATA_DIRS="$BREW_PREFIX/share:$XDG_DATA_DIRS"
    
    print_success "Environment variables set"
}

# Function to test the setup
test_setup() {
    print_status "Testing setup..."
    
    # Test system dependencies
    print_status "Testing system dependencies..."
    if ! command_exists sshpass; then
        print_error "sshpass not found in PATH"
        exit 1
    fi
    
    # Test Python dependencies
    print_status "Testing Python dependencies..."
    python3 -c "
import sys
sys.path.insert(0, '$BREW_PREFIX/lib/python$PYTHON_VERSION/site-packages')
try:
    import gi
    gi.require_version('Adw', '1')
    gi.require_version('Gtk', '4.0')
    gi.require_version('Vte', '3.91')
    from gi.repository import Adw, Gtk, Vte
    import paramiko
    import cryptography
    import keyring
    import psutil
    print('✅ All components available!')
    print('   - PyGObject (gi)')
    print('   - libadwaita (Adw)')
    print('   - GTK4 (Gtk)')
    print('   - VTE (Vte)')
    print('   - paramiko')
    print('   - cryptography')
    print('   - keyring')
    print('   - psutil')
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
    
    # Check if run.py exists
    if [[ ! -f "$script_dir/run.py" ]]; then
        print_error "run.py not found in $script_dir"
        print_status "Please run this script from the sshpilot project directory"
        exit 1
    fi
    
    # Launch the application
    cd "$script_dir"
    python3 run.py
}

# Main execution
main() {
    echo "🚀 sshpilot macOS Launcher"
    echo "=========================="
    
    # Check and install dependencies
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
