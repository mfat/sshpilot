#!/usr/bin/env python3
"""
Simple development runner for sshPilot with hot reloading
A more stable version with reduced complexity
"""

import os
import sys
import time
import signal
import subprocess
import threading
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Add the project root to Python path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

class SimpleFileHandler(FileSystemEventHandler):
    """Simple file system event handler"""
    
    def __init__(self, restart_callback):
        self.restart_callback = restart_callback
        self.last_restart = 0
        self.restart_delay = 3.0  # 3 second delay between restarts
        
    def should_ignore(self, file_path: str) -> bool:
        """Check if a file should be ignored"""
        path = Path(file_path)
        
        # Ignore common build/cache directories
        ignored_dirs = {'__pycache__', '.git', 'venv', '.pytest_cache', 'build', 'dist', 'sshpilot.egg-info'}
        for part in path.parts:
            if part in ignored_dirs:
                return True
                
        # Ignore common build files
        ignored_extensions = {'.pyc', '.pyo', '.pyd', '.so', '.dylib', '.dll', '.log'}
        if path.suffix in ignored_extensions:
            return True
            
        # Ignore hidden files
        if path.name.startswith('.'):
            return True
            
        return False
    
    def on_modified(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            self.handle_file_change(event.src_path)
    
    def on_created(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            self.handle_file_change(event.src_path)
    
    def handle_file_change(self, file_path: str):
        """Handle file change events"""
        current_time = time.time()
        if current_time - self.last_restart < self.restart_delay:
            return
            
        # Only restart for Python files
        if file_path.endswith('.py'):
            print(f"\n🔄 Python file changed: {file_path}")
            print("   Restarting sshPilot...")
            self.last_restart = current_time
            self.restart_callback()

class SimpleDevRunner:
    """Simple development runner"""
    
    def __init__(self):
        self.process = None
        self.observer = None
        self.running = False
        self.restart_lock = threading.Lock()
        
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('sshpilot-dev')
        
        # Reduce watchdog verbosity
        logging.getLogger('watchdog.observers.inotify_buffer').setLevel(logging.WARNING)
        
    def start_application(self):
        """Start the sshPilot application"""
        if self.process and self.process.poll() is None:
            return
            
        try:
            # Set environment variables to reduce noise
            env = os.environ.copy()
            env['G_MESSAGES_DEBUG'] = '0'
            env['GTK_THEME'] = 'Adwaita'
            
            # Use the virtual environment Python if available
            venv_python = PROJECT_ROOT / 'venv' / 'bin' / 'python'
            if venv_python.exists():
                python_cmd = str(venv_python)
                self.logger.info("Using virtual environment Python")
            else:
                python_cmd = sys.executable
                self.logger.info("Using system Python")
            
            self.logger.info("Starting sshPilot application...")
            self.process = subprocess.Popen(
                [python_cmd, 'run.py', '--verbose'],
                cwd=PROJECT_ROOT,
                env=env
            )
            self.logger.info(f"Application started with PID: {self.process.pid}")
        except Exception as e:
            self.logger.error(f"Failed to start application: {e}")
    
    def stop_application(self):
        """Stop the sshPilot application"""
        if self.process and self.process.poll() is None:
            try:
                self.logger.info("Stopping application...")
                self.process.terminate()
                self.process.wait(timeout=5)
                self.logger.info("Application stopped")
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception as e:
                self.logger.error(f"Error stopping application: {e}")
        self.process = None
    
    def restart_application(self):
        """Restart the application"""
        with self.restart_lock:
            self.logger.info("Restarting application...")
            self.stop_application()
            time.sleep(1)  # Brief pause
            self.start_application()
    
    def start_watching(self):
        """Start watching for file changes"""
        if self.observer and self.observer.is_alive():
            return
            
        self.logger.info("Starting file watcher...")
        
        self.observer = Observer()
        handler = SimpleFileHandler(self.restart_application)
        
        # Watch only the sshpilot directory
        watch_path = str(PROJECT_ROOT / 'sshpilot')
        if os.path.exists(watch_path):
            self.observer.schedule(handler, watch_path, recursive=True)
            self.logger.info(f"Watching: {watch_path}")
        
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
        
        # Set up signal handlers
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
            print("🔧 sshPilot Simple Development Mode")
            print("=" * 40)
            print("🚀 Starting sshPilot with hot reloading...")
            print("   • Python files (.py) → Auto restart")
            print("   • Press Ctrl+C to stop")
            print()
            
            # Start the application
            self.start_application()
            
            # Start watching for changes
            self.start_watching()
            
            print("🔍 Watching for changes...")
            print("   Make changes to Python files to see hot reloading in action!")
            print()
            
            # Keep the main thread alive
            while self.running:
                time.sleep(1)
                
                # Check if application is still running
                if self.process and self.process.poll() is not None:
                    exit_code = self.process.returncode
                    if exit_code != 0:
                        self.logger.warning(f"Application crashed with exit code {exit_code}, restarting...")
                        self.start_application()
                    else:
                        self.logger.info("Application stopped normally")
                        break
                    
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
    # Check if watchdog is available
    try:
        import watchdog
    except ImportError:
        print("❌ Error: 'watchdog' package is required")
        print("   Install it with: pip install watchdog")
        sys.exit(1)
    
    # Check if we're in the right directory
    if not os.path.exists('run.py'):
        print("❌ Error: run.py not found. Please run from sshPilot root directory")
        sys.exit(1)
    
    # Start the development runner
    runner = SimpleDevRunner()
    runner.run()

if __name__ == '__main__':
    main()
