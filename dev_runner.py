#!/usr/bin/env python3
"""
Advanced development runner for sshPilot with hot reloading
Supports Python files, CSS, and UI resource changes
"""

import os
import sys
import time
import signal
import subprocess
import threading
import logging
import json
from pathlib import Path
from typing import Set, Optional, Dict, Any
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileMovedEvent

# Add the project root to Python path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

class SshPilotDevHandler(FileSystemEventHandler):
    """Advanced file system event handler for sshPilot development"""
    
    def __init__(self, restart_callback, css_reload_callback):
        self.restart_callback = restart_callback
        self.css_reload_callback = css_reload_callback
        self.last_restart = 0
        self.last_css_reload = 0
        self.restart_delay = 2.0  # Increased delay to prevent excessive restarts
        self.css_reload_delay = 1.0
        
        # File type patterns
        self.python_extensions = {'.py'}
        self.css_extensions = {'.css', '.scss', '.sass'}
        self.ui_extensions = {'.glade', '.ui', '.xml', '.gresource'}
        self.config_extensions = {'.json', '.yaml', '.yml', '.toml', '.ini'}
        
        # Directories to watch
        self.watch_dirs = {
            'sshpilot': 'python',
            'resources': 'ui',
            'tests': 'python',
            'documentation': 'config'
        }
        
        # Ignored patterns
        self.ignored_extensions = {'.pyc', '.pyo', '.pyd', '.so', '.dylib', '.dll', '.log'}
        self.ignored_dirs = {
            '__pycache__', '.git', 'venv', '.pytest_cache', 'build', 'dist', 
            'sshpilot.egg-info', '.mypy_cache', '.coverage'
        }
        
    def should_ignore(self, file_path: str) -> bool:
        """Check if a file should be ignored"""
        path = Path(file_path)
        
        # Check if any parent directory should be ignored
        for part in path.parts:
            if part in self.ignored_dirs:
                return True
                
        # Check file extension
        if path.suffix in self.ignored_extensions:
            return True
            
        # Ignore hidden files (except .gitignore, etc.)
        if path.name.startswith('.') and path.name not in {'.gitignore', '.gitattributes'}:
            return True
            
        return False
    
    def get_file_type(self, file_path: str) -> str:
        """Determine the type of file for appropriate handling"""
        path = Path(file_path)
        
        if path.suffix in self.python_extensions:
            return 'python'
        elif path.suffix in self.css_extensions:
            return 'css'
        elif path.suffix in self.ui_extensions:
            return 'ui'
        elif path.suffix in self.config_extensions:
            return 'config'
        else:
            return 'other'
    
    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent) and not event.is_directory:
            self.handle_file_change(event.src_path)
    
    def on_created(self, event):
        if isinstance(event, FileCreatedEvent) and not event.is_directory:
            self.handle_file_change(event.src_path)
    
    def on_moved(self, event):
        if isinstance(event, FileMovedEvent) and not event.is_directory:
            self.handle_file_change(event.dest_path)
    
    def handle_file_change(self, file_path: str):
        """Handle file change events with appropriate action"""
        if self.should_ignore(file_path):
            return
            
        file_type = self.get_file_type(file_path)
        current_time = time.time()
        
        if file_type == 'python':
            if current_time - self.last_restart < self.restart_delay:
                return
            print(f"\n🐍 Python file changed: {file_path}")
            print("   Restarting sshPilot...")
            self.last_restart = current_time
            self.restart_callback()
            
        elif file_type == 'css':
            if current_time - self.last_css_reload < self.css_reload_delay:
                return
            print(f"\n🎨 CSS file changed: {file_path}")
            print("   Reloading styles...")
            self.last_css_reload = current_time
            self.css_reload_callback()
            
        elif file_type == 'ui':
            if current_time - self.last_restart < self.restart_delay:
                return
            print(f"\n🖼️  UI file changed: {file_path}")
            print("   Restarting sshPilot...")
            self.last_restart = current_time
            self.restart_callback()
            
        elif file_type == 'config':
            if current_time - self.last_restart < self.restart_delay:
                return
            print(f"\n⚙️  Config file changed: {file_path}")
            print("   Restarting sshPilot...")
            self.last_restart = current_time
            self.restart_callback()

