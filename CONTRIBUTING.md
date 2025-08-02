# Contributing to sshPilot

Thank you for your interest in contributing to sshPilot! This document provides guidelines and information for contributors.

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [Getting Started](#getting-started)
3. [Development Setup](#development-setup)
4. [Contributing Guidelines](#contributing-guidelines)
5. [Pull Request Process](#pull-request-process)
6. [Issue Reporting](#issue-reporting)
7. [Development Workflow](#development-workflow)
8. [Coding Standards](#coding-standards)
9. [Testing](#testing)
10. [Documentation](#documentation)

## Code of Conduct

This project and everyone participating in it is governed by our Code of Conduct. By participating, you are expected to uphold this code.

### Our Pledge

We pledge to make participation in our project a harassment-free experience for everyone, regardless of age, body size, disability, ethnicity, gender identity and expression, level of experience, nationality, personal appearance, race, religion, or sexual identity and orientation.

### Our Standards

Examples of behavior that contributes to creating a positive environment include:
- Using welcoming and inclusive language
- Being respectful of differing viewpoints and experiences
- Gracefully accepting constructive criticism
- Focusing on what is best for the community
- Showing empathy towards other community members

### Enforcement

Project maintainers are responsible for clarifying the standards of acceptable behavior and are expected to take appropriate and fair corrective action in response to any instances of unacceptable behavior.

## Getting Started

### Prerequisites

Before contributing, ensure you have:
- Python 3.10 or newer
- GTK 4.6+ and libadwaita 1.2+
- Git for version control
- Basic understanding of Python and GTK

### First Time Contributors

If you're new to open source or this project:
1. Look for issues labeled `good first issue` or `help wanted`
2. Read through the [Developer Guide](docs/developer_guide.md)
3. Set up your development environment
4. Start with small changes to familiarize yourself with the codebase

## Development Setup

### 1. Fork and Clone

```bash
# Fork the repository on GitHub, then clone your fork
git clone https://github.com/yourusername/sshpilot.git
cd sshpilot

# Add upstream remote
git remote add upstream https://github.com/mfat/sshpilot.git
```

### 2. Install System Dependencies

**Debian/Ubuntu:**
```bash
sudo apt install python3-dev python3-pip python3-venv \
    libgirepository1.0-dev libgtk-4-dev libadwaita-1-dev \
    libvte-2.91-gtk4-dev gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-vte-2.91-gtk4 meson ninja-build
```

**Fedora:**
```bash
sudo dnf install python3-devel python3-pip \
    gobject-introspection-devel gtk4-devel libadwaita-devel \
    vte291-gtk4-devel meson ninja-build
```

### 3. Set Up Python Environment

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -e .

# Install development dependencies
pip install pytest pytest-cov black flake8 mypy sphinx
```

### 4. Verify Setup

```bash
# Run application
python -m io.github.mfat.sshpilot.main

# Run tests
pytest tests/

# Check code style
black --check src/
flake8 src/
```

## Contributing Guidelines

### Types of Contributions

We welcome various types of contributions:

#### Bug Fixes
- Fix reported issues
- Improve error handling
- Resolve crashes or unexpected behavior

#### New Features
- SSH protocol enhancements
- UI improvements
- New monitoring capabilities
- Additional authentication methods

#### Documentation
- API documentation
- User manual updates
- Code comments
- Tutorial content

#### Testing
- Unit tests
- Integration tests
- UI tests
- Performance tests

#### Translations
- UI text translations
- Documentation translations
- Error message translations

### Contribution Areas

#### High Priority
- SSH connection stability
- Terminal performance
- Security improvements
- Accessibility features

#### Medium Priority
- New SSH features
- UI enhancements
- Additional monitoring metrics
- Plugin system

#### Low Priority
- Code refactoring
- Performance optimizations
- Additional themes
- Nice-to-have features

## Pull Request Process

### 1. Create Feature Branch

```bash
# Sync with upstream
git fetch upstream
git checkout main
git merge upstream/main

# Create feature branch
git checkout -b feature/your-feature-name
```

### 2. Make Changes

- Follow coding standards
- Write tests for new functionality
- Update documentation as needed
- Ensure all tests pass

### 3. Commit Changes

```bash
# Stage changes
git add .

# Commit with descriptive message
git commit -m "feat: add SSH tunnel support

- Implement local, remote, and dynamic forwarding
- Add tunnel configuration UI
- Update connection dialog
- Add tests for tunnel functionality"
```

### 4. Push and Create PR

```bash
# Push to your fork
git push origin feature/your-feature-name

# Create pull request on GitHub
```

### 5. PR Requirements

Your pull request must:
- [ ] Pass all automated tests
- [ ] Include tests for new functionality
- [ ] Follow code style guidelines
- [ ] Update relevant documentation
- [ ] Have a clear description of changes
- [ ] Reference related issues

### 6. Review Process

1. **Automated Checks**: CI/CD pipeline runs tests and checks
2. **Code Review**: Maintainers review code quality and design
3. **Testing**: Manual testing of new features
4. **Documentation Review**: Ensure docs are updated
5. **Approval**: At least one maintainer approval required
6. **Merge**: Squash and merge to main branch

## Issue Reporting

### Before Creating an Issue

1. **Search existing issues** to avoid duplicates
2. **Check latest version** - issue may be already fixed
3. **Gather information** - logs, system details, steps to reproduce

### Bug Reports

Use the bug report template and include:
- **Environment**: OS, Python version, GTK version
- **Steps to reproduce**: Detailed steps
- **Expected behavior**: What should happen
- **Actual behavior**: What actually happens
- **Logs**: Relevant log output
- **Screenshots**: If applicable

### Feature Requests

Use the feature request template and include:
- **Use case**: Why is this feature needed
- **Proposed solution**: How should it work
- **Alternatives**: Other approaches considered
- **Additional context**: Screenshots, mockups, etc.

### Issue Labels

- `bug`: Something isn't working
- `enhancement`: New feature or improvement
- `documentation`: Documentation related
- `good first issue`: Good for newcomers
- `help wanted`: Extra attention needed
- `question`: Further information requested
- `wontfix`: This will not be worked on

## Development Workflow

### Branch Strategy

- **main**: Stable release branch
- **develop**: Development integration branch
- **feature/***: Feature development branches
- **bugfix/***: Bug fix branches
- **release/***: Release preparation branches

### Commit Message Format

Use conventional commit format:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

**Examples:**
```
feat(ssh): add support for Ed25519 keys

Implement Ed25519 key generation and authentication support.
Includes UI updates and comprehensive tests.

Closes #123
```

```
fix(terminal): resolve copy/paste issue in Wayland

The clipboard operations were failing under Wayland due to
incorrect GDK clipboard API usage.

Fixes #456
```

### Release Process

1. **Version Bump**: Update version in `__init__.py`
2. **Changelog**: Update `CHANGELOG.md` with new features and fixes
3. **Testing**: Comprehensive testing of release candidate
4. **Documentation**: Update user manual and API docs
5. **Packaging**: Build and test all package formats
6. **Release**: Create GitHub release with packages
7. **Distribution**: Update Flatpak, distribution packages

## Coding Standards

### Python Style

Follow PEP 8 with these specifics:
- **Line length**: 88 characters (Black default)
- **Imports**: Group and sort imports
- **Docstrings**: Google style for all public methods
- **Type hints**: Use where appropriate
- **f-strings**: Preferred for string formatting

### Code Formatting

Use Black for consistent formatting:
```bash
black src/ tests/
```

### Linting

Use flake8 for code quality:
```bash
flake8 src/ tests/
```

Configuration in `setup.cfg`:
```ini
[flake8]
max-line-length = 88
extend-ignore = E203, W503
exclude = build, venv
```

### Type Checking

Use mypy for type checking:
```bash
mypy src/ --ignore-missing-imports
```

### Documentation Strings

```python
def connect_to_host(self, connection: Connection) -> bool:
    """Connect to SSH host and create terminal session.
    
    Args:
        connection: Connection object with host details
        
    Returns:
        True if connection successful, False otherwise
        
    Raises:
        ConnectionError: If SSH connection fails
        AuthenticationError: If authentication fails
    """
```

### Error Handling

```python
import logging

logger = logging.getLogger(__name__)

def risky_operation():
    try:
        # Operation that might fail
        result = perform_ssh_operation()
        return result
    except paramiko.AuthenticationException as e:
        logger.error(f"Authentication failed: {e}")
        raise AuthenticationError(f"SSH authentication failed: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise
```

### GTK/UI Code

```python
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, GObject

@Gtk.Template(resource_path='/io/github/mfat/sshpilot/ui/dialog.ui')
class MyDialog(Adw.Window):
    __gtype_name__ = 'MyDialog'
    
    # Template children
    entry = Gtk.Template.Child()
    button = Gtk.Template.Child()
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setup_signals()
    
    @Gtk.Template.Callback()
    def on_button_clicked(self, button):
        """Handle button click event."""
        # Event handling logic
```

## Testing

### Test Structure

```
tests/
├── __init__.py
├── conftest.py              # Pytest configuration and fixtures
├── test_connection_manager.py
├── test_terminal.py
├── test_ui.py
├── integration/
│   ├── test_ssh_integration.py
│   └── test_ui_integration.py
└── fixtures/
    ├── ssh_config_samples/
    └── test_data/
```

### Unit Tests

```python
import pytest
from unittest.mock import Mock, patch, MagicMock
from io.github.mfat.sshpilot.connection_manager import ConnectionManager

class TestConnectionManager:
    def setup_method(self):
        """Set up test fixtures."""
        self.manager = ConnectionManager()
    
    def test_load_ssh_config_valid(self):
        """Test loading valid SSH configuration."""
        # Test implementation
        assert len(self.manager.connections) > 0
    
    @patch('paramiko.SSHClient')
    def test_connect_success(self, mock_ssh_client):
        """Test successful SSH connection."""
        # Mock setup
        mock_client = Mock()
        mock_ssh_client.return_value = mock_client
        
        # Test connection
        connection = Mock()
        result = self.manager.connect(connection)
        
        # Assertions
        assert result is not None
        mock_client.connect.assert_called_once()
```

### Integration Tests

```python
import pytest
from gi.repository import Gtk, GLib
from io.github.mfat.sshpilot.main import SshPilotApplication

class TestApplicationIntegration:
    def setup_method(self):
        """Set up application for testing."""
        self.app = SshPilotApplication()
    
    def test_application_startup(self):
        """Test application starts correctly."""
        # Test application initialization
        assert self.app.get_application_id() == 'io.github.mfat.sshpilot'
```

### Running Tests

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest tests/ --cov=src/ --cov-report=html

# Run specific test file
pytest tests/test_connection_manager.py

# Run specific test
pytest tests/test_connection_manager.py::TestConnectionManager::test_connect

# Run with verbose output
pytest tests/ -v

# Run integration tests only
pytest tests/integration/
```

### Test Coverage

Maintain high test coverage:
- **Minimum**: 80% overall coverage
- **Target**: 90%+ for core components
- **Critical paths**: 100% coverage for security-related code

```bash
# Generate coverage report
pytest tests/ --cov=src/ --cov-report=html --cov-report=term

# View HTML report
firefox htmlcov/index.html
```

## Documentation

### Types of Documentation

#### Code Documentation
- Docstrings for all public methods
- Inline comments for complex logic
- Type hints for function signatures

#### User Documentation
- User manual updates for new features
- Installation instructions
- Troubleshooting guides

#### Developer Documentation
- API documentation
- Architecture decisions
- Contributing guidelines

### Documentation Tools

#### Sphinx for API Docs
```bash
# Install Sphinx
pip install sphinx sphinx-rtd-theme

# Generate documentation
cd docs/
make html

# View documentation
firefox _build/html/index.html
```

#### Markdown for Guides
- Use GitHub Flavored Markdown
- Include code examples
- Add screenshots for UI features

### Documentation Standards

#### Writing Style
- Clear and concise language
- Step-by-step instructions
- Include examples and screenshots
- Test all code examples

#### Code Examples
```python
# Good: Complete, runnable example
from io.github.mfat.sshpilot.connection_manager import ConnectionManager

manager = ConnectionManager()
connections = manager.get_connections()
for conn in connections:
    print(f"Connection: {conn.nickname}")
```

#### Screenshots
- Use consistent window sizes
- Highlight relevant UI elements
- Include dark and light theme variants
- Compress images appropriately

### Documentation Review Process

1. **Technical accuracy**: Verify all code examples work
2. **Clarity**: Ensure instructions are easy to follow
3. **Completeness**: Cover all necessary information
4. **Consistency**: Match project style and tone
5. **Accessibility**: Consider users with different backgrounds

---

## Getting Help

### Communication Channels

- **GitHub Issues**: Bug reports and feature requests
- **GitHub Discussions**: General questions and discussions
- **Email**: newmfat@gmail.com for direct contact

### Resources

- [Developer Guide](docs/developer_guide.md)
- [User Manual](docs/user_manual.md)
- [API Documentation](https://mfat.github.io/sshpilot/)
- [GTK4 Documentation](https://docs.gtk.org/gtk4/)
- [libadwaita Documentation](https://gnome.pages.gitlab.gnome.org/libadwaita/)

Thank you for contributing to sshPilot! Your contributions help make SSH management better for everyone.