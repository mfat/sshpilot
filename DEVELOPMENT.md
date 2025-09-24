# sshPilot Development Guide

This guide explains how to set up and use the hot reloading development environment for sshPilot.

## 🔥 Hot Reloading Features

The development environment provides:

- **Python file watching**: Automatically restarts the application when Python files change
- **CSS/UI file watching**: Reloads styles and UI resources without full restart
- **Config file watching**: Restarts when configuration files change
- **Graceful restart**: Properly shuts down and restarts the application
- **Development mode detection**: Automatically enables development features

## 🚀 Quick Start

### 1. Install Development Dependencies

```bash
# Install watchdog for file monitoring
pip install watchdog

# Or install from dev requirements
pip install -r dev-requirements.txt
```

### 2. Start Development Mode

```bash
# Simple way - uses the dev.py script
python dev.py

# Advanced way - uses dev_runner.py directly
python dev_runner.py --verbose

# Run once without file watching
python dev_runner.py --no-watch
```

### 3. Development Workflow

1. Start the development mode
2. Make changes to Python files, CSS, or UI resources
3. The application will automatically restart or reload styles
4. See changes immediately without manual restart

## 📁 File Watching

The development watcher monitors:

### Python Files (Full Restart)
- `sshpilot/*.py` - All Python modules
- `run.py` - Main entry point
- `requirements.txt` - Dependencies

### UI Files (Full Restart)
- `sshpilot/resources/*.xml` - GResource files
- `sshpilot/resources/*.gresource` - Compiled resources
- `*.glade`, `*.ui` - UI definition files

### CSS Files (Style Reload)
- `*.css` - Stylesheet files
- `*.scss`, `*.sass` - Preprocessed stylesheets

### Config Files (Full Restart)
- `*.json`, `*.yaml`, `*.toml`, `*.ini` - Configuration files

## ⚙️ Configuration

### Environment Variables

```bash
# Enable development mode
export SSHPILOT_DEV_MODE=true

# Run with verbose logging
export SSHPILOT_VERBOSE=true
```

### Application Settings

You can also enable development mode through the application configuration:

```python
# In your config
config.set_setting('dev.hot_reload_enabled', True)
```

## 🛠️ Development Scripts

### `dev.py`
Simple entry point that:
- Checks for dependencies
- Offers to install missing packages
- Starts the development runner

### `dev_runner.py`
Advanced development runner with:
- File type detection
- Configurable restart delays
- CSS hot reloading support
- Verbose logging options

### `dev_watcher.py`
Basic file watcher (legacy):
- Simple file monitoring
- Basic restart functionality

## 🎨 CSS Hot Reloading

The application supports CSS hot reloading for development:

1. **Color Overrides**: Changes to color settings are reloaded without restart
2. **Style Providers**: CSS providers are updated in real-time
3. **Theme Changes**: Adwaita theme modifications are applied immediately

### Example CSS Development

```python
# In your development code
def test_css_changes():
    app = get_application()
    if hasattr(app, 'reload_css_styles'):
        app.reload_css_styles()
```

## 🐛 Troubleshooting

### Common Issues

1. **"watchdog not found"**
   ```bash
   pip install watchdog
   ```

2. **"run.py not found"**
   - Make sure you're in the sshPilot root directory
   - Check that `run.py` exists

3. **Application doesn't restart**
   - Check file permissions
   - Verify the file is being saved
   - Check the console for error messages

4. **CSS changes not applied**
   - Ensure development mode is enabled
   - Check that the CSS provider is properly registered
   - Verify the file is being watched

### Debug Mode

Run with verbose logging to see detailed information:

```bash
python dev_runner.py --verbose
```

### Manual Testing

Test the hot reloading manually:

```bash
# Start development mode
python dev.py

# In another terminal, modify a file
echo "# Test change" >> sshpilot/main.py

# The application should restart automatically
```

## 📝 Development Tips

1. **File Changes**: Make sure to save files completely - some editors don't trigger file system events on partial saves

2. **Restart Delays**: The system has built-in delays to prevent excessive restarts:
   - Python files: 1 second delay
   - CSS files: 0.5 second delay

3. **Ignored Files**: The watcher ignores:
   - `__pycache__` directories
   - `.pyc`, `.pyo` files
   - Hidden files (starting with `.`)
   - Build directories (`build`, `dist`, etc.)

4. **Performance**: File watching has minimal performance impact, but you can disable it with `--no-watch` if needed

## 🔧 Advanced Usage

### Custom File Patterns

You can modify the file watching patterns in `dev_runner.py`:

```python
# Add custom file extensions
self.css_extensions = {'.css', '.scss', '.sass', '.less'}

# Add custom directories to watch
watch_paths.append('/path/to/custom/directory')
```

### Integration with IDEs

Most IDEs can be configured to work with the development watcher:

- **VS Code**: Use the integrated terminal
- **PyCharm**: Run the dev script in the terminal
- **Vim/Neovim**: Use `:!python dev.py` or run in background

### CI/CD Integration

For automated testing, you can run without file watching:

```bash
python dev_runner.py --no-watch --verbose
```

## 📚 API Reference

### SshPilotDevRunner

Main development runner class.

```python
runner = SshPilotDevRunner(verbose=True, isolated=False)
runner.run()
```

### SshPilotDevHandler

File system event handler.

```python
handler = SshPilotDevHandler(restart_callback, css_reload_callback)
```

### Application CSS Reloading

```python
# In the main application
app.reload_css_styles()  # Reload CSS without restart
```

## 🤝 Contributing

When contributing to sshPilot:

1. Use the development mode for testing changes
2. Test both Python and CSS changes
3. Ensure the application restarts properly
4. Check that all file types are being watched correctly

## 📄 License

The development tools follow the same license as sshPilot (GPL-3.0).