class SshPilotDevRunner:
    """Advanced development runner with hot reloading"""
    
    def __init__(self, verbose: bool = True, isolated: bool = False):
        self.process: Optional[subprocess.Popen] = None
        self.observer: Optional[Observer] = None
        self.running = False
        self.restart_lock = threading.Lock()
        self.verbose = verbose
        self.isolated = isolated
        
        # Set up logging
        log_level = logging.DEBUG if verbose else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('sshpilot-dev')
        
        # Reduce watchdog verbosity
        logging.getLogger('watchdog.observers.inotify_buffer').setLevel(logging.WARNING)
        
        # CSS reloading state
        self.css_provider = None
        self.css_file_path = None
        
    def start_application(self):
        """Start the sshPilot application"""
        if self.process and self.process.poll() is None:
            self.logger.info("Application already running")
            return
            
        try:
            cmd = [sys.executable, 'run.py']
            if self.verbose:
                cmd.append('--verbose')
            if self.isolated:
                cmd.append('--isolated')
                
            self.logger.info("Starting sshPilot application...")
            # Set environment variables to reduce noise
            env = os.environ.copy()
            env['G_MESSAGES_DEBUG'] = '0'  # Reduce GTK debug messages
            env['GTK_THEME'] = 'Adwaita'   # Use default theme to avoid parsing errors
            
            self.process = subprocess.Popen(
                cmd, 
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            self.logger.info(f"Application started with PID: {self.process.pid}")
        except Exception as e:
            self.logger.error(f"Failed to start application: {e}")
    
    def stop_application(self):
        """Stop the sshPilot application gracefully"""
        if self.process and self.process.poll() is None:
            try:
                self.logger.info("Stopping sshPilot application...")
                self.process.terminate()
                
                # Wait for graceful shutdown
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.logger.warning("Application didn't stop gracefully, forcing...")
                    self.process.kill()
                    self.process.wait()
                    
                self.logger.info("Application stopped")
            except Exception as e:
                self.logger.error(f"Error stopping application: {e}")
        self.process = None
    
    def restart_application(self):
        """Restart the sshPilot application"""
        with self.restart_lock:
            self.logger.info("Restarting application...")
            self.stop_application()
            time.sleep(0.5)  # Brief pause between stop and start
            self.start_application()
    
    def reload_css(self):
        """Reload CSS styles without restarting the application"""
        try:
            # This would need to be implemented in the main application
            # to support CSS reloading without restart
            self.logger.info("CSS reloading not yet implemented - restarting application")
            self.restart_application()
        except Exception as e:
            self.logger.error(f"Failed to reload CSS: {e}")
    
    def start_watching(self):
        """Start watching for file changes"""
        if self.observer and self.observer.is_alive():
            return
            
        self.logger.info("Starting file watcher...")
        
        # Create observer and handler
        self.observer = Observer()
        handler = SshPilotDevHandler(self.restart_application, self.reload_css)
        
        # Watch relevant directories
        watch_paths = [
            str(PROJECT_ROOT / 'sshpilot'),
            str(PROJECT_ROOT / 'run.py'),
            str(PROJECT_ROOT / 'requirements.txt'),
        ]
        
        # Add resources directory if it exists
        resources_path = PROJECT_ROOT / 'sshpilot' / 'resources'
        if resources_path.exists():
            watch_paths.append(str(resources_path))
        
        for path in watch_paths:
            if os.path.exists(path):
                self.observer.schedule(handler, path, recursive=True)
                self.logger.info(f"Watching: {path}")
        
        self.observer.start()
        self.logger.info("File watcher started")
    
    def stop_watching(self):
        """Stop watching for file changes"""
        if self.observer:
            self.logger.info("Stopping file watcher...")
            self.observer.stop()
            self.observer.join()
            self.observer = None
            self.logger.info("File watcher stopped")
    
    def print_banner(self):
        """Print a nice banner for the development mode"""
        banner = """
╔══════════════════════════════════════════════════════════════╗
║                    🚀 sshPilot Dev Mode                     ║
╠══════════════════════════════════════════════════════════════╣
║  • Hot reloading enabled for Python files                   ║
║  • CSS and UI file watching active                          ║
║  • Automatic application restart on changes                 ║
║  • Press Ctrl+C to stop                                     ║
╚══════════════════════════════════════════════════════════════╝
        """
        print(banner)
    
    def run(self):
        """Main run loop"""
        self.running = True
        
        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            self.logger.info("Received shutdown signal, cleaning up...")
            self.running = False
            self.stop_application()
            self.stop_watching()
            print("\n👋 Development mode stopped. Goodbye!")
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Print banner
            self.print_banner()
            
            # Start the application
            self.start_application()
            
            # Start watching for changes
            self.start_watching()
            
            print("🔍 Watching for changes...")
            print("   • Python files (.py) → Full restart")
            print("   • CSS files (.css) → Style reload")
            print("   • UI files (.ui, .xml) → Full restart")
            print("   • Config files (.json, .toml) → Full restart")
            print()
            
            # Keep the main thread alive
            while self.running:
                time.sleep(1)
                
                # Check if application is still running
                if self.process and self.process.poll() is not None:
                    exit_code = self.process.returncode
                    if exit_code != 0:
                        self.logger.warning(f"Application stopped unexpectedly with exit code {exit_code}, restarting...")
                    else:
                        self.logger.info("Application stopped normally")
                    self.start_application()
                    
        except KeyboardInterrupt:
            self.logger.info("Received keyboard interrupt")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
        finally:
            self.stop_application()
            self.stop_watching()
            self.logger.info("Development runner stopped")

def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="sshPilot Development Runner with Hot Reloading")
    parser.add_argument("--verbose", "-v", action="store_true", 
                       help="Enable verbose debug logging")
    parser.add_argument("--isolated", action="store_true", 
                       help="Use isolated SSH configuration")
    parser.add_argument("--no-watch", action="store_true", 
                       help="Disable file watching (run once)")
    
    args = parser.parse_args()
    
    # Check if watchdog is available
    try:
        import watchdog
    except ImportError:
        print("❌ Error: 'watchdog' package is required for file watching")
        print("   Install it with: pip install watchdog")
        sys.exit(1)
    
    # Check if we're in the right directory
    if not os.path.exists('run.py'):
        print("❌ Error: run.py not found. Please run this script from the sshPilot root directory")
        sys.exit(1)
    
    # Start the development runner
    runner = SshPilotDevRunner(verbose=args.verbose, isolated=args.isolated)
    
    if args.no_watch:
        print("🔧 Running sshPilot once (no file watching)")
        runner.start_application()
        try:
            while runner.process and runner.process.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            runner.stop_application()
    else:
        runner.run()

if __name__ == '__main__':
    main()
