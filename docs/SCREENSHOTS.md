# Screenshots Management

This document explains how to add and manage screenshots for the sshPilot website.

## Adding New Screenshots

### 1. Add Screenshot Files
Simply copy your PNG screenshot files to the `docs/screenshots/` directory:

```bash
cp your-screenshot.png docs/screenshots/
```

### 2. Update the Website
Run the update script to automatically update the website:

```bash
python3 update_screenshots.py
```

This script will:
- Scan the `docs/screenshots/` directory for PNG files
- Generate captions based on filenames (e.g., `main-window.png` → `Main Window`)
- Update the `docs/screenshots.js` file automatically

### 3. Commit and Deploy
Commit your changes and push to deploy:

```bash
git add docs/screenshots/your-screenshot.png
git add docs/screenshots.js
git commit -m "Add new screenshot: your-screenshot.png"
git push origin main
```

## Screenshot Guidelines

### File Format
- **Format**: PNG only
- **Naming**: Use descriptive names with hyphens (e.g., `main-window.png`, `ssh-settings.png`)
- **Size**: Recommended 1200x800 pixels or similar 3:2 aspect ratio
- **Quality**: High quality, clear screenshots

### Caption Generation
The script automatically generates captions from filenames:
- `main-window.png` → "Main Window"
- `ssh-settings.png` → "Ssh Settings"
- `port-forwarding.png` → "Port Forwarding"

### Manual Caption Override
If you want custom captions, edit the `screenshotData` object in `docs/screenshots.js`:

```javascript
const screenshotData = {
    'main-window.png': 'Custom caption here',
    'ssh-settings.png': 'Another custom caption',
    // ... other screenshots
};
```

## Website Features

### Grid View
- Displays all screenshots in a responsive grid
- Click any screenshot to open it in slideshow mode
- Consistent aspect ratios (16:10) for uniform appearance

### Slideshow View
- Full-screen slideshow with navigation controls
- Auto-play functionality (3-second intervals)
- Keyboard navigation (arrow keys, spacebar)
- Click indicators to jump to specific screenshots

### Responsive Design
- Works on desktop, tablet, and mobile devices
- Optimized loading with lazy loading
- Dark/light theme support

## File Structure

```
docs/
├── screenshots/           # Screenshot PNG files
│   ├── main-window.png
│   ├── ssh-settings.png
│   └── ...
├── screenshots.js         # JavaScript for slideshow functionality
├── styles.css            # CSS styles for screenshots
└── index.html           # Main website with screenshot section
```

## Troubleshooting

### Screenshots Not Appearing
1. Ensure files are PNG format
2. Check file permissions
3. Run `python3 update_screenshots.py` to update the JavaScript

### Captions Not Updating
1. Edit the `screenshotData` object in `docs/screenshots.js`
2. Or rename the file to match the desired caption

### Slideshow Not Working
1. Check browser console for JavaScript errors
2. Ensure `screenshots.js` is properly loaded in `index.html`
3. Verify all screenshot files exist in the correct path
