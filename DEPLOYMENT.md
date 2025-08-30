# Deployment Scripts

This document explains how to use the deployment scripts for the sshPilot website.

## Quick Deploy

### Basic Deployment
```bash
./deploy.sh
```

This script will:
- Check for uncommitted changes
- Switch to dev branch
- Pull latest changes
- Merge dev into main
- Push to main branch
- Switch back to dev branch

### Deployment with Screenshot Updates
```bash
./deploy-with-screenshots.sh
```

This script will:
- Update screenshots automatically (if `update_screenshots.py` exists)
- Commit screenshot changes
- Perform the same deployment steps as `deploy.sh`

## Manual Steps

If you prefer to deploy manually:

### 1. Update Screenshots (Optional)
```bash
python3 update_screenshots.py
```

### 2. Commit Changes
```bash
git add .
git commit -m "Your commit message"
```

### 3. Deploy to Main
```bash
git checkout main
git merge dev
git push origin main
git checkout dev
```

## Script Features

### Safety Checks
- ✅ Verifies you're in a git repository
- ✅ Checks for uncommitted changes
- ✅ Ensures dev branch exists
- ✅ Handles merge conflicts gracefully

### User Experience
- 🎨 Colored output for better readability
- 📝 Clear status messages
- ⚠️ Helpful warnings and error messages
- 🔄 Automatic branch switching

### Automation
- 🔄 Automatic screenshot updates
- 📝 Auto-commit of screenshot changes
- 🚀 One-command deployment

## Troubleshooting

### "Not in a git repository"
Run the script from the project root directory.

### "You have uncommitted changes"
Commit your changes first:
```bash
git add .
git commit -m "Your commit message"
```

### "Merge failed"
Resolve conflicts manually and try again:
```bash
git status  # Check what needs to be resolved
# Edit conflicted files
git add .
git commit
./deploy.sh  # Try again
```

### "Failed to push to main branch"
Check your GitHub permissions and try again.

## Website URL

After successful deployment, your website will be available at:
**https://mfat.github.io/sshpilot/**

The deployment typically takes 1-3 minutes to complete.
