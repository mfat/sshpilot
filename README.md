# SSHPilot Tauri

A modern, cross-platform SSH client built with Tauri, Rust, and web technologies. SSHPilot provides a beautiful, responsive interface for managing SSH connections, terminals, and keys while maintaining the performance and security of native applications.

## ✨ Features

### 🔐 SSH Connection Management
- **Connection Profiles**: Save and manage SSH connection configurations
- **Multiple Authentication Methods**: Password, SSH key, and key with passphrase
- **Connection Groups**: Organize connections by project or purpose
- **Connection Testing**: Verify connectivity before establishing sessions
- **Secure Storage**: Encrypted storage of sensitive connection data

### 🖥️ Terminal Emulation
- **xterm.js Integration**: Full-featured terminal emulation with web technologies
- **Tabbed Interface**: Multiple terminal sessions in organized tabs
- **Local & Remote Terminals**: Both local shell and SSH-connected terminals
- **Terminal Customization**: Font size, colors, and theme support
- **Copy/Paste Support**: Native clipboard integration

### 🔑 SSH Key Management
- **Key Generation**: Create Ed25519, RSA, and ECDSA keys
- **Key Discovery**: Automatically detect existing SSH keys
- **Secure Storage**: Encrypted storage of private keys
- **Key Import**: Import existing SSH keys from various formats
- **Passphrase Support**: Optional encryption for added security

### 🌐 Modern Web Interface
- **Libadwaita-inspired Design**: Clean, modern UI following GNOME design principles
- **Responsive Layout**: Works seamlessly on different screen sizes
- **Theme Support**: Light, dark, and custom themes with system preference detection
- **Accessibility**: High contrast mode and reduced motion support
- **Cross-platform**: Consistent experience on Windows, macOS, and Linux

### 🚀 Advanced Features
- **Port Forwarding**: Local-to-remote and remote-to-local port forwarding
- **SCP File Transfer**: Secure file uploads and downloads
- **Command Execution**: Run commands on remote servers
- **Search & Filter**: Quick access to connections, keys, and commands
- **Keyboard Shortcuts**: Power user shortcuts for common actions

## 🏗️ Architecture

### Backend (Rust/Tauri)
- **SSH Client**: `ssh2` crate for SSH protocol implementation
- **Connection Management**: Persistent storage and connection lifecycle
- **Terminal Management**: Virtual terminal session handling
- **Key Management**: SSH key operations and storage
- **Port Utilities**: Network port management and forwarding
- **Configuration**: Application settings and preferences

### Frontend (Web Technologies)
- **HTML5**: Semantic markup and accessibility
- **CSS3**: Modern styling with CSS custom properties
- **JavaScript ES6+**: Modular architecture with ES modules
- **xterm.js**: Terminal emulation library
- **Responsive Design**: Mobile-first approach

### Key Technologies
- **Tauri**: Desktop application framework
- **Rust**: Backend language for performance and safety
- **xterm.js**: Terminal emulation
- **Vite**: Frontend build tool
- **CSS Grid/Flexbox**: Modern layout systems

## 🚀 Getting Started

### Prerequisites
- **Node.js** 18+ and npm
- **Rust** toolchain (rustup)
- **Tauri CLI**: `npm install -g @tauri-apps/cli`

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/sshpilot-tauri.git
   cd sshpilot-tauri
   ```

2. **Install dependencies**
   ```bash
   npm install
   ```

3. **Start development server**
   ```bash
   npm run tauri dev
   ```

4. **Build for production**
   ```bash
   npm run tauri build
   ```

### Development Commands

```bash
# Start development server
npm run dev

# Build frontend
npm run build

# Preview build
npm run preview

