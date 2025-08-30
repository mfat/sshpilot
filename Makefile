# Makefile for building sshPilot DMG on macOS
# Supports universal binaries (Intel + Apple Silicon)

APP_NAME = sshPilot
VERSION = 1.0.0
DMG_NAME = $(APP_NAME)-$(VERSION)-universal.dmg

# Directories
DIST_DIR = dist
BUILD_DIR = build
APP_PATH = $(DIST_DIR)/$(APP_NAME).app

# Colors for output
GREEN = \033[0;32m
YELLOW = \033[1;33m
RED = \033[0;31m
NC = \033[0m

.PHONY: all clean deps build dmg verify install help

# Default target
all: clean deps build dmg verify

help:
	@echo "$(GREEN)sshPilot macOS Build System$(NC)"
	@echo ""
	@echo "Available targets:"
	@echo "  $(YELLOW)all$(NC)     - Complete build process (clean, deps, build, dmg)"
	@echo "  $(YELLOW)clean$(NC)   - Clean build artifacts"
	@echo "  $(YELLOW)deps$(NC)    - Install build dependencies"
	@echo "  $(YELLOW)build$(NC)   - Build the macOS app bundle"
	@echo "  $(YELLOW)dmg$(NC)     - Create the DMG file"
	@echo "  $(YELLOW)verify$(NC)  - Verify the DMG"
	@echo "  $(YELLOW)install$(NC) - Install build tools (requires Homebrew)"
	@echo ""
	@echo "Requirements:"
	@echo "  - macOS 11.0 or later"
	@echo "  - Python 3.9 or later"
	@echo "  - Homebrew (for installing tools)"
	@echo ""
	@echo "Output:"
	@echo "  - Universal DMG: $(DIST_DIR)/$(DMG_NAME)"

# Check if we're on macOS
check-macos:
	@if [ "$$(uname)" != "Darwin" ]; then \
		echo "$(RED)Error: This build system requires macOS$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ Running on macOS$(NC)"

# Install build tools
install: check-macos
	@echo "$(YELLOW)Installing build dependencies...$(NC)"
	@if ! command -v brew >/dev/null 2>&1; then \
		echo "$(RED)Error: Homebrew is required$(NC)"; \
		echo "Install from: https://brew.sh"; \
		exit 1; \
	fi
	brew install create-dmg
	python3 -m pip install --upgrade pip setuptools wheel
	@echo "$(GREEN)✓ Build tools installed$(NC)"

# Install Python dependencies
deps: check-macos
	@echo "$(YELLOW)Installing Python dependencies...$(NC)"
	python3 -m pip install -r macos_requirements.txt
	@echo "$(GREEN)✓ Dependencies installed$(NC)"

# Clean build artifacts
clean:
	@echo "$(YELLOW)Cleaning build artifacts...$(NC)"
	rm -rf $(BUILD_DIR) $(DIST_DIR)
	rm -f setup.py *.icns *.iconset
	rm -rf *.egg-info
	@echo "$(GREEN)✓ Clean completed$(NC)"

# Build the app bundle
build: check-macos deps
	@echo "$(YELLOW)Building $(APP_NAME) for macOS...$(NC)"
	python3 build_macos.py
	@if [ ! -d "$(APP_PATH)" ]; then \
		echo "$(RED)✗ App build failed$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ App built: $(APP_PATH)$(NC)"

# Verify universal binary
verify-binary: build
	@echo "$(YELLOW)Verifying universal binary...$(NC)"
	@EXECUTABLE="$(APP_PATH)/Contents/MacOS/$(APP_NAME)"; \
	if [ -f "$$EXECUTABLE" ]; then \
		echo "Binary info:"; \
		file "$$EXECUTABLE"; \
		echo "Architecture info:"; \
		lipo -info "$$EXECUTABLE" 2>/dev/null || echo "lipo info not available"; \
		if lipo -info "$$EXECUTABLE" 2>/dev/null | grep -q "arm64.*x86_64\|x86_64.*arm64"; then \
			echo "$(GREEN)✓ Universal binary confirmed$(NC)"; \
		else \
			echo "$(YELLOW)⚠ Binary may not be universal$(NC)"; \
		fi; \
	else \
		echo "$(RED)✗ Executable not found$(NC)"; \
		exit 1; \
	fi

# Create DMG
dmg: verify-binary
	@echo "$(YELLOW)Creating DMG...$(NC)"
	python3 create_styled_dmg.py
	@if [ ! -f "$(DIST_DIR)/$(DMG_NAME)" ]; then \
		echo "$(RED)✗ DMG creation failed$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ DMG created: $(DIST_DIR)/$(DMG_NAME)$(NC)"

# Verify DMG
verify: dmg
	@echo "$(YELLOW)Verifying DMG...$(NC)"
	@if [ -f "$(DIST_DIR)/$(DMG_NAME)" ]; then \
		hdiutil verify "$(DIST_DIR)/$(DMG_NAME)"; \
		SIZE=$$(du -h "$(DIST_DIR)/$(DMG_NAME)" | cut -f1); \
		echo "$(GREEN)✓ DMG verified successfully$(NC)"; \
		echo "$(GREEN)✓ Size: $$SIZE$(NC)"; \
	else \
		echo "$(RED)✗ DMG file not found$(NC)"; \
		exit 1; \
	fi

# Quick build without verification
quick: clean build dmg

# Show build info
info:
	@echo "$(GREEN)Build Configuration:$(NC)"
	@echo "  App Name: $(APP_NAME)"
	@echo "  Version: $(VERSION)"
	@echo "  DMG Name: $(DMG_NAME)"
	@echo "  Python: $$(python3 --version)"
	@echo "  macOS: $$(sw_vers -productVersion)"
	@echo "  Architecture: $$(uname -m)"
	@echo ""
	@echo "$(GREEN)Build Targets:$(NC)"
	@echo "  App Bundle: $(APP_PATH)"
	@echo "  DMG File: $(DIST_DIR)/$(DMG_NAME)"

# Development target - build and open DMG
dev: dmg
	@echo "$(YELLOW)Opening DMG for testing...$(NC)"
	open "$(DIST_DIR)/$(DMG_NAME)"