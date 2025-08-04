#!/usr/bin/env python3
"""
Test script to verify SSH connection and dynamic port forwarding using system SSH client.
"""
import asyncio
import subprocess
import sys
import os
from typing import Optional, List, Tuple

def build_ssh_command(host: str, port: int, username: str, 
                    password: Optional[str] = None,
                    keyfile: Optional[str] = None,
                    key_passphrase: Optional[str] = None) -> List[str]:
    """Build the SSH command for system SSH client"""
    cmd = ['ssh']
    
    # Add key file if specified
    if keyfile and os.path.exists(keyfile):
        cmd.extend(['-i', keyfile])
        if key_passphrase:
            # Note: For passphrase-protected keys, you might need to use ssh-agent
            print("Warning: Passphrase-protected keys may require additional setup")
    
    # Add host and port
    if port != 22:
        cmd.extend(['-p', str(port)])
    
    # Add username if specified
    if username:
        cmd.append(f"{username}@{host}")
    else:
        cmd.append(host)
    
    return cmd

async def run_ssh_command(host: str, port: int, username: str, 
                         command: str,
                         password: Optional[str] = None,
                         keyfile: Optional[str] = None,
                         key_passphrase: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a command over SSH using system SSH client"""
    ssh_cmd = build_ssh_command(host, port, username, password, keyfile, key_passphrase)
    ssh_cmd.append(command)
    
    try:
        process = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode().strip(), stderr.decode().strip()
    except Exception as e:
        return -1, "", str(e)

async def test_ssh_connection(host: str, port: int, username: str, 
                            password: Optional[str] = None,
                            keyfile: Optional[str] = None,
                            key_passphrase: Optional[str] = None):
    """Test SSH connection and dynamic port forwarding using system SSH client"""
    print(f"Testing connection to {username}@{host}:{port}")
    
    # Test basic connection
    print("Attempting to establish SSH connection...")
    returncode, stdout, stderr = await run_ssh_command(
        host, port, username, 
        'echo "Hello from $(hostname)"',
        password, keyfile, key_passphrase
    )
    
    if returncode != 0:
        print(f"✗ SSH connection failed with code {returncode}")
        if stderr:
            print(f"Error: {stderr}")
        return
        
    print("✓ SSH connection established successfully!")
    print(f"Command output: {stdout}")
    
    # Test dynamic port forwarding (SOCKS proxy)
    print("\nTesting dynamic port forwarding (SOCKS proxy) on port 1080...")
    try:
        # Start SSH with dynamic port forwarding in the background
        ssh_cmd = build_ssh_command(host, port, username, password, keyfile, key_passphrase)
        ssh_cmd.extend(['-D', '1080', '-N', '-f'])  # -f for background, -N for no remote command
        
        process = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            print("✓ Dynamic port forwarding started successfully on port 1080")
            print("  You can now configure your applications to use SOCKS5 proxy at localhost:1080")
            print("  Press Ctrl+C to stop the proxy")
            
            # Keep the script running until interrupted
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                # Clean up the SSH process
                process.terminate()
                await process.wait()
        else:
            print(f"✗ Failed to start dynamic port forwarding: {stderr.decode().strip()}")
    except Exception as e:
        print(f"✗ Error setting up dynamic port forwarding: {e}")
    return True

def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Test SSH connection with AsyncSSH')
    parser.add_argument('host', help='SSH server hostname or IP')
    parser.add_argument('-p', '--port', type=int, default=22, help='SSH server port (default: 22)')
    parser.add_argument('-u', '--username', required=True, help='SSH username')
    parser.add_argument('-P', '--password', help='SSH password (not recommended, use key auth if possible)')
    parser.add_argument('-k', '--keyfile', help='Path to private key file')
    parser.add_argument('--key-passphrase', help='Passphrase for the private key')
    
    args = parser.parse_args()
    
    # Run the test
    try:
        asyncio.get_event_loop().run_until_complete(
            test_ssh_connection(
                args.host,
                args.port,
                args.username,
                args.password,
                args.keyfile,
                args.key_passphrase
            )
        )
    except KeyboardInterrupt:
        print("\nTest interrupted by user.")
        sys.exit(0)
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