# Tauri commands
npm run tauri dev      # Development with hot reload
npm run tauri build    # Production build
npm run tauri preview  # Preview production build
```

## 📁 Project Structure

```
sshpilot-tauri/
├── src-tauri/                 # Rust backend
│   ├── src/
│   │   ├── main.rs           # Application entry point
│   │   ├── ssh.rs            # SSH client implementation
│   │   ├── connection.rs     # Connection management
│   │   ├── terminal.rs       # Terminal session handling
│   │   ├── key_manager.rs    # SSH key operations
│   │   ├── port_utils.rs     # Port forwarding utilities
│   │   └── config.rs         # Configuration management
│   ├── Cargo.toml            # Rust dependencies
│   └── tauri.conf.json       # Tauri configuration
├── src/                       # Frontend source
│   ├── js/                   # JavaScript modules
│   │   ├── app.js            # Main application logic
│   │   └── modules/          # Feature modules
│   │       ├── connection-manager.js
│   │       ├── terminal-manager.js
│   │       ├── key-manager.js
│   │       ├── ui-manager.js
│   │       ├── theme-manager.js
│   │       └── notification-manager.js
│   ├── styles/               # CSS stylesheets
│   │   ├── main.css          # Core styles
│   │   ├── components.css    # UI components
│   │   ├── terminal.css      # Terminal styling
│   │   └── themes.css        # Theme definitions
│   └── index.html            # Main HTML file
├── package.json               # Frontend dependencies
├── vite.config.js            # Vite configuration
└── README.md                 # This file
```

## 🎨 UI Components

### Header Bar
- **Sidebar Toggle**: Show/hide connection sidebar
- **Search**: Global search across connections and keys
- **Action Buttons**: Quick access to common functions
- **Theme Toggle**: Switch between light and dark themes

### Sidebar
- **Connections**: List of saved SSH connections
- **Groups**: Organized connection categories
- **SSH Keys**: Available authentication keys
- **Quick Actions**: Add new connections, keys, or groups

### Terminal Area
- **Tab Bar**: Multiple terminal sessions
- **Terminal Container**: xterm.js integration
- **Toolbar**: Terminal-specific actions
- **Status Bar**: Connection and session information

### Modal Dialogs
- **Connection Dialog**: Create/edit SSH connections
- **Key Generation**: Generate new SSH keys
- **Settings**: Application preferences
- **File Browser**: Select SSH key files

## 🔧 Configuration

### Tauri Configuration
The `tauri.conf.json` file configures:
- **Application metadata**: Name, version, description
- **Window properties**: Size, position, decorations
- **Permissions**: File system, shell, dialog access
- **Security**: Allowed domains and capabilities

### SSH Configuration
- **Key Storage**: `~/.ssh/` directory integration
- **Connection Profiles**: Persistent connection settings
- **Authentication**: Multiple auth method support
- **Port Forwarding**: Local and remote port rules

### Theme Configuration
- **System Integration**: Automatic theme detection
- **Custom Themes**: User-defined color schemes
- **Terminal Themes**: xterm.js color customization
- **Accessibility**: High contrast and reduced motion

## 🚀 Deployment

### Building Distributables

```bash
# Build for current platform
npm run tauri build

# Build for specific platform
npm run tauri build -- --target x86_64-unknown-linux-gnu
npm run tauri build -- --target x86_64-pc-windows-msvc
npm run tauri build -- --target aarch64-apple-darwin
```

### Distribution Formats
- **Windows**: `.msi` installer and `.exe` executable
- **macOS**: `.dmg` disk image and `.app` bundle
- **Linux**: `.AppImage`, `.deb`, and `.rpm` packages

### Code Signing
- **Windows**: Authenticode certificate
- **macOS**: Developer ID certificate
- **Linux**: GPG signing for packages

## 🤝 Contributing

### Development Setup
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

### Code Style
- **Rust**: Follow rustfmt and clippy guidelines
- **JavaScript**: Use ESLint and Prettier
- **CSS**: Follow BEM methodology
- **HTML**: Semantic markup and accessibility

### Testing
```bash
# Run Rust tests
cargo test

# Run frontend tests
npm test

# Run integration tests
npm run test:integration
```

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **Tauri Team**: For the excellent desktop app framework
- **xterm.js Contributors**: For the powerful terminal emulation library
- **GNOME Design Team**: For the libadwaita design inspiration
- **SSH2 Crate Maintainers**: For the robust SSH implementation

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/sshpilot-tauri/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/sshpilot-tauri/discussions)
- **Documentation**: [Wiki](https://github.com/yourusername/sshpilot-tauri/wiki)

## 🔮 Roadmap

### v1.0.0 (Current)
- ✅ Basic SSH connection management
- ✅ Terminal emulation with xterm.js
- ✅ SSH key generation and management
- ✅ Port forwarding support
- ✅ Modern web-based UI

### v1.1.0 (Planned)
- 🔄 SCP file transfer interface
- 🔄 X11 forwarding support
- 🔄 Connection bookmarks and favorites
- 🔄 Advanced terminal customization

### v1.2.0 (Future)
- 🔮 Multi-factor authentication
- 🔮 SSH connection tunneling
- 🔮 Plugin system for extensions
- 🔮 Cloud sync for configurations

---

**SSHPilot Tauri** - Modern SSH client for the modern web. 🚀
