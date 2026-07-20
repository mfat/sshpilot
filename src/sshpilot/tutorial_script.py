#!/usr/bin/env python3
"""Standalone tutorial script that runs in a terminal."""

import sys
import time

# ANSI color codes
RESET = '\033[0m'
BOLD = '\033[1m'
RED = '\033[31m'
GREEN = '\033[32m'
YELLOW = '\033[33m'
BLUE = '\033[34m'
CYAN = '\033[36m'
BRIGHT_RED = '\033[91m'
BRIGHT_GREEN = '\033[92m'
BRIGHT_YELLOW = '\033[93m'
BRIGHT_BLUE = '\033[94m'
BRIGHT_CYAN = '\033[96m'
BRIGHT_WHITE = '\033[97m'

def colorize(text, color):
    """Add ANSI color to text."""
    return f"{color}{text}{RESET}"

def type_text(text, color=None, delay=0.03):
    """Print text with typing animation."""
    if color:
        text = colorize(text, color)
    
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write('\n')
    sys.stdout.flush()

def clear_screen():
    """Clear terminal screen."""
    sys.stdout.write('\033[2J\033[H')
    sys.stdout.flush()

def print_header(title):
    """Print styled header."""
    print()
    border = '=' * 70
    print(colorize(border, CYAN))
    print(colorize(f"  {title}", BRIGHT_CYAN))
    print(colorize(border, CYAN))
    print()

def print_section(title):
    """Print section header."""
    print()
    print(colorize(f"▶ {title}", BRIGHT_YELLOW))
    print()

def print_code(code, description=None):
    """Print code example."""
    if description:
        print(colorize(f"  {description}:", BOLD))
    print(colorize(f"  $ {code}", BRIGHT_GREEN))
    print()

def print_info(text):
    """Print info message."""
    print(colorize(f"  ℹ {text}", BLUE))

def print_success(text):
    """Print success message."""
    print(colorize(f"  ✓ {text}", GREEN))

def print_tip(text):
    """Print tip."""
    print(colorize(f"  💡 {text}", BRIGHT_CYAN))

def wait(seconds):
    """Wait for specified seconds."""
    time.sleep(seconds)

