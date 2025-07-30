#!/bin/bash

# SSH Manager Installation Script
# This script installs the required system dependencies for SSH Manager

set -e

echo "SSH Manager - Installation Script"
echo "=================================="

# Detect the operating system
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$NAME
    VER=$VERSION_ID
else
    echo "Error: Cannot detect operating system"
    exit 1
fi

echo "Detected OS: $OS $VER"
echo ""

# Function to install dependencies based on OS
install_dependencies() {
    case $OS in
        "Ubuntu"|"Debian GNU/Linux")
            echo "Installing dependencies for Ubuntu/Debian..."
            sudo apt update
            sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-vte-3.91 libgirepository1.0-dev python3-paramiko python3-cryptography
            ;;
        "Fedora")
            echo "Installing dependencies for Fedora..."
            sudo dnf install -y python3-gobject gtk4 vte291 python3-paramiko python3-cryptography
            ;;
        "Arch Linux")
            echo "Installing dependencies for Arch Linux..."
            sudo pacman -S --noconfirm python-gobject gtk4 vte3 python-paramiko python-cryptography
            ;;
        "openSUSE Tumbleweed"|"openSUSE Leap")
            echo "Installing dependencies for openSUSE..."
            sudo zypper install -y python3-gobject gtk4 vte3 python3-paramiko python3-cryptography
            ;;
        *)
            echo "Unsupported operating system: $OS"
            echo "Please install the following packages manually:"
            echo "- python3-gobject or python-gobject"
            echo "- gtk4"
            echo "- vte3 or vte291"
            echo "- python3-paramiko"
            echo "- python3-cryptography"
            echo ""
            echo "Then run: python3 ssh_manager.py"
            exit 1
            ;;
    esac
}

# Function to make the script executable
make_executable() {
    echo "Making SSH Manager executable..."
    chmod +x ssh_manager.py
}

# Function to create desktop shortcut
create_desktop_shortcut() {
    echo "Creating desktop shortcut..."
    
    # Create desktop entry
    cat > ssh-manager.desktop << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=SSH Manager
Comment=GTK-based SSH client with terminal integration
Exec=$(pwd)/ssh_manager.py
Icon=utilities-terminal
Terminal=false
Categories=Network;TerminalEmulator;
EOF

    # Install desktop file
    if [ -d "$HOME/.local/share/applications" ]; then
        cp ssh-manager.desktop "$HOME/.local/share/applications/"
        echo "Desktop shortcut created in ~/.local/share/applications/"
    else
        echo "Could not create desktop shortcut. You can run the application with:"
        echo "python3 ssh_manager.py"
    fi
}

# Function to run installation test
run_test() {
    echo "Running installation test..."
    if python3 test_installation.py; then
        echo "✓ Installation test passed!"
    else
        echo "✗ Installation test failed. Please check the installation."
        exit 1
    fi
}

# Main installation process
main() {
    echo "Starting installation..."
    echo ""
    
    # Check if running as root
    if [ "$EUID" -eq 0 ]; then
        echo "Error: Please do not run this script as root"
        exit 1
    fi
    
    # Install system dependencies
    install_dependencies
    
    # Make script executable
    make_executable
    
    # Create desktop shortcut
    create_desktop_shortcut
    
    # Run installation test
    run_test
    
    echo ""
    echo "Installation completed successfully!"
    echo ""
    echo "You can now run SSH Manager with:"
    echo "python3 ssh_manager.py"
    echo ""
    echo "Or find it in your applications menu as 'SSH Manager'"
    echo ""
    echo "Configuration will be saved to: ~/.ssh_manager_config.json"
}

# Run main function
main "$@" 