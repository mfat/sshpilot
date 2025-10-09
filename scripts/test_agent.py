#!/usr/bin/env python3
"""
Test script for sshpilot-agent

This script tests the agent in isolation to verify it can:
1. Create PTY with proper flags
2. Discover user shell
3. Spawn shell with job control
4. Handle I/O correctly

Usage:
    python3 scripts/test_agent.py [--verbose]
"""

import sys
import os
import time
import select
import subprocess
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sshpilot.sshpilot_agent import PTYAgent


def test_shell_discovery():
    """Test shell discovery"""
    print("Testing shell discovery...")
    agent = PTYAgent()
    shell = agent.discover_shell()
    print(f"  ✓ Discovered shell: {shell}")
    assert os.path.isfile(shell), f"Shell {shell} does not exist"
    return True


def test_pty_creation():
    """Test PTY creation"""
    print("Testing PTY creation...")
    agent = PTYAgent()
    master_fd, slave_fd = agent.create_pty()
    print(f"  ✓ Created PTY: master_fd={master_fd}, slave_fd={slave_fd}")
    assert master_fd > 0, "Invalid master FD"
    assert slave_fd > 0, "Invalid slave FD"
    agent.cleanup()
    return True


def test_agent_interactive():
    """Test agent in interactive mode"""
    print("\nTesting agent in interactive mode...")
    print("This will spawn a shell via the agent.")
    print("Type 'echo test' and press Enter, then 'exit' to quit.\n")
    
    agent = PTYAgent()
    
    try:
        # Run the agent
        agent.run(rows=24, cols=80)
        print("\n  ✓ Agent exited cleanly")
        return True
    except KeyboardInterrupt:
        print("\n  ✓ Agent interrupted by user")
        return True
    except Exception as e:
        print(f"\n  ✗ Agent failed: {e}")
        return False


def test_agent_non_interactive():
    """Test agent by sending commands non-interactively"""
    print("Testing agent with automated commands...")
    
    # Build command to run agent
    agent_path = project_root / 'sshpilot' / 'sshpilot_agent.py'
    cmd = [sys.executable, str(agent_path), '--rows', '24', '--cols', '80']
    
    print(f"  Running: {' '.join(cmd)}")
    
    try:
        # Start agent
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False
        )
        
        # Wait a bit for agent to start
        time.sleep(0.5)
        
        # Send a simple command
        test_cmd = b"echo 'Agent test successful'\n"
        process.stdin.write(test_cmd)
        process.stdin.flush()
        
        # Read output for a short time
        start_time = time.time()
        output = b""
        
        while time.time() - start_time < 2:
            readable, _, _ = select.select([process.stdout], [], [], 0.1)
            if process.stdout in readable:
                chunk = process.stdout.read(1024)
                if chunk:
                    output += chunk
                    # Check if we got our expected output
                    if b"Agent test successful" in output:
                        print("  ✓ Agent executed command successfully")
                        break
        
        # Send exit command
        process.stdin.write(b"exit\n")
        process.stdin.flush()
        
        # Wait for process to exit
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=1)
        
        if b"Agent test successful" in output:
            print("  ✓ Agent test passed")
            return True
        else:
            print(f"  ✗ Did not receive expected output")
            print(f"  Output: {output[:200]}")
            return False
            
    except Exception as e:
        print(f"  ✗ Agent test failed: {e}")
        return False


def test_flatpak_spawn():
    """Test that agent can be launched via flatpak-spawn if in Flatpak"""
    print("Testing Flatpak integration...")
    
    # Check if we're in Flatpak
    is_flatpak = os.path.exists('/.flatpak-info')
    
    if not is_flatpak:
        print("  ⊘ Not in Flatpak, skipping")
        return True
    
    # Check for flatpak-spawn
    import shutil
    flatpak_spawn = shutil.which('flatpak-spawn')
    
    if not flatpak_spawn:
        print("  ✗ flatpak-spawn not found")
        return False
    
    print(f"  ✓ Found flatpak-spawn: {flatpak_spawn}")
    
    # Try to run a simple command on host
    try:
        result = subprocess.run(
            [flatpak_spawn, '--host', 'echo', 'test'],
            capture_output=True,
            text=True,
            check=True
        )
        if result.stdout.strip() == 'test':
            print("  ✓ flatpak-spawn works correctly")
            return True
        else:
            print(f"  ✗ Unexpected output: {result.stdout}")
            return False
    except Exception as e:
        print(f"  ✗ flatpak-spawn test failed: {e}")
        return False


def main():
    """Run all tests"""
    print("=" * 60)
    print("SSHPilot Agent Test Suite")
    print("=" * 60)
    print()
    
    tests = [
        ("Shell Discovery", test_shell_discovery),
        ("PTY Creation", test_pty_creation),
        ("Flatpak Integration", test_flatpak_spawn),
        ("Automated Commands", test_agent_non_interactive),
    ]
    
    results = []
    
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"  ✗ Test failed with exception: {e}")
            results.append((name, False))
        print()
    
    # Print summary
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status:8} {name}")
    
    print()
    
    passed = sum(1 for _, p in results if p)
    total = len(results)
    
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("\n✓ All tests passed!")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed")
        return 1


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Test SSHPilot Agent')
    parser.add_argument('--verbose', action='store_true', help='Verbose output')
    parser.add_argument('--interactive', action='store_true', 
                       help='Run interactive test (opens a shell)')
    
    args = parser.parse_args()
    
    if args.interactive:
        # Run interactive test
        test_agent_interactive()
        sys.exit(0)
    else:
        # Run automated tests
        sys.exit(main())

