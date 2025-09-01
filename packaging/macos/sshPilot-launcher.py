#!/usr/bin/env python3
import sys
import os

# Get the path to the Resources directory
script_dir = os.path.dirname(os.path.abspath(__file__))
resources_dir = os.path.join(script_dir, '..', 'Resources')

# Add the Resources directory to Python path so we can find run.py
sys.path.insert(0, resources_dir)

# Also add the current directory (MacOS) to Python path
sys.path.insert(0, script_dir)

# Run the application
from run import main
if __name__ == "__main__":
    main()
