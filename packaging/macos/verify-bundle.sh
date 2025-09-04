#!/usr/bin/env bash
set -euo pipefail

# Bundle verification script for sshPilot.app
# This script verifies that all required libraries, resources, and paths are correct

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
APP_DIR="${DIST_DIR}/sshPilot.app"
RES_DIR="${APP_DIR}/Contents/Resources"
FRAMEWORKS_DIR="${APP_DIR}/Contents/Frameworks"
MACOS_DIR="${APP_DIR}/Contents/MacOS"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
TOTAL_CHECKS=0
PASSED_CHECKS=0
FAILED_CHECKS=0
WARNING_CHECKS=0

# Helper functions
check_file() {
    local file_path="$1"
    local description="$2"
    local required="${3:-true}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if [ -f "$file_path" ]; then
        echo -e "${GREEN}✓${NC} $description"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        if [ "$required" = "true" ]; then
            echo -e "${RED}✗${NC} $description - MISSING: $file_path"
            FAILED_CHECKS=$((FAILED_CHECKS + 1))
            return 1
        else
            echo -e "${YELLOW}⚠${NC} $description - OPTIONAL: $file_path"
            WARNING_CHECKS=$((WARNING_CHECKS + 1))
            return 0
        fi
    fi
}

# Check for system library dependencies
check_system_dependencies() {
    local binary_path="$1"
    local description="$2"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if [ ! -f "$binary_path" ]; then
        echo -e "${YELLOW}⚠${NC} $description - Binary not found: $binary_path"
        WARNING_CHECKS=$((WARNING_CHECKS + 1))
        return 0
    fi
    
    # Check for system library dependencies
    local system_deps=()
    while IFS= read -r line; do
        # Skip the first line (binary path) and system libraries
        if [[ "$line" == *"$binary_path"* ]]; then
            continue
        fi
        # Check for system paths
        if [[ "$line" == *"/opt/homebrew/"* ]] || [[ "$line" == *"/usr/local/"* ]] || [[ "$line" == *"/System/"* ]]; then
            # Extract library name
            local lib_name=$(echo "$line" | awk '{print $1}' | xargs basename)
            system_deps+=("$lib_name")
        fi
    done < <(otool -L "$binary_path" 2>/dev/null)
    
    if [ ${#system_deps[@]} -eq 0 ]; then
        echo -e "${GREEN}✓${NC} $description - No system dependencies"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
    else
        echo -e "${YELLOW}⚠${NC} $description - System dependencies found: ${system_deps[*]}"
        WARNING_CHECKS=$((WARNING_CHECKS + 1))
    fi
}

check_dir() {
    local dir_path="$1"
    local description="$2"
    local required="${3:-true}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if [ -d "$dir_path" ]; then
        echo -e "${GREEN}✓${NC} $description"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        if [ "$required" = "true" ]; then
            echo -e "${RED}✗${NC} $description - MISSING: $dir_path"
            FAILED_CHECKS=$((FAILED_CHECKS + 1))
            return 1
        else
            echo -e "${YELLOW}⚠${NC} $description - OPTIONAL: $dir_path"
            WARNING_CHECKS=$((WARNING_CHECKS + 1))
            return 0
        fi
    fi
}

check_python_package() {
    local package_name="$1"
    local package_path="${RES_DIR}/lib/python3.13/site-packages/${package_name}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    # Check for package directory, .py file, .so file, or .dist-info directory
    if [ -d "$package_path" ] || [ -f "${package_path}.py" ] || [ -f "${package_path}.so" ] || [ -d "${package_path}.dist-info" ]; then
        echo -e "${GREEN}✓${NC} Python package: $package_name"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        echo -e "${RED}✗${NC} Python package: $package_name - MISSING"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

check_gtk_library() {
    local lib_name="$1"
    local lib_path="${RES_DIR}/lib/${lib_name}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if [ -f "$lib_path" ] || [ -d "$lib_path" ]; then
        echo -e "${GREEN}✓${NC} GTK library: $lib_name"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        echo -e "${RED}✗${NC} GTK library: $lib_name - MISSING"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

check_typelib() {
    local typelib_name="$1"
    local typelib_path="${RES_DIR}/lib/girepository-1.0/${typelib_name}"
    
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    
    if [ -f "$typelib_path" ]; then
        echo -e "${GREEN}✓${NC} GI typelib: $typelib_name"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        return 0
    else
        echo -e "${RED}✗${NC} GI typelib: $typelib_name - MISSING"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

# Main verification function
verify_bundle() {
    echo -e "${BLUE}🔍 Verifying sshPilot.app bundle...${NC}"
    echo "Bundle location: $APP_DIR"
    echo ""
    
    # Check if bundle exists
    if [ ! -d "$APP_DIR" ]; then
        echo -e "${RED}❌ Bundle not found at: $APP_DIR${NC}"
        echo "Run the build script first: bash packaging/macos/make-bundle.sh"
        exit 1
    fi
    
    echo -e "${BLUE}📁 Basic Bundle Structure${NC}"
    check_dir "$APP_DIR" "App bundle directory"
    check_dir "$APP_DIR/Contents" "Contents directory"
    check_dir "$RES_DIR" "Resources directory"
    check_dir "$FRAMEWORKS_DIR" "Frameworks directory"
    check_dir "$MACOS_DIR" "MacOS directory"
    check_file "$APP_DIR/Contents/Info.plist" "Info.plist file"
    echo ""
    
    echo -e "${BLUE}🚀 Application Files${NC}"
    check_file "$MACOS_DIR/sshPilot" "Main executable (launcher)"
    check_file "$RES_DIR/app/run.py" "Python entry point"
    check_dir "$RES_DIR/app" "Application source code directory"
    check_file "$RES_DIR/app/main.py" "Main application module"
    check_file "$RES_DIR/app/window.py" "Window module"
    check_file "$RES_DIR/app/connection_manager.py" "Connection manager"
    check_file "$RES_DIR/app/ssh_utils.py" "SSH utilities"
    check_file "$RES_DIR/app/terminal.py" "Terminal module"
    check_file "$RES_DIR/app/__init__.py" "Package init file"
    check_file "$RES_DIR/app/actions.py" "Actions module"
    check_file "$RES_DIR/app/config.py" "Configuration module"
    check_file "$RES_DIR/app/preferences.py" "Preferences module"
    echo ""
    
    echo -e "${BLUE}🐍 Python Runtime and Dependencies${NC}"
    # Check Python runtime
    check_dir "$RES_DIR/lib/python3.13/site-packages" "Python 3.13 site-packages directory"
    
    # Check all packages from requirements.txt
    # PyGObject is installed as 'gi' module with PyGObject-*.dist-info
    check_python_package "gi"
    check_python_package "cairo"
    check_python_package "paramiko"
    check_python_package "cryptography"
    check_python_package "keyring"
    check_python_package "psutil"
    check_python_package "bcrypt"
    check_python_package "nacl"
    check_python_package "cffi"
    check_python_package "invoke"
    check_python_package "jaraco"
    check_python_package "more_itertools"
    check_python_package "pycparser"
    
    # Check for PyGObject dist-info (the actual package name)
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    if ls "${RES_DIR}/lib/python3.13/site-packages"/PyGObject-*.dist-info >/dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Python package: PyGObject (dist-info)"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
    else
        echo -e "${RED}✗${NC} Python package: PyGObject - MISSING"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
    fi
    
    # Check for secretstorage (Linux only, should be excluded on macOS)
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    if [ -d "${RES_DIR}/lib/python3.13/site-packages/secretstorage" ]; then
        echo -e "${YELLOW}⚠${NC} secretstorage found (Linux-only package, should be excluded on macOS)"
        WARNING_CHECKS=$((WARNING_CHECKS + 1))
    else
        echo -e "${GREEN}✓${NC} secretstorage correctly excluded (Linux-only package)"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
    fi
    echo ""
    
    echo -e "${BLUE}🎨 GTK4 and libadwaita Libraries${NC}"
    # Core GTK4 libraries (check for actual versioned names)
    check_gtk_library "libgtk-4.dylib"
    check_gtk_library "libgobject-2.0.dylib"
    check_gtk_library "libglib-2.0.dylib"
    check_gtk_library "libgio-2.0.dylib"
    check_gtk_library "libgmodule-2.0.dylib"
    check_gtk_library "libgthread-2.0.dylib"
    
    # libadwaita (Adwaita UI library)
    check_gtk_library "libadwaita-1.dylib"
    
    # Supporting libraries
    check_gtk_library "libpango-1.0.dylib"
    check_gtk_library "libpangocairo-1.0.dylib"
    check_gtk_library "libpangoft2-1.0.dylib"
    check_gtk_library "libatk-1.0.dylib"
    check_gtk_library "libcairo.dylib"
    check_gtk_library "libgdk_pixbuf-2.0.dylib"
    check_gtk_library "libvte-2.91-gtk4.dylib"  # VTE for GTK4
    
    # Graphene (used by GTK4)
    check_gtk_library "libgraphene-1.0.dylib"
    
    # pkg-config (build tool, may be needed for some operations)
    check_file "$RES_DIR/bin/pkg-config" "pkg-config binary" false
    echo ""
    
    echo -e "${BLUE}📚 GI Typelibs (GObject Introspection)${NC}"
    # Core GTK4 typelibs
    check_typelib "Gtk-4.0.typelib"
    check_typelib "Gdk-4.0.typelib"
    check_typelib "GObject-2.0.typelib"
    check_typelib "GLib-2.0.typelib"
    check_typelib "Gio-2.0.typelib"
    check_typelib "GModule-2.0.typelib"
    # GThread typelib is optional and not always present
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    if [ -f "${RES_DIR}/lib/girepository-1.0/GThread-2.0.typelib" ]; then
        echo -e "${GREEN}✓${NC} GI typelib: GThread-2.0.typelib"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
    else
        echo -e "${YELLOW}⚠${NC} GI typelib: GThread-2.0.typelib - OPTIONAL (not always present)"
        WARNING_CHECKS=$((WARNING_CHECKS + 1))
    fi
    
    # libadwaita typelib
    check_typelib "Adw-1.typelib"
    
    # Supporting typelibs
    check_typelib "Pango-1.0.typelib"
    check_typelib "PangoCairo-1.0.typelib"
    check_typelib "PangoFT2-1.0.typelib"
    check_typelib "Atk-1.0.typelib"
    check_typelib "Cairo-1.0.typelib"
    check_typelib "GdkPixbuf-2.0.typelib"
    check_typelib "Vte-2.91.typelib"
    check_typelib "Graphene-1.0.typelib"
    echo ""
    
    echo -e "${BLUE}🌐 ICU Libraries (Unicode Support)${NC}"
    check_file "$FRAMEWORKS_DIR/libicuuc.dylib" "ICU Unicode library" false
    check_file "$FRAMEWORKS_DIR/libicudata.dylib" "ICU data library" false
    check_file "$FRAMEWORKS_DIR/libicui18n.dylib" "ICU internationalization library" false
    echo ""
    
    echo -e "${BLUE}🔧 System Utilities and Tools${NC}"
    # Check for sshpass (SSH password authentication tool)
    check_file "$RES_DIR/bin/sshpass" "sshpass utility" false
    
    # Check for other common system tools that might be needed
    check_file "$RES_DIR/bin/ssh" "SSH client" false
    check_file "$RES_DIR/bin/scp" "SCP client" false
    check_file "$RES_DIR/bin/sftp" "SFTP client" false
    
    # Check for gobject-introspection tools
    check_file "$RES_DIR/bin/g-ir-compiler" "GObject introspection compiler" false
    check_file "$RES_DIR/bin/g-ir-generate" "GObject introspection generator" false
    echo ""
    
    echo -e "${BLUE}📋 Complete Dependency Checklist${NC}"
    echo "Verifying all specified dependencies are present:"
    echo ""
    
    # Core dependencies from your list
    echo -e "${BLUE}Core Dependencies:${NC}"
    check_gtk_library "libgtk-4.dylib"  # gtk4
    check_gtk_library "libadwaita-1.dylib"  # libadwaita
    check_python_package "gi"  # pygobject3 (installed as 'gi' module)
    check_python_package "cairo"  # py3cairo
    check_gtk_library "libvte-2.91-gtk4.dylib"  # vte3
    check_typelib "GObject-2.0.typelib"  # gobject-introspection
    check_dir "$RES_DIR/share/icons/Adwaita" "Adwaita icon theme"  # adwaita-icon-theme
    check_file "$RES_DIR/bin/pkg-config" "pkg-config" false  # pkg-config
    check_gtk_library "libglib-2.0.dylib"  # glib
    check_gtk_library "libgraphene-1.0.dylib"  # graphene
    check_file "$FRAMEWORKS_DIR/libicuuc.dylib" "ICU library" false  # icu4c
    check_file "$RES_DIR/bin/sshpass" "sshpass utility" false  # sshpass
    echo ""
    
    echo -e "${BLUE}📦 Requirements.txt Module Verification${NC}"
    echo "Verifying all modules from requirements.txt are bundled:"
    echo ""
    
    # Read requirements.txt and check each package
    if [ -f "${ROOT_DIR}/requirements.txt" ]; then
        while IFS= read -r line; do
            # Skip comments and empty lines
            if [[ "$line" =~ ^[[:space:]]*# ]] || [[ -z "${line// }" ]]; then
                continue
            fi
            
            # Extract package name (remove version specifiers and platform markers)
            package_name=$(echo "$line" | sed 's/[>=<;].*//' | sed 's/[[:space:]]*$//')
            
            if [ -n "$package_name" ]; then
                # Skip platform-specific packages that shouldn't be on macOS
                if [[ "$line" == *"platform_system"*"Linux"* ]]; then
                    echo -e "${YELLOW}⚠${NC} Skipping Linux-only package: $package_name"
                    continue
                fi
                
                # Check if package is bundled
                TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
                
                # Handle special cases for package names
                local check_name="$package_name"
                case "$package_name" in
                    "PyGObject")
                        # PyGObject is installed as 'gi' module with PyGObject-*.dist-info
                        if [ -d "${RES_DIR}/lib/python3.13/site-packages/gi" ] || \
                           ls "${RES_DIR}/lib/python3.13/site-packages"/PyGObject-*.dist-info >/dev/null 2>&1; then
                            echo -e "${GREEN}✓${NC} requirements.txt: $package_name (as 'gi' module)"
                            PASSED_CHECKS=$((PASSED_CHECKS + 1))
                        else
                            echo -e "${RED}✗${NC} requirements.txt: $package_name - MISSING"
                            FAILED_CHECKS=$((FAILED_CHECKS + 1))
                        fi
                        ;;
                    "pycairo")
                        # pycairo is installed as 'cairo' module
                        if [ -d "${RES_DIR}/lib/python3.13/site-packages/cairo" ] || \
                           [ -f "${RES_DIR}/lib/python3.13/site-packages/cairo.py" ]; then
                            echo -e "${GREEN}✓${NC} requirements.txt: $package_name (as 'cairo' module)"
                            PASSED_CHECKS=$((PASSED_CHECKS + 1))
                        else
                            echo -e "${RED}✗${NC} requirements.txt: $package_name - MISSING"
                            FAILED_CHECKS=$((FAILED_CHECKS + 1))
                        fi
                        ;;
                    *)
                        # Standard package check
                        if [ -d "${RES_DIR}/lib/python3.13/site-packages/${package_name}" ] || \
                           [ -f "${RES_DIR}/lib/python3.13/site-packages/${package_name}.py" ] || \
                           [ -f "${RES_DIR}/lib/python3.13/site-packages/${package_name}.so" ] || \
                           [ -d "${RES_DIR}/lib/python3.13/site-packages/${package_name}.dist-info" ]; then
                            echo -e "${GREEN}✓${NC} requirements.txt: $package_name"
                            PASSED_CHECKS=$((PASSED_CHECKS + 1))
                        else
                            echo -e "${RED}✗${NC} requirements.txt: $package_name - MISSING"
                            FAILED_CHECKS=$((FAILED_CHECKS + 1))
                        fi
                        ;;
                esac
            fi
        done < "${ROOT_DIR}/requirements.txt"
    else
        echo -e "${YELLOW}⚠${NC} requirements.txt not found at ${ROOT_DIR}/requirements.txt"
    fi
    echo ""
    
    echo -e "${BLUE}🎯 Application Resources${NC}"
    check_file "$RES_DIR/sshPilot.icns" "Application icon"
    check_file "$RES_DIR/sshpilot.gresource" "GResource file"
    check_file "$RES_DIR/sshpilot.svg" "SVG icon"
    check_dir "$RES_DIR/share/icons/Adwaita" "Adwaita icon theme"
    check_file "$RES_DIR/share/icons/Adwaita/index.theme" "Adwaita theme index"
    echo ""
    
    echo -e "${BLUE}🖼️ Image Loaders${NC}"
    check_dir "$RES_DIR/lib/gdk-pixbuf-2.0" "gdk-pixbuf directory"
    check_file "$RES_DIR/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" "gdk-pixbuf loaders cache" false
    echo ""
    
    echo -e "${BLUE}🔐 Code Signing${NC}"
    if command -v codesign >/dev/null 2>&1; then
        if codesign --verify --verbose "$APP_DIR" 2>/dev/null; then
            echo -e "${GREEN}✓${NC} Code signature is valid"
            PASSED_CHECKS=$((PASSED_CHECKS + 1))
        else
            echo -e "${YELLOW}⚠${NC} Code signature verification failed or not signed"
            WARNING_CHECKS=$((WARNING_CHECKS + 1))
        fi
        TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    else
        echo -e "${YELLOW}⚠${NC} codesign command not available - skipping signature check"
    fi
    echo ""
    
    echo -e "${BLUE}📊 Bundle Size Analysis${NC}"
    if command -v du >/dev/null 2>&1; then
        BUNDLE_SIZE=$(du -sh "$APP_DIR" 2>/dev/null | cut -f1)
        echo "Bundle size: $BUNDLE_SIZE"
        
        # Check if bundle is reasonably sized (not too small, not too large)
        BUNDLE_SIZE_BYTES=$(du -s "$APP_DIR" 2>/dev/null | cut -f1)
        if [ "$BUNDLE_SIZE_BYTES" -lt 50000 ]; then  # Less than ~50MB
            echo -e "${YELLOW}⚠${NC} Bundle seems small - may be missing dependencies"
            WARNING_CHECKS=$((WARNING_CHECKS + 1))
        elif [ "$BUNDLE_SIZE_BYTES" -gt 2000000 ]; then  # More than ~2GB
            echo -e "${YELLOW}⚠${NC} Bundle seems large - may have unnecessary files"
            WARNING_CHECKS=$((WARNING_CHECKS + 1))
        else
            echo -e "${GREEN}✓${NC} Bundle size is reasonable"
            PASSED_CHECKS=$((PASSED_CHECKS + 1))
        fi
        TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    fi
    echo ""
    
    echo -e "${BLUE}🔍 System Dependency Analysis${NC}"
    echo "Checking for system library dependencies (should be minimal for self-contained bundle):"
    echo ""
    
    # Check main launcher script
    check_system_dependencies "$MACOS_DIR/sshPilot" "Main launcher script"
    
    # Check bundled Python if it exists
    if [ -f "$RES_DIR/python-runtime/python3" ]; then
        check_system_dependencies "$RES_DIR/python-runtime/python3" "Bundled Python runtime"
    else
        echo -e "${YELLOW}⚠${NC} Bundled Python runtime not found - using system Python"
        WARNING_CHECKS=$((WARNING_CHECKS + 1))
        TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    fi
    
    # Check key GTK libraries for system dependencies
    for lib in "libgtk-4" "libadwaita-1" "libvte-2.91-gtk4"; do
        # Check both Frameworks and Resources/lib directories
        lib_path=$(find "$FRAMEWORKS_DIR" "$RES_DIR/lib" -name "${lib}*.dylib" 2>/dev/null | head -1)
        if [ -n "$lib_path" ]; then
            check_system_dependencies "$lib_path" "GTK library: $(basename "$lib_path")"
        fi
    done
    
    # Check for critical system dependencies that should NOT be present
    echo ""
    echo "Checking for problematic system dependencies:"
    
    # Check if any bundled binaries depend on system Python
    local python_deps=0
    for binary in "$MACOS_DIR"/* "$FRAMEWORKS_DIR"/*.dylib; do
        if [ -f "$binary" ] && [ -x "$binary" ]; then
            if otool -L "$binary" 2>/dev/null | grep -q "/opt/homebrew.*python\|/usr/local.*python\|/System.*python"; then
                python_deps=$((python_deps + 1))
                echo -e "${RED}✗${NC} $(basename "$binary") depends on system Python"
                FAILED_CHECKS=$((FAILED_CHECKS + 1))
                TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
            fi
        fi
    done
    
    if [ $python_deps -eq 0 ]; then
        echo -e "${GREEN}✓${NC} No system Python dependencies found"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    fi
    
    # Check for system GTK dependencies
    local gtk_deps=0
    for binary in "$MACOS_DIR"/* "$FRAMEWORKS_DIR"/*.dylib; do
        if [ -f "$binary" ] && [ -x "$binary" ]; then
            if otool -L "$binary" 2>/dev/null | grep -q "/opt/homebrew.*gtk\|/usr/local.*gtk\|/System.*gtk"; then
                gtk_deps=$((gtk_deps + 1))
                echo -e "${RED}✗${NC} $(basename "$binary") depends on system GTK"
                FAILED_CHECKS=$((FAILED_CHECKS + 1))
                TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
            fi
        fi
    done
    
    if [ $gtk_deps -eq 0 ]; then
        echo -e "${GREEN}✓${NC} No system GTK dependencies found"
        PASSED_CHECKS=$((PASSED_CHECKS + 1))
        TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    fi
    
    echo ""
    
    # Summary
    echo -e "${BLUE}📋 Verification Summary${NC}"
    echo "Total checks: $TOTAL_CHECKS"
    echo -e "Passed: ${GREEN}$PASSED_CHECKS${NC}"
    echo -e "Failed: ${RED}$FAILED_CHECKS${NC}"
    echo -e "Warnings: ${YELLOW}$WARNING_CHECKS${NC}"
    echo ""
    
    if [ $FAILED_CHECKS -eq 0 ]; then
        echo -e "${GREEN}🎉 Bundle verification PASSED!${NC}"
        echo "The bundle appears to be complete and ready for distribution."
        exit 0
    else
        echo -e "${RED}❌ Bundle verification FAILED!${NC}"
        echo "Some required components are missing. Please check the build process."
        exit 1
    fi
}

# Run verification
verify_bundle
