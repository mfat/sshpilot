#!/usr/bin/env python3
"""
Create a simple DMG background image for sshPilot
"""

from PIL import Image, ImageDraw, ImageFont
import os

def create_dmg_background():
    # Create a 600x400 background image
    width, height = 600, 400
    
    # Create a gradient background (light blue to white)
    image = Image.new('RGB', (width, height), color='#f0f8ff')
    draw = ImageDraw.Draw(image)
    
    # Add a subtle gradient effect
    for y in range(height):
        # Create a subtle gradient from top to bottom
        r = int(240 + (y / height) * 15)  # 240 -> 255
        g = int(248 + (y / height) * 7)   # 248 -> 255
        b = int(255)                       # 255
        color = (r, g, b)
        draw.line([(0, y), (width, y)], fill=color)
    
    # Add a title
    try:
        # Try to use a system font
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
    except:
        # Fallback to default font
        font = ImageFont.load_default()
    
    # Add title text
    title = "sshPilot"
    title_bbox = draw.textbbox((0, 0), title, font=font)
    title_width = title_bbox[2] - title_bbox[0]
    title_x = (width - title_width) // 2
    title_y = 50
    
    # Draw title with shadow
    draw.text((title_x + 2, title_y + 2), title, fill='#666666', font=font)
    draw.text((title_x, title_y), title, fill='#2c3e50', font=font)
    
    # Add subtitle
    subtitle = "SSH Client for macOS"
    subtitle_bbox = draw.textbbox((0, 0), subtitle, font=font)
    subtitle_width = subtitle_bbox[2] - subtitle_bbox[0]
    subtitle_x = (width - subtitle_width) // 2
    subtitle_y = title_y + 40
    
    draw.text((subtitle_x + 1, subtitle_y + 1), subtitle, fill='#666666', font=font)
    draw.text((subtitle_x, subtitle_y), subtitle, fill='#34495e', font=font)
    
    # Add instructions
    instructions = "Drag sshPilot to Applications to install"
    try:
        small_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    except:
        small_font = ImageFont.load_default()
    
    inst_bbox = draw.textbbox((0, 0), instructions, small_font)
    inst_width = inst_bbox[2] - inst_bbox[0]
    inst_x = (width - inst_width) // 2
    inst_y = height - 80
    
    draw.text((inst_x + 1, inst_y + 1), instructions, fill='#666666', font=small_font)
    draw.text((inst_x, inst_y), instructions, fill='#7f8c8d', font=small_font)
    
    # Save the image
    output_path = os.path.join(os.path.dirname(__file__), 'dmg-background.png')
    image.save(output_path, 'PNG')
    print(f"Created DMG background: {output_path}")

if __name__ == "__main__":
    create_dmg_background()
