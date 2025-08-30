#!/bin/bash

# sshPilot Website Deployment Script with Screenshot Updates
# This script updates screenshots, commits changes, and deploys to main

set -e  # Exit on any error

echo "🚀 Starting sshPilot website deployment with screenshot updates..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    print_error "Not in a git repository. Please run this script from the project root."
    exit 1
fi

# Check current branch
current_branch=$(git branch --show-current)
print_status "Current branch: $current_branch"

# Check if we're on dev branch
if [ "$current_branch" != "dev" ]; then
    print_status "Switching to dev branch..."
    git checkout dev
fi

# Update screenshots if the script exists
if [ -f "update_screenshots.py" ]; then
    print_status "Updating screenshots..."
    if python3 update_screenshots.py; then
        print_success "Screenshots updated successfully"
        
        # Check if screenshots.js was modified
        if ! git diff-index --quiet HEAD -- docs/screenshots.js; then
            print_status "Committing screenshot updates..."
            git add docs/screenshots.js
            git commit -m "Auto-update screenshots from directory"
        fi
    else
        print_warning "Screenshot update failed, continuing with deployment..."
    fi
else
    print_warning "update_screenshots.py not found, skipping screenshot update"
fi

# Check if there are uncommitted changes
if ! git diff-index --quiet HEAD --; then
    print_warning "You have uncommitted changes. Please commit them first:"
    echo "  git add ."
    echo "  git commit -m 'Your commit message'"
    exit 1
fi

# Check if dev branch exists
if ! git show-ref --verify --quiet refs/heads/dev; then
    print_error "Dev branch does not exist. Please create it first."
    exit 1
fi

# Pull latest changes from remote
print_status "Pulling latest changes from remote..."
git pull origin dev

# Check if main branch exists locally
if ! git show-ref --verify --quiet refs/heads/main; then
    print_status "Creating local main branch from origin/main..."
    git checkout -b main origin/main
fi

# Switch to main branch
print_status "Switching to main branch..."
git checkout main

# Pull latest changes from remote main
print_status "Pulling latest changes from remote main..."
git pull origin main

# Merge dev into main
print_status "Merging dev into main..."
if git merge dev --no-edit; then
    print_success "Merge completed successfully"
else
    print_error "Merge failed. Please resolve conflicts manually and try again."
    exit 1
fi

# Push to remote main
print_status "Pushing to remote main..."
if git push origin main; then
    print_success "Successfully pushed to main branch"
else
    print_error "Failed to push to main branch"
    exit 1
fi

# Switch back to dev branch
print_status "Switching back to dev branch..."
git checkout dev

print_success "🎉 Deployment completed successfully!"
print_status "Your website will be updated in a few minutes at: https://mfat.github.io/sshpilot/"
print_status "You can check the deployment status in the Actions tab on GitHub."
