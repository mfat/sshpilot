#!/bin/bash

# SSH Manager Launcher Script

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Change to the script directory
cd "$SCRIPT_DIR"

# Check if we're in a virtual environment
if [[ -n "$VIRTUAL_ENV" ]]; then
    echo "Virtual environment detected: $VIRTUAL_ENV"
    echo "Launching SSH Manager with clean system environment..."
    
    # Create a clean environment script
    cat > /tmp/run_ssh_manager_clean.sh << 'EOF'
#!/bin/bash
# Clean environment launcher
unset VIRTUAL_ENV
unset PYTHONPATH
unset PYTHONHOME
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
cd "$1"
exec python3 sshpilot.py
EOF
    
    chmod +x /tmp/run_ssh_manager_clean.sh
    exec /tmp/run_ssh_manager_clean.sh "$SCRIPT_DIR"
else
    echo "Using system Python for GTK/PyGObject compatibility..."
    # Run directly with system Python
    python3 sshpilot.py
fi 