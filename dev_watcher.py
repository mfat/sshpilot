#!/usr/bin/env python3
"""
Development file watcher for sshPilot
Monitors Python files and UI resources for changes and restarts the application
"""

import os
import sys
import time
import signal
import subprocess
import threading
import logging
from pathlib import Path
from typing import Set, Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileMovedEvent

# Add the project root to Python path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

class SshPilotFileHandler(FileSystemEventHandler):
    """File system event handler for sshPilot development"""
    
    def __init__(self, restart_callback):
        self.restart_callback = restart_callback
        self.last_restart = 0
        self.restart_delay = 1.0  # Minimum delay between restarts
        self.ignored_extensions = {'.pyc', '.pyo', '.pyd', '.so', '.dylib', '.dll'}
        self.ignored_dirs = {'__pycache__', '.git', 'venv', '.pytest_cache', 'build', 'dist', 'sshpilot.egg-info'}
        
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
            
        # Ignore hidden files
        if path.name.startswith('.'):
            return True
            
        return False
    
    def is_python_file(self, file_path: str) -> bool:
        """Check if the file is a Python file"""
        return Path(file_path).suffix == '.py'
    
    def is_ui_file(self, file_path: str) -> bool:
        """Check if the file is a UI-related file"""
        path = Path(file_path)
        ui_extensions = {'.glade', '.ui', '.xml', '.css', '.gresource'}
        ui_dirs = {'resources', 'ui', 'styles'}
        
        # Check file extension
        if path.suffix in ui_extensions:
            return True
            
        # Check if in UI directory
        for part in path.parts:
            if part in ui_dirs:
                return True
                
        return False
    
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
        """Handle file change events"""
        if self.should_ignore(file_path):
            return
            
        current_time = time.time()
        if current_time - self.last_restart < self.restart_delay:
            return
            
        # Check if it's a relevant file
        if self.is_python_file(file_path) or self.is_ui_file(file_path):
            print(f"\n🔄 File changed: {file_path}")
            print("   Restarting sshPilot...")
            
            self.last_restart = current_time
            self.restart_callback()

class SshPilotDevWatcher:
    """Main development watcher class"""
    
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.observer: Optional[Observer] = None
        self.running = False
        self.restart_lock = threading.Lock()
        
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('sshpilot-dev')
        
    def start_application(self):
        """Start the sshPilot application"""
        if self.process and self.process.poll() is None:
            self.logger.info("Application already running")
            return
            
        try:
            self.logger.info("Starting sshPilot application...")
            self.process = subprocess.Popen([
                sys.executable, 'run.py', '--verbose'
            ], cwd=PROJECT_ROOT)
            self.logger.info(f"Application started with PID: {self.process.pid}")
        except Exception as e:
            self.logger.error(f"Failed to start application: {e}")
    
    def stop_application(self):
        """Stop the sshPilot application"""
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
    
    def start_watching(self):
        """Start watching for file changes"""
        if self.observer and self.observer.is_alive():
            return
            
        self.logger.info("Starting file watcher...")
        
        # Create observer and handler
        self.observer = Observer()
        handler = SshPilotFileHandler(self.restart_application)
        
        # Watch the sshpilot directory and root files
        watch_paths = [
            str(PROJECT_ROOT / 'sshpilot'),
            str(PROJECT_ROOT / 'run.py'),
            str(PROJECT_ROOT / 'requirements.txt'),
        ]
        
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
    
    def run(self):
        """Main run loop"""
        self.running = True
        
        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            self.logger.info("Received shutdown signal, cleaning up...")
            self.running = False
            self.stop_application()
            self.stop_watching()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Start the application
            self.start_application()
            
            # Start watching for changes
            self.start_watching()
            
            print("\n🚀 sshPilot development mode started!")
            print("   Watching for file changes...")
            print("   Press Ctrl+C to stop")
            print("   The application will restart automatically when files change\n")
            
            # Keep the main thread alive
            while self.running:
                time.sleep(1)
                
                # Check if application is still running
                if self.process and self.process.poll() is not None:
                    self.logger.warning("Application stopped unexpectedly, restarting...")
                    self.start_application()
                    
        except KeyboardInterrupt:
            self.logger.info("Received keyboard interrupt")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
        finally:
            self.stop_application()
            self.stop_watching()
            self.logger.info("Development watcher stopped")

def main():
    """Main entry point"""
    print("🔧 sshPilot Development Watcher")
    print("=" * 40)
    
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
    
    # Start the development watcher
    watcher = SshPilotDevWatcher()
    watcher.run()

if __name__ == '__main__':
    main()
