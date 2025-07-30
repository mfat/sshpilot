#!/usr/bin/env python3
"""
SSH Manager Demo Script
This script demonstrates how to use the SSH Manager programmatically
"""

import sys
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_manager import SSHManager, SSHConnection

def demo_connection_management():
    """Demonstrate connection management features"""
    print("SSH Manager Demo")
    print("=" * 50)
    
    # Create SSH Manager instance
    manager = SSHManager()
    
    # Create some sample connections
    connections = [
        SSHConnection(
            hostname="example.com",
            username="user1",
            port=22,
            name="Example Server"
        ),
        SSHConnection(
            hostname="192.168.1.100",
            username="admin",
            port=2222,
            name="Local Server"
        ),
        SSHConnection(
            hostname="test.example.org",
            username="testuser",
            key_path="~/.ssh/id_rsa",
            name="Test Server"
        )
    ]
    
    # Add connections to manager
    for conn in connections:
        manager.connections.append(conn)
        print(f"Added connection: {conn.name} ({conn.username}@{conn.hostname}:{conn.port})")
    
    # Save connections
    manager.save_connections()
    print(f"\nConnections saved to: {manager.config_file}")
    
    # Detect SSH keys
    keys = manager.detect_ssh_keys()
    print(f"\nDetected SSH keys: {keys}")
    
    # Show connection list
    print("\nCurrent connections:")
    for i, conn in enumerate(manager.connections, 1):
        status = "Connected" if conn.hostname in manager.connected_connections else "Disconnected"
        print(f"  {i}. {conn.name} - {conn.username}@{conn.hostname}:{conn.port} ({status})")
    
    print("\nDemo completed! Run 'python3 ssh_manager.py' to use the GUI.")

if __name__ == "__main__":
    demo_connection_management() 