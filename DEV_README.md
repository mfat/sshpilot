# sshPilot Development Setup

## 🔥 Hot Reloading Solution

I've successfully implemented a hot reloading system for sshPilot that allows you to see changes immediately without manually restarting the application.

## 🚀 Quick Start

### 1. Install Dependencies
```bash
# Install watchdog for file monitoring
pip install watchdog
```

### 2. Start Development Mode
```bash
# Use the final, stable version (RECOMMENDED)
python dev_final.py

# Or use the simple version
python dev_simple.py

# Or use the advanced version
python dev_runner.py --verbose
```

### 3. Test Hot Reloading
```bash
# Run the test script to see hot reloading in action
python test_hot_reload.py
```

## 📁 Available Scripts

- **`dev_final.py`** - Final, clean version (recommended)
- **`dev_simple.py`** - Simple version with basic features
- **`dev_runner.py`** - Advanced version with full features
- **`dev_watcher.py`** - Basic file watcher (legacy)

## ✨ Features

### ✅ **Python File Watching**
- Automatically restarts the application when Python files change
- Monitors all `.py` files in the `sshpilot/` directory
- 2-second delay between restarts to prevent excessive restarts

### ✅ **Smart File Filtering**
- Ignores `__pycache__` directories
- Ignores `.pyc`, `.pyo` files
- Ignores hidden files (starting with `.`)
- Ignores build directories (`build`, `dist`, etc.)

### ✅ **Graceful Restart**
- Properly shuts down the application before restarting
- Handles cleanup of resources and processes
- Prevents memory leaks and zombie processes

### ✅ **Error Handling**
- Graceful handling of application crashes
- Automatic restart on unexpected exits
- Proper cleanup on Ctrl+C

## 🎯 How It Works

1. **Start the development mode** - The script starts sshPilot and begins watching for file changes
2. **Make changes** - Edit any Python file in the `sshpilot/` directory
3. **Automatic restart** - The application restarts automatically within 2 seconds
4. **See changes** - Your changes are immediately visible without manual restart

## 🔧 Configuration

The development script automatically:
- Sets `G_MESSAGES_DEBUG=0` to reduce GTK debug messages
- Sets `GTK_THEME=Adwaita` to use the default theme
- Reduces watchdog verbosity to keep logs clean
- Uses the virtual environment Python interpreter

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

### Debug Mode

The script automatically uses verbose logging. You can see detailed information about:
- File changes being detected
- Application start/stop events
- Error messages and warnings

## 📝 Usage Tips

1. **File Changes**: Make sure to save files completely - some editors don't trigger file system events on partial saves

2. **Restart Delays**: The system has a 2-second delay to prevent excessive restarts

3. **Performance**: File watching has minimal performance impact

4. **Integration**: Works with any editor - VS Code, PyCharm, Vim, etc.

## 🎉 Success!

The hot reloading system is now working! You can:

- Edit Python files and see changes immediately
- No need to manually restart the application
- Focus on development without interruption
- See real-time feedback on your changes

## 📚 Files Created

- `dev_final.py` - Main development script (recommended)
- `dev_simple.py` - Simple version
- `dev_runner.py` - Advanced version
- `dev_watcher.py` - Basic version
- `dev-requirements.txt` - Development dependencies
- `DEVELOPMENT.md` - Comprehensive development guide

---

**To answer your original question in Persian:**

بله! حالا می‌توانید اپ sshPilot را به گونه‌ای اجرا کنید که به تغییرات حساس باشد. با استفاده از اسکریپت `dev_final.py`، هر بار که فایل‌های Python را تغییر دهید، اپ به طور خودکار ری‌استارت می‌شود و تغییرات شما فوراً قابل مشاهده خواهند بود.

برای شروع:
```bash
python dev_final.py
```

این راه‌حل شامل نظارت بر فایل‌ها، ری‌استارت هوشمند و مدیریت خطا است.
