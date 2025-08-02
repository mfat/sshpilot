# sshPilot Developer Guide

## Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Development Setup](#development-setup)
4. [Code Organization](#code-organization)
5. [Core Components](#core-components)
6. [UI Development](#ui-development)
7. [Building and Packaging](#building-and-packaging)
8. [Testing](#testing)
9. [Contributing](#contributing)
10. [API Documentation](#api-documentation)

## Project Overview

sshPilot is a modern SSH connection manager built with Python, GTK4, and libadwaita. It follows GNOME Human Interface Guidelines and provides a comprehensive SSH management solution.

### Technology Stack

- **Language**: Python 3.10+
- **GUI Framework**: GTK4 + libadwaita
- **Terminal**: VTE (Virtual Terminal Emulator)
- **SSH**: Paramiko library
- **Monitoring**: Matplotlib for charts
- **Security**: SecretStorage for password management
- **Build System**: Meson + setuptools
- **Packaging**: Flatpak, DEB, RPM

### Key Features

- Tabbed terminal interface
- SSH connection management
- Secure password storage
- SSH key generation and deployment
- Port forwarding (tunneling)
- Resource monitoring
- Theme customization

## Architecture

### Design Principles

1. **Separation of Concerns**: Clear separation between UI and business logic
2. **Signal-Based Communication**: Loose coupling using GObject signals
3. **Async Operations**: Non-blocking SSH operations using threading
4. **Security First**: Secure password storage and key management
5. **Extensible Design**: Plugin-ready architecture

### Component Overview

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   MainWindow    │    │ ConnectionManager│    │  TerminalWidget │
│   (UI Layer)    │◄──►│ (Business Logic) │◄──►│  (Terminal)     │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                        │                        │
         ▼                        ▼                        ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│     Config      │    │   KeyManager     │    │ ResourceMonitor │
│  (Settings)     │    │  (SSH Keys)      │    │  (Monitoring)   │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

### Signal Flow

```
User Action → MainWindow → ConnectionManager → SSH Connection
     ↓              ↓              ↓                    ↓
UI Update ← Signal Emission ← Status Change ← Connection Event
```

## Development Setup

### Prerequisites

#### System Dependencies

**Debian/Ubuntu:**
```bash
sudo apt install python3-dev python3-pip python3-venv \
    libgirepository1.0-dev libgtk-4-dev libadwaita-1-dev \
    libvte-2.91-gtk4-dev gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-vte-2.91-gtk4 meson ninja-build git
```

**Fedora:**
```bash
sudo dnf install python3-devel python3-pip \
    gobject-introspection-devel gtk4-devel libadwaita-devel \
    vte291-gtk4-devel meson ninja-build git
```

#### Python Environment

```bash
# Clone repository
git clone https://github.com/mfat/sshpilot.git
cd sshpilot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -e .

# Install development dependencies
pip install pytest pytest-cov black flake8 mypy sphinx
```

### Running from Source

```bash
# Activate environment
source venv/bin/activate

# Run application
python -m io.github.mfat.sshpilot.main

# Or with debug logging
SSHPILOT_DEBUG=1 python -m io.github.mfat.sshpilot.main
```

### Development Tools

#### Code Formatting
```bash
black src/
```

#### Linting
```bash
flake8 src/
```

#### Type Checking
```bash
mypy src/ --ignore-missing-imports
```

#### Testing
```bash
pytest tests/ --cov=src/
```

## Code Organization

### Directory Structure

```
sshPilot/
├── src/io.github.mfat.sshpilot/    # Main Python package
│   ├── __init__.py                  # Package initialization
│   ├── main.py                      # Application entry point
│   ├── window.py                    # Main window implementation
│   ├── connection_manager.py        # SSH connection handling
│   ├── terminal.py                  # Terminal widget
│   ├── config.py                    # Configuration management
│   ├── resource_monitor.py          # System monitoring
│   ├── key_manager.py               # SSH key management
│   ├── ui/                          # GTK UI definitions
│   │   ├── main_window.ui
│   │   ├── connection_dialog.ui
│   │   ├── preferences.ui
│   │   ├── key_dialog.ui
│   │   ├── welcome.ui
│   │   └── resource_view.ui
│   └── resources/                   # Icons and resources
│       ├── sshpilot.gresource.xml
│       └── sshpilot.png
├── data/                            # Desktop integration files
│   ├── io.github.mfat.sshpilot.desktop
│   ├── io.github.mfat.sshpilot.appdata.xml
│   ├── io.github.mfat.sshpilot.gschema.xml
│   ├── io.github.mfat.sshpilot.json    # Flatpak manifest
│   └── sshpilot.in                     # Launcher script template
├── debian/                          # Debian packaging
├── docs/                            # Documentation
├── tests/                           # Unit tests
├── .github/workflows/               # CI/CD workflows
├── requirements.txt                 # Python dependencies
├── setup.py                         # Python packaging
├── meson.build                      # Meson build system
└── sshPilot.spec                   # RPM packaging
```

### Naming Conventions

- **Classes**: PascalCase (`MainWindow`, `ConnectionManager`)
- **Functions/Methods**: snake_case (`connect_to_host`, `on_button_clicked`)
- **Constants**: UPPER_SNAKE_CASE (`DEFAULT_PORT`, `SSH_CONFIG_PATH`)
- **Files**: snake_case (`connection_manager.py`, `main_window.ui`)
- **Signals**: kebab-case (`connection-added`, `status-changed`)

## Core Components

### MainWindow (`window.py`)

Primary application window handling UI layout and user interactions.

**Key Responsibilities:**
- UI layout management (sidebar, tabs, toolbar)
- Connection list display and management
- Tab creation and management
- Menu and toolbar handling
- Keyboard shortcut processing

**Important Methods:**
```python
def setup_ui(self):
    """Initialize UI components"""

def show_connection_dialog(self, connection=None):
    """Show connection add/edit dialog"""

def connect_to_host(self, connection):
    """Create terminal tab and connect to SSH host"""

def on_connection_activated(self, list_box, row):
    """Handle connection list activation"""
```

### ConnectionManager (`connection_manager.py`)

Manages SSH connections, configuration, and authentication.

**Key Responsibilities:**
- SSH configuration file parsing
- Connection establishment and management
- Password storage/retrieval via system keyring
- SSH agent integration
- Connection state tracking

**Important Methods:**
```python
def load_ssh_config(self):
    """Load connections from ~/.ssh/config"""

def save_connection(self, connection_data):
    """Save connection to SSH config"""

def connect(self, connection):
    """Establish SSH connection"""

def store_password(self, host, username, password):
    """Store password in system keyring"""
```

**Signals:**
- `connection-added(connection)`: New connection added
- `connection-removed(connection)`: Connection deleted
- `connection-status-changed(connection, is_connected)`: Status change

### TerminalWidget (`terminal.py`)

VTE-based terminal widget with SSH integration.

**Key Responsibilities:**
- Terminal emulation using VTE
- SSH connection handling
- Terminal theming and customization
- Copy/paste operations
- Terminal title management

**Important Methods:**
```python
def connect_ssh(self):
    """Establish SSH connection in terminal"""

def apply_theme(self, theme_name=None):
    """Apply terminal color theme"""

def disconnect(self):
    """Disconnect SSH session"""
```

**Signals:**
- `connection-established()`: SSH connection successful
- `connection-lost()`: SSH connection terminated
- `title-changed(title)`: Terminal title updated

### Config (`config.py`)

Configuration management with GSettings and JSON backends.

**Key Responsibilities:**
- Settings storage and retrieval
- Terminal theme management
- Window geometry persistence
- Preference synchronization

**Important Methods:**
```python
def get_setting(self, key, default=None):
    """Get configuration value"""

def set_setting(self, key, value):
    """Set configuration value"""

def get_terminal_profile(self, theme_name=None):
    """Get terminal theme configuration"""
```

### ResourceMonitor (`resource_monitor.py`)

System resource monitoring with real-time charts.

**Key Responsibilities:**
- Remote system monitoring via SSH
- Data collection and storage
- Chart generation with matplotlib
- Monitoring thread management

**Important Methods:**
```python
def start_monitoring(self, connection):
    """Start monitoring session"""

def fetch_system_info(self, connection):
    """Fetch system statistics via SSH"""

def parse_system_info(self, results):
    """Parse command output into metrics"""
```

### KeyManager (`key_manager.py`)

SSH key generation, management, and deployment.

**Key Responsibilities:**
- SSH key generation (RSA, Ed25519)
- Key file management
- Public key deployment
- SSH agent integration

**Important Methods:**
```python
def generate_key(self, key_name, key_type, key_size, comment, passphrase):
    """Generate new SSH key pair"""

def deploy_key_to_host(self, ssh_key, connection):
    """Deploy public key to remote host"""

def discover_keys(self):
    """Find existing SSH keys"""
```

## UI Development

### GTK4 and libadwaita

sshPilot uses modern GTK4 with libadwaita for native GNOME integration.

#### Key Widgets Used
- `AdwApplicationWindow`: Main window
- `AdwOverlaySplitView`: Sidebar layout
- `AdwTabView`/`AdwTabBar`: Tab interface
- `AdwPreferencesWindow`: Settings dialog
- `AdwToastOverlay`: Notifications
- `GtkListBox`: Connection list
- `VteTerminal`: Terminal emulator

#### UI File Loading
```python
@Gtk.Template(resource_path='/io/github/mfat/sshpilot/ui/main_window.ui')
class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'MainWindow'
    
    # Template children
    connection_list = Gtk.Template.Child()
    tab_view = Gtk.Template.Child()
```

### Signal Handling

#### GObject Signals
```python
class ConnectionManager(GObject.Object):
    __gsignals__ = {
        'connection-added': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-removed': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }
    
    def emit_connection_added(self, connection):
        self.emit('connection-added', connection)
```

#### Signal Connections
```python
def setup_signals(self):
    self.connection_manager.connect('connection-added', self.on_connection_added)
    self.connection_manager.connect('connection-removed', self.on_connection_removed)
```

### Threading Considerations

SSH operations run in separate threads to avoid blocking the UI:

```python
def connect_ssh(self):
    thread = threading.Thread(target=self._connect_ssh_thread)
    thread.daemon = True
    thread.start()

def _connect_ssh_thread(self):
    # SSH connection logic
    # Use GLib.idle_add() for UI updates
    GLib.idle_add(self._on_connection_established)
```

## Building and Packaging

### Meson Build System

Primary build system for native packaging:

```bash
# Configure build
meson setup build

# Compile
meson compile -C build

# Install
meson install -C build

# Run tests
meson test -C build
```

### Python setuptools

Alternative build system for Python packaging:

```bash
# Build wheel
python setup.py bdist_wheel

# Install
pip install dist/sshPilot-1.0.0-py3-none-any.whl
```

### Flatpak Packaging

```bash
# Build Flatpak
flatpak-builder --repo=repo build data/io.github.mfat.sshpilot.json

# Install locally
flatpak --user install repo io.github.mfat.sshpilot

# Create bundle
flatpak build-bundle repo sshpilot.flatpak io.github.mfat.sshpilot
```

### Debian Packaging

```bash
# Build DEB package
python setup.py --command-packages=stdeb.command bdist_deb

# Install
sudo dpkg -i deb_dist/sshpilot_1.0.0-1_all.deb
```

### RPM Packaging

```bash
# Prepare sources
rpmdev-setuptree
cp sshPilot.spec ~/rpmbuild/SPECS/
tar czf ~/rpmbuild/SOURCES/sshpilot-1.0.0.tar.gz .

# Build RPM
rpmbuild -ba ~/rpmbuild/SPECS/sshPilot.spec
```

## Testing

### Test Structure

```
tests/
├── __init__.py
├── test_connection_manager.py
├── test_terminal.py
├── test_config.py
├── test_key_manager.py
├── test_resource_monitor.py
└── fixtures/
    ├── ssh_config_sample
    └── test_keys/
```

### Unit Testing

```python
import pytest
from unittest.mock import Mock, patch
from io.github.mfat.sshpilot.connection_manager import ConnectionManager

class TestConnectionManager:
    def setup_method(self):
        self.manager = ConnectionManager()
    
    def test_load_ssh_config(self):
        # Test SSH config parsing
        pass
    
    @patch('paramiko.SSHClient')
    def test_connect(self, mock_ssh_client):
        # Test SSH connection
        pass
```

### Integration Testing

```python
import pytest
from gi.repository import Gtk
from io.github.mfat.sshpilot.window import MainWindow

class TestMainWindow:
    def setup_method(self):
        self.app = Gtk.Application()
        self.window = MainWindow(application=self.app)
    
    def test_add_connection(self):
        # Test UI interaction
        pass
```

### Running Tests

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=src/ --cov-report=html

# Run specific test
pytest tests/test_connection_manager.py::TestConnectionManager::test_connect
```

### Mock SSH Connections

```python
@pytest.fixture
def mock_ssh_client():
    with patch('paramiko.SSHClient') as mock:
        client = mock.return_value
        client.connect.return_value = None
        client.exec_command.return_value = (Mock(), Mock(), Mock())
        yield client
```

## Contributing

### Development Workflow

1. **Fork the Repository**
   ```bash
   git clone https://github.com/yourusername/sshpilot.git
   cd sshpilot
   ```

2. **Create Feature Branch**
   ```bash
   git checkout -b feature/new-feature
   ```

3. **Set Up Development Environment**
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   pip install -e .
   ```

4. **Make Changes**
   - Follow coding standards
   - Add tests for new features
   - Update documentation

5. **Test Changes**
   ```bash
   pytest tests/
   black src/
   flake8 src/
   mypy src/
   ```

6. **Commit and Push**
   ```bash
   git add .
   git commit -m "Add new feature"
   git push origin feature/new-feature
   ```

7. **Create Pull Request**

### Coding Standards

#### Python Style
- Follow PEP 8
- Use Black for formatting
- Maximum line length: 88 characters
- Use type hints where appropriate

#### Git Commits
- Use conventional commit format
- Examples:
  - `feat: add SSH tunnel support`
  - `fix: resolve connection timeout issue`
  - `docs: update user manual`
  - `test: add connection manager tests`

#### Documentation
- Document all public methods
- Use Google-style docstrings
- Update user manual for UI changes
- Include code examples

### Adding New Features

#### New UI Components
1. Create UI file in `src/io.github.mfat.sshpilot/ui/`
2. Add to GResource file
3. Create Python class with `@Gtk.Template`
4. Implement signal handlers
5. Add to main window if needed

#### New SSH Features
1. Extend `ConnectionManager` class
2. Add configuration options
3. Update connection dialog UI
4. Add tests
5. Document in user manual

#### New Monitoring Metrics
1. Extend `ResourceMonitor` class
2. Add parsing logic
3. Update chart display
4. Add configuration options
5. Test with various systems

## API Documentation

### Core Classes

#### SshPilotApplication
Main application class extending `Adw.Application`.

```python
class SshPilotApplication(Adw.Application):
    def __init__(self):
        """Initialize application with actions and shortcuts"""
    
    def do_activate(self):
        """Create and present main window"""
```

#### MainWindow
Primary application window.

```python
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        """Initialize main window with UI components"""
    
    def show_connection_dialog(self, connection=None):
        """Show connection add/edit dialog"""
    
    def connect_to_host(self, connection):
        """Create terminal tab and connect to host"""
```

#### Connection
Data class representing SSH connection.

```python
class Connection:
    def __init__(self, data):
        """Initialize connection from configuration data"""
    
    @property
    def is_connected(self):
        """Check if connection is active"""
```

### Configuration Schema

#### GSettings Keys
```xml
<key name="terminal-theme" type="s">
  <default>'default'</default>
  <summary>Terminal color theme</summary>
</key>

<key name="ui-window-width" type="i">
  <default>1200</default>
  <summary>Window width in pixels</summary>
</key>
```

#### JSON Configuration
```json
{
  "terminal": {
    "theme": "default",
    "font": "Monospace 12"
  },
  "ui": {
    "window_width": 1200,
    "window_height": 800
  }
}
```

### Signal Reference

#### ConnectionManager Signals
- `connection-added(connection)`: New connection created
- `connection-removed(connection)`: Connection deleted
- `connection-updated(connection)`: Connection modified
- `connection-status-changed(connection, is_connected)`: Status change

#### TerminalWidget Signals
- `connection-established()`: SSH connection successful
- `connection-lost()`: SSH connection terminated
- `title-changed(title)`: Terminal title updated

#### ResourceMonitor Signals
- `data-updated(connection, resource_data)`: New monitoring data
- `monitoring-started(connection)`: Monitoring session started
- `monitoring-stopped(connection)`: Monitoring session ended
- `error-occurred(connection, error_message)`: Monitoring error

### Extension Points

#### Custom Themes
```python
def add_custom_theme(self, name, theme_data):
    """Add custom terminal theme"""
    self.terminal_themes[name] = theme_data
```

#### Plugin Architecture
Future plugin system will support:
- Custom connection types
- Additional monitoring metrics
- UI extensions
- Protocol handlers

---

This developer guide provides comprehensive information for contributing to sshPilot. For questions or clarifications, please open an issue on GitHub or contact the maintainers.