def main():
    """Run the tutorial."""
    clear_screen()
    
    # Welcome
    print_header("SSH Pilot Tutorial")
    print()
    type_text("Welcome to SSH Pilot! This interactive tutorial will guide you", BRIGHT_WHITE, 0.03)
    wait(0.5)
    type_text("through the key features and best practices for SSH connections.", BRIGHT_WHITE, 0.03)
    wait(1.5)
    
    # Introduction
    print_section("What is SSH?")
    type_text("SSH (Secure Shell) is a protocol for securely accessing remote systems.", BRIGHT_WHITE, 0.03)
    wait(0.5)
    type_text("SSH Pilot makes it easy to manage multiple SSH connections.", BRIGHT_WHITE, 0.03)
    wait(1.5)
    
    # Features
    print_section("Key Features")
    features = [
        ("Connection Management", "Organize and quickly access your servers"),
        ("Tabbed Interface", "Work with multiple connections simultaneously"),
        ("File Management", "Browse and transfer files via SFTP"),
        ("Key Management", "Secure authentication with SSH keys"),
        ("Port Forwarding", "Tunnel traffic through SSH connections"),
        ("Groups", "Organize connections by project or environment"),
    ]
    
    for name, desc in features:
        type_text(f"  {name}:", BRIGHT_CYAN, 0.02)
        wait(0.2)
        type_text(f"    {desc}", BRIGHT_WHITE, 0.02)
        wait(0.5)
    
    wait(1.5)
    
    # Connection Basics
    print_section("Creating Your First Connection")
    type_text("To connect to a server, you need:", BRIGHT_WHITE, 0.03)
    wait(0.5)
    type_text("  • Hostname or IP address", BRIGHT_WHITE, 0.02)
    wait(0.3)
    type_text("  • Username (optional, defaults to your local username)", BRIGHT_WHITE, 0.02)
    wait(0.3)
    type_text("  • Port (optional, defaults to 22)", BRIGHT_WHITE, 0.02)
    wait(1.5)
    
    # SSH Examples
    print_section("SSH Command Examples")
    examples = [
        ("Basic connection", "ssh user@hostname"),
        ("With custom port", "ssh -p 2222 user@hostname"),
        ("Using SSH key", "ssh -i ~/.ssh/id_rsa user@hostname"),
        ("With options", "ssh -o StrictHostKeyChecking=no user@hostname"),
    ]
    
    for desc, cmd in examples:
        print_code(cmd, desc)
        wait(0.8)
    
    wait(1.5)
    
    # Key Management
    print_section("SSH Key Management")
    type_text("SSH keys provide secure, passwordless authentication.", BRIGHT_WHITE, 0.03)
    wait(0.5)
    type_text("Generate a new key pair:", BRIGHT_YELLOW, 0.03)
    wait(0.3)
    print_code("ssh-keygen -t ed25519 -C 'your_email@example.com'", "Generate Ed25519 key (recommended)")
    wait(1.0)
    type_text("Copy your public key to the server:", BRIGHT_YELLOW, 0.03)
    wait(0.3)
    print_code("ssh-copy-id user@hostname", "Copy public key to server")
    wait(1.0)
    type_text("Or use SSH Pilot's built-in key manager!", BRIGHT_GREEN, 0.03)
    wait(1.5)
    
    # Port Forwarding
    print_section("Port Forwarding")
    type_text("Port forwarding allows you to access services on remote servers.", BRIGHT_WHITE, 0.03)
    wait(0.5)
    type_text("Types of port forwarding:", BRIGHT_YELLOW, 0.03)
    wait(0.5)
    type_text("  • Local: Forward local port to remote server", BRIGHT_WHITE, 0.02)
    wait(0.3)
    type_text("  • Remote: Forward remote port to local machine", BRIGHT_WHITE, 0.02)
    wait(0.3)
    type_text("  • Dynamic: SOCKS proxy through SSH tunnel", BRIGHT_WHITE, 0.02)
    wait(1.0)
    print_code("ssh -L 8080:localhost:80 user@hostname", "Local port forwarding example")
    wait(1.5)
    
    # Tips
    print_section("Tips & Best Practices")
    tips = [
        "Use SSH keys instead of passwords for better security",
        "Organize connections into groups by project or environment",
        "Use connection aliases for easy identification",
        "Enable port forwarding for accessing remote services",
        "Use the file manager for quick file transfers",
        "Take advantage of keyboard shortcuts for faster navigation",
    ]
    
    for tip in tips:
        print_tip(tip)
        wait(0.6)
    
    wait(1.5)
    
    # Shortcuts
    print_section("Keyboard Shortcuts")
    shortcuts = [
        ("Ctrl+N", "New connection"),
        ("Ctrl+T", "New tab"),
        ("Ctrl+W", "Close tab"),
        ("Ctrl+Tab", "Next tab"),
        ("Ctrl+Shift+Tab", "Previous tab"),
        ("F11", "Toggle fullscreen"),
        ("Ctrl+F", "Search in terminal"),
    ]
    
    for key, desc in shortcuts:
        print(f"  {colorize(key, BRIGHT_GREEN)}: {desc}")
        wait(0.4)
    
    wait(1.5)
    
    # Conclusion
    print_section("You're All Set!")
    type_text("You now know the basics of SSH Pilot!", BRIGHT_GREEN, 0.03)
    print()
    wait(0.5)
    type_text("Ready to connect? Press Ctrl+N to create a new connection.", BRIGHT_CYAN, 0.03)
    print()
    wait(0.5)
    type_text("Happy connecting! 🚀", BRIGHT_YELLOW, 0.03)
    print()
    wait(1.0)
    
    # Show prompt
    print()
    print(colorize("tutorial@sshpilot:~$ ", BRIGHT_GREEN), end='', flush=True)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nTutorial interrupted.")
        sys.exit(0)
    except Exception as e:
        print(f"\n\nError running tutorial: {e}")
        sys.exit(1)
