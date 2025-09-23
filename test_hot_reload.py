#!/usr/bin/env python3
"""
Test script to demonstrate hot reloading functionality
This script will make a small change to a Python file to trigger hot reloading
"""

import time
import os
import sys

def test_hot_reload():
    """Test the hot reloading by making a small change to a Python file"""
    
    # File to modify
    test_file = "sshpilot/main.py"
    
    if not os.path.exists(test_file):
        print(f"❌ Test file {test_file} not found")
        return
    
    print("🧪 Testing hot reloading functionality...")
    print(f"   Modifying file: {test_file}")
    
    # Read the current file
    with open(test_file, 'r') as f:
        content = f.read()
    
    # Add a small comment to trigger hot reload
    if "# Hot reload test comment" not in content:
        # Find a good place to add the comment (after imports)
        lines = content.split('\n')
        insert_index = 0
        for i, line in enumerate(lines):
            if line.startswith('from gi.repository import'):
                insert_index = i + 1
                break
        
        # Insert the comment
        lines.insert(insert_index, "# Hot reload test comment - added by test script")
        new_content = '\n'.join(lines)
        
        # Write the modified content
        with open(test_file, 'w') as f:
            f.write(new_content)
        
        print("✅ File modified - hot reload should trigger in 2 seconds")
        print("   Watch the development script output for restart messages")
        
        # Wait a bit
        time.sleep(3)
        
        # Restore the original content
        with open(test_file, 'w') as f:
            f.write(content)
        
        print("✅ File restored to original state")
        print("   Hot reload should trigger again in 2 seconds")
        
    else:
        print("ℹ️  Test comment already exists, removing it...")
        
        # Remove the test comment
        lines = content.split('\n')
        lines = [line for line in lines if "# Hot reload test comment" not in line]
        new_content = '\n'.join(lines)
        
        with open(test_file, 'w') as f:
            f.write(new_content)
        
        print("✅ Test comment removed - hot reload should trigger in 2 seconds")

if __name__ == '__main__':
    test_hot_reload()
