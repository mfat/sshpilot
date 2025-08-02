# Testing sshPilot

This guide covers how to test the sshPilot application during development.

## Quick Start

### 1. Install Dependencies

```bash
# Install system dependencies (Ubuntu/Debian)
sudo apt install python3-dev python3-pip python3-venv \
    libgirepository1.0-dev libgtk-4-dev libadwaita-1-dev \
    libvte-2.91-gtk4-dev gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-vte-2.91-gtk4

# Or for Fedora
sudo dnf install python3-devel python3-pip \
    gobject-introspection-devel gtk4-devel libadwaita-devel \
    vte291-gtk4-devel
```

### 2. Set Up Python Environment

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install development dependencies
pip install pytest pytest-cov black flake8 mypy
```

### 3. Run the Application

```bash
# Method 1: Using the run script (recommended for development)
./run_sshpilot.py

# Method 2: Using Python module syntax
python -m io.github.mfat.sshpilot.main

# Method 3: With debug logging
SSHPILOT_DEBUG=1 ./run_sshpilot.py
```

## Running Tests

### Unit Tests

```bash
# Run all tests
pytest tests/

# Run with verbose output
pytest tests/ -v

# Run specific test file
pytest tests/test_connection.py

# Run specific test
pytest tests/test_connection.py::TestConnectionManager::test_connect_success

# Run with coverage
pytest tests/ --cov=src/ --cov-report=html --cov-report=term

# View coverage report
firefox htmlcov/index.html
```

### Test Categories

#### Connection Tests
```bash
# Test SSH connection management
pytest tests/test_connection.py -v
```

#### UI Tests
```bash
# Test UI components (requires mocked GTK)
pytest tests/test_ui.py -v
```

#### Resource Monitor Tests
```bash
# Test system monitoring functionality
pytest tests/test_resource_monitor.py -v
```

#### Key Manager Tests
```bash
# Test SSH key management
pytest tests/test_key_manager.py -v
```

### Integration Tests

```bash
# Run integration tests (if any)
pytest tests/integration/ -v

# Run all tests including integration
pytest tests/ --integration
```

## Code Quality Checks

### Linting

```bash
# Check code style with flake8
flake8 src/ tests/

# Format code with black
black src/ tests/

# Check formatting without changes
black --check src/ tests/
```

### Type Checking

```bash
# Run mypy type checking
mypy src/ --ignore-missing-imports
```

### Pre-commit Checks

```bash
# Run all quality checks
./scripts/check_code.sh
```

## Manual Testing

### Testing the UI

1. **Start the application:**
   ```bash
   ./run_sshpilot.py
   ```

2. **Test basic functionality:**
   - Welcome screen should appear
   - Sidebar should be empty initially
   - Menu and toolbar should be responsive

3. **Test connection management:**
   - Click "+" to add connection
   - Fill in connection details
   - Save and verify it appears in sidebar
   - Try editing and deleting connections

### Testing SSH Connections

⚠️ **Warning:** These tests require actual SSH servers to connect to.

1. **Set up test SSH server:**
   ```bash
   # On a separate machine or VM
   sudo apt install openssh-server
   sudo systemctl start ssh
   ```

2. **Test connection:**
   - Add connection with real server details
   - Double-click to connect
   - Verify terminal opens and connects
   - Test commands in terminal

3. **Test authentication methods:**
   - Password authentication
   - SSH key authentication
   - SSH agent integration

### Testing SSH Key Management

1. **Generate keys:**
   - Press Ctrl+Shift+K
   - Try different key types (RSA, Ed25519)
   - Test with and without passphrase

2. **Deploy keys:**
   - Generate key
   - Use in connection
   - Verify key deployment works

### Testing Resource Monitoring

1. **Connect to server**
2. **Press Ctrl+R to open resource monitor**
3. **Verify charts show:**
   - CPU usage
   - Memory usage
   - Disk usage
   - Network I/O

## Debugging

### Debug Mode

```bash
# Enable debug logging
SSHPILOT_DEBUG=1 ./run_sshpilot.py

# Check logs
tail -f ~/.local/share/sshPilot/sshpilot.log
```

### Common Issues

#### ImportError: attempted relative import

**Problem:** Running modules directly with relative imports
```bash
# ❌ Wrong way
python src/io.github.mfat.sshpilot/main.py

