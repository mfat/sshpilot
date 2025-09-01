# GitHub Actions for sshPilot

This document explains the automated build and release workflows for sshPilot.

## 🚀 Available Workflows

### 1. Build macOS Bundle and DMG (`build-macos.yml`)

**Purpose**: Create production releases with automatic GitHub releases

**Triggers**:
- ✅ **Automatic**: Push tags matching `v*.*.*` (e.g., `v2.7.1`)
- ✅ **Manual**: Workflow dispatch with custom version

**What it does**:
1. Sets up macOS runner with Python 3.13
2. Installs Homebrew and GTK dependencies
3. Builds the application bundle
4. Creates the DMG installer
5. Tests the bundle functionality
6. Creates a GitHub release with the DMG
7. Uploads artifacts for download

**Output**:
- Professional DMG installer attached to GitHub release
- Build artifacts available for download
- Automatic release notes generation

### 2. Test macOS Build (`test-build.yml`)

**Purpose**: Verify builds work before creating releases

**Triggers**:
- ✅ **Automatic**: Every push to `main` and `develop` branches
- ✅ **Automatic**: Pull requests to `main` branch
- ✅ **Manual**: Workflow dispatch

**What it does**:
1. Same build process as production workflow
2. Tests the application bundle
3. Uploads test artifacts (7-day retention)
4. **No releases created** - just testing

**Output**:
- Test artifacts for verification
- Build verification for CI/CD pipeline

## 🎯 How to Use

### Creating a New Release

#### Option 1: Automatic (Recommended)
```bash
# Create and push a new version tag
git tag v2.7.1
git push origin v2.7.1
```

This automatically:
- Triggers the build workflow
- Creates the macOS bundle and DMG
- Publishes a GitHub release
- Attaches the DMG for download

#### Option 2: Manual Trigger
1. Go to GitHub → Actions → Build macOS Bundle and DMG
2. Click "Run workflow"
3. Enter version (e.g., `v2.7.1`)
4. Click "Run workflow"

### Testing Builds

#### Automatic Testing
- Every push to main/develop branches triggers test builds
- Pull requests are automatically tested
- Ensures code quality before releases

#### Manual Testing
1. Go to GitHub → Actions → Test macOS Build
2. Click "Run workflow"
3. Build will run and upload test artifacts

## 🔧 Workflow Details

### Build Environment
- **Runner**: `macos-latest` (macOS 13)
- **Python**: 3.13
- **Architecture**: ARM64 (Apple Silicon)

### Dependencies Installed
```bash
# GTK Stack
brew install gtk4 libadwaita pygobject3

# Bundling Tools
brew install gtk-mac-bundler create-dmg

# Python Dependencies
pip3 install -r requirements.txt
```

### Build Process
1. **Setup**: Install dependencies and set environment
2. **Build**: Create `.app` bundle using `gtk-mac-bundler`
3. **Package**: Create DMG using `create-dmg`
4. **Test**: Verify bundle launches correctly
5. **Release**: Create GitHub release with DMG

### Artifacts
- **App Bundle**: `sshPilot.app` (fully self-contained)
- **DMG Installer**: `sshPilot-macOS.dmg` (professional installer)
- **Retention**: 30 days for releases, 7 days for tests

## 📊 Release Process

### Automatic Release Creation
When a version tag is pushed:

1. **Build Phase**
   - Install dependencies
   - Build application bundle
   - Create DMG installer
   - Test functionality

2. **Release Phase**
   - Create GitHub release
   - Attach DMG file
   - Generate release notes
   - Set release title and description

3. **Distribution**
   - DMG available for download
   - Release notes published
   - Artifacts uploaded

### Release Content
Each release includes:
- **Version**: Tag-based versioning
- **DMG**: Professional installer package
- **Notes**: Automatic feature detection
- **Build Info**: Platform, Python version, build date

## 🔍 Monitoring and Debugging

### Workflow Status
- Check Actions tab for build status
- View logs for any build failures
- Download artifacts for testing

### Common Issues

**Build Failures**
- Check dependency installation logs
- Verify Python version compatibility
- Ensure all scripts are executable

**Release Failures**
- Verify tag format (`v*.*.*`)
- Check repository permissions
- Review release creation logs

**Artifact Issues**
- Check artifact upload logs
- Verify file paths and permissions
- Review retention settings

## 🎉 Success Indicators

A successful workflow run means:
- ✅ macOS bundle builds successfully
- ✅ DMG installer is created
- ✅ Application launches correctly
- ✅ GitHub release is published
- ✅ DMG is attached and downloadable

## 🚀 Next Steps

### For Users
1. Go to GitHub Releases
2. Download the latest DMG
3. Install by dragging to Applications
4. Launch sshPilot

### For Developers
1. Push code to main/develop for testing
2. Create version tags for releases
3. Monitor workflow execution
4. Download and test artifacts

### For Contributors
1. Fork the repository
2. Make changes and test locally
3. Submit pull request
4. Automatic testing ensures quality

## 📚 Related Documentation

- **`packaging/macos/README.md`** - Detailed packaging guide
- **`packaging/macos/PYGOBJECT_MACOS_BUNDLING_GUIDE.md`** - Technical bundling guide
- **`packaging/macos/QUICK_REFERENCE.md`** - Quick reference for developers

---

Your sshPilot app now has a complete, automated build and release pipeline! 🎉
