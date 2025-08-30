#!/usr/bin/env python3
"""
Script to automatically update screenshots.js when new screenshots are added to docs/screenshots/
"""

import os
import re
from pathlib import Path

def get_screenshot_caption(filename):
    """Generate a caption based on the filename"""
    # Remove extension and replace hyphens/underscores with spaces
    name = os.path.splitext(filename)[0]
    name = name.replace('-', ' ').replace('_', ' ')
    
    # Capitalize first letter of each word
    name = ' '.join(word.capitalize() for word in name.split())
    
    return name

def update_screenshots_js():
    """Update the screenshots.js file with current screenshots"""
    screenshots_dir = Path('docs/screenshots')
    js_file = Path('docs/screenshots.js')
    
    if not screenshots_dir.exists():
        print(f"Error: {screenshots_dir} does not exist")
        return
    
    # Get all PNG files from screenshots directory
    png_files = [f.name for f in screenshots_dir.glob('*.png')]
    png_files.sort()  # Sort alphabetically
    
    if not png_files:
        print("No PNG files found in screenshots directory")
        return
    
    # Generate screenshot data
    screenshot_data = {}
    for filename in png_files:
        caption = get_screenshot_caption(filename)
        screenshot_data[filename] = caption
    
    # Read the current JS file
    if js_file.exists():
        with open(js_file, 'r', encoding='utf-8') as f:
            content = f.read()
    else:
        print(f"Error: {js_file} does not exist")
        return
    
    # Create the new screenshot data string
    data_lines = []
    for filename, caption in screenshot_data.items():
        data_lines.append(f"    '{filename}': '{caption}'")
    
    data_string = ',\n'.join(data_lines)
    
    # Replace the screenshotData object
    pattern = r'const screenshotData = \{[^}]*\};'
    replacement = f'const screenshotData = {{\n{data_string}\n}};'
    
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    
    # Write the updated content
    with open(js_file, 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print(f"Updated {js_file} with {len(png_files)} screenshots:")
    for filename in png_files:
        print(f"  - {filename}")

def main():
    """Main function"""
    print("Updating screenshots.js...")
    update_screenshots_js()
    print("Done!")

if __name__ == '__main__':
    main()