# ✅ Correct ways
./run_sshpilot.py
python -m io.github.mfat.sshpilot.main
```

#### GTK/Adwaita not found

**Problem:** Missing system dependencies
```bash
# Install required packages
sudo apt install gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-2.91-gtk4
```

#### Permission denied for SSH keys

**Problem:** Incorrect key file permissions
```bash
# Fix key permissions
chmod 600 ~/.ssh/id_rsa
chmod 644 ~/.ssh/id_rsa.pub
```

### Testing Without Display

For headless testing (CI/CD):

```bash
# Install virtual display
sudo apt install xvfb

# Run tests with virtual display
xvfb-run -a pytest tests/test_ui.py
```

## Mocking for Tests

### SSH Connections

```python
@patch('paramiko.SSHClient')
def test_ssh_connection(self, mock_ssh_client):
    mock_client = Mock()
    mock_ssh_client.return_value = mock_client
    
    # Test SSH functionality
    connection = Connection(test_data)
    result = manager.connect(connection)
    
    mock_client.connect.assert_called_once()
```

### GTK Components

```python
@patch('gi.repository.Gtk.ListBox')
def test_ui_component(self, mock_listbox):
    # Test UI functionality without actual GTK
    window = MainWindow()
    # Test logic
```

### System Keyring

```python
@patch('secretstorage.get_default_collection')
def test_password_storage(self, mock_collection):
    collection = Mock()
    mock_collection.return_value = collection
    
    # Test password storage
    manager.store_password('host', 'user', 'pass')
    collection.create_item.assert_called_once()
```

## Performance Testing

### Memory Usage

```bash
# Monitor memory usage
python -m memory_profiler run_sshpilot.py
```

### Connection Performance

```bash
# Time SSH connections
time ssh user@host "echo 'test'"
```

## Automated Testing

### GitHub Actions

The project includes automated testing via GitHub Actions:

- **Code quality checks:** flake8, black, mypy
- **Unit tests:** pytest with coverage
- **Build tests:** Verify packages build correctly

### Local CI Simulation

```bash
# Run the same checks as CI
./.github/scripts/test.sh
```

## Test Data

### Sample SSH Config

```bash
# Create test SSH config
mkdir -p ~/.ssh
cat > ~/.ssh/config << EOF
Host test-local
    HostName localhost
    User $(whoami)
    Port 22

Host test-remote
    HostName example.com
    User testuser
    IdentityFile ~/.ssh/id_rsa
EOF
```

### Mock SSH Server

For integration testing, you can use a mock SSH server:

```python
# tests/mock_ssh_server.py
import paramiko
import threading
import socket

class MockSSHServer:
    def __init__(self, port=2222):
        self.port = port
        self.server_socket = None
        self.thread = None
    
    def start(self):
        # Implement mock SSH server
        pass
    
    def stop(self):
        # Stop mock server
        pass
```

## Continuous Integration

### Pre-commit Hooks

```bash
# Install pre-commit
pip install pre-commit

# Set up hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

### Test Matrix

The project tests against:
- **Python:** 3.10, 3.11, 3.12
- **OS:** Ubuntu 22.04, Fedora 38
- **GTK:** 4.6+, 4.8+, 4.10+

## Troubleshooting Tests

### Test Failures

1. **Check dependencies:**
   ```bash
   pip check
   ```

2. **Update packages:**
   ```bash
   pip install -r requirements.txt --upgrade
   ```

3. **Clear cache:**
   ```bash
   pytest --cache-clear
   ```

### Mock Issues

1. **Verify mock paths:**
   ```python
   # Make sure mock targets are correct
   @patch('io.github.mfat.sshpilot.connection_manager.paramiko.SSHClient')
   ```

2. **Check import paths:**
   ```python
   # Ensure imports work
   from io.github.mfat.sshpilot.connection_manager import ConnectionManager
   ```

## Contributing Tests

When adding new features:

1. **Write tests first** (TDD approach)
2. **Test both success and failure cases**
3. **Mock external dependencies**
4. **Include integration tests for complex features**
5. **Update this testing guide**

For more details, see [CONTRIBUTING.md](CONTRIBUTING.md).

---

Happy testing! 🧪