#!/usr/bin/env python3
"""
Test script to verify AsyncSSH connection and dynamic port forwarding.
"""
import asyncio
import asyncssh
import sys
import os
from typing import Optional, Dict, Any

async def test_ssh_connection(host: str, port: int, username: str, 
                            password: Optional[str] = None,
                            keyfile: Optional[str] = None,
                            key_passphrase: Optional[str] = None):
    """Test SSH connection and dynamic port forwarding"""
    print(f"Testing connection to {username}@{host}:{port}")
    
    # Connection options
    conn_kwargs: Dict[str, Any] = {
        'host': host,
        'port': port,
        'username': username,
        'known_hosts': None,  # Disable known hosts check for testing
        'encoding': 'utf-8',
    }
    
    # Add authentication method
    if keyfile and os.path.exists(keyfile):
        conn_kwargs['client_keys'] = [keyfile]
        if key_passphrase:
            conn_kwargs['passphrase'] = key_passphrase
    elif password:
        conn_kwargs['password'] = password
    else:
        # Try using the default SSH agent
        conn_kwargs['agent_path'] = os.environ.get('SSH_AUTH_SOCK')
    
    try:
        # Test basic connection
        print("Attempting to establish SSH connection...")
        async with asyncssh.connect(**conn_kwargs) as conn:
            print("✓ SSH connection established successfully!")
            
            # Test command execution
            print("\nTesting command execution...")
            result = await conn.run('echo "Hello from $(hostname)"')
            print(f"Command output: {result.stdout.strip()}")
            
            # Test dynamic port forwarding (SOCKS proxy)
            print("\nTesting dynamic port forwarding (SOCKS proxy) on port 1080...")
            try:
                # Start a SOCKS proxy on localhost:1080
                listen_port = 1080
                server = await conn.forward_socks(
                    '',  # Listen on all interfaces
                    listen_port
                )
                print(f"✓ SOCKS proxy started on port {listen_port}")
                print("You can now configure your applications to use this SOCKS proxy.")
                print("Press Ctrl+C to stop the proxy and exit.")
                
                # Keep the connection alive
                while True:
                    await asyncio.sleep(1)
                    
            except asyncio.CancelledError:
                print("\nStopping SOCKS proxy...")
                server.close()
                await server.wait_closed()
                print("SOCKS proxy stopped.")
            except Exception as e:
                print(f"Error with SOCKS proxy: {e}")
                raise
                
    except Exception as e:
        print(f"Error: {e}")
        return False
    
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
