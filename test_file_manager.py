#!/usr/bin/env python3
"""
Comprehensive test suite for SFTP file manager.
Tests all upload/download scenarios with the "router" server.

Usage:
    python3 test_file_manager.py [--connection NICKNAME] [--remote-dir PATH] [--verbose]
"""

import sys
import os
import pathlib
import time
import tempfile
import shutil
import argparse
import threading
from typing import List, Tuple, Optional
from concurrent.futures import Future

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gio', '2.0')
from gi.repository import Gtk, Adw, Gio, GLib

from sshpilot.file_manager_window import FileManagerWindow, AsyncSFTPManager
from sshpilot.connection_manager import ConnectionManager
from sshpilot.config import Config

import logging

logger = logging.getLogger(__name__)


class FileManagerTester:
    """Comprehensive test suite for file manager operations"""
    
    def __init__(self, connection_nickname: str = "router", remote_test_dir: str = "/root/gluetun"):
        self.connection_nickname = connection_nickname
        self.remote_test_dir = remote_test_dir
        self.local_test_dir = None
        self.app = None
        self.window = None
        self.manager = None
        self.connection = None
        self.connection_manager = None
        self.test_results = []
        self.errors = []
        
    def setup(self):
        """Set up test environment"""
        logger.info("=" * 80)
        logger.info("Setting up test environment")
        logger.info("=" * 80)
        
        # Create local test directory
        self.local_test_dir = pathlib.Path(tempfile.mkdtemp(prefix="sshpilot_test_"))
        logger.info(f"Local test directory: {self.local_test_dir}")
        
        # Initialize GTK (required for Adw)
        if not Gtk.init_check():
            Gtk.init()
        
        # Load connection manager
        try:
            config = Config()
            self.connection_manager = ConnectionManager(config)
            self.connection = self.connection_manager.find_connection_by_nickname(self.connection_nickname)
            
            if not self.connection:
                raise ValueError(f"Connection '{self.connection_nickname}' not found in SSH config")
            
            logger.info(f"Found connection: {self.connection.nickname}")
            logger.info(f"  Host: {self.connection.hostname or self.connection.host}")
            logger.info(f"  Username: {self.connection.username}")
            logger.info(f"  Port: {self.connection.port}")
            
        except Exception as e:
            logger.error(f"Failed to load connection: {e}")
            raise
        
        # Create file manager window
        try:
            # Create a minimal application for the window
            self.app = Adw.Application(application_id="com.sshpilot.test")
            
            ssh_config = config.get_ssh_config() if hasattr(config, 'get_ssh_config') else None
            self.window = FileManagerWindow(
                application=self.app,
                host=self.connection.hostname or self.connection.host,
                username=self.connection.username,
                port=self.connection.port or 22,
                initial_path="~",
                nickname=self.connection.nickname,
                connection=self.connection,
                connection_manager=self.connection_manager,
                ssh_config=ssh_config
            )
            
            # Connect to server
            logger.info("Connecting to server...")
            self.window._manager.connect_to_server()
            
            # Wait for connection with proper event processing
            max_wait = 30
            waited = 0
            connected = False
            
            # Set up connection signal handler
            def on_connected(manager):
                nonlocal connected
                connected = True
                logger.info("Connection signal received!")
            
            def on_error(manager, error_msg):
                logger.error(f"Connection error: {error_msg}")
            
            self.window._manager.connect("connected", on_connected)
            self.window._manager.connect("connection-error", on_error)
            
            while waited < max_wait:
                self.process_gtk_events()
                
                if connected or self.window._manager._sftp is not None:
                    logger.info("Connected successfully!")
                    break
                
                time.sleep(0.5)
                waited += 0.5
            
            if self.window._manager._sftp is None:
                raise RuntimeError("Failed to connect to server within timeout")
            
            self.manager = self.window._manager
            
        except Exception as e:
            logger.error(f"Failed to create file manager: {e}", exc_info=True)
            raise
    
    def create_test_files(self):
        """Create test files with various names and content"""
        logger.info("=" * 80)
        logger.info("Creating test files")
        logger.info("=" * 80)
        
        test_files = [
            # Simple files
            ("simple_file.txt", "This is a simple test file.\nLine 2\nLine 3"),
            ("test123.txt", "Numeric filename test"),
            
            # Files with spaces
            ("file with spaces.txt", "File with spaces in name"),
            ("multi word file name.log", "Multi-word filename"),
            
            # Files with special characters
            ("file-with-dashes.txt", "File with dashes"),
            ("file_with_underscores.txt", "File with underscores"),
            ("file.with.dots.txt", "File with dots"),
            ("file@special#chars$.txt", "File with special chars @#$"),
            ("file(1).txt", "File with parentheses"),
            ("file[test].txt", "File with brackets"),
            ("file{test}.txt", "File with braces"),
            
            # Files with unicode
            ("file_émojis_🎉.txt", "File with emojis and unicode"),
            ("file_中文.txt", "File with Chinese characters"),
            ("file_русский.txt", "File with Cyrillic"),
            
            # Large file (for progress testing)
            ("large_file.txt", "X" * 100000),  # 100KB
        ]
        
        created_files = []
        for filename, content in test_files:
            file_path = self.local_test_dir / filename
            try:
                file_path.write_text(content, encoding='utf-8')
                created_files.append(file_path)
                logger.info(f"Created: {filename} ({len(content)} bytes)")
            except Exception as e:
                logger.error(f"Failed to create {filename}: {e}")
                self.errors.append(f"Failed to create {filename}: {e}")
        
        # Create test directories
        test_dirs = [
            "simple_dir",
            "dir with spaces",
            "dir-with-dashes",
            "dir_with_underscores",
            "dir.with.dots",
            "dir@special#chars$",
            "dir(1)",
            "dir[test]",
            "dir{test}",
            "dir_émojis_🎉",
            "dir_中文",
        ]
        
        created_dirs = []
        for dirname in test_dirs:
            dir_path = self.local_test_dir / dirname
            try:
                dir_path.mkdir()
                created_dirs.append(dir_path)
                
                # Add a file inside each directory
                test_file = dir_path / "test.txt"
                test_file.write_text(f"Test file in {dirname}")
                
                logger.info(f"Created directory: {dirname}")
            except Exception as e:
                logger.error(f"Failed to create directory {dirname}: {e}")
                self.errors.append(f"Failed to create directory {dirname}: {e}")
        
        logger.info(f"Created {len(created_files)} files and {len(created_dirs)} directories")
        return created_files, created_dirs
    
    def process_gtk_events(self):
        """Process GTK events in the main loop"""
        context = GLib.MainContext.default()
        while context.iteration(False):
            pass
    
    def wait_for_operation(self, future: Future, timeout: int = 60) -> Tuple[bool, Optional[str]]:
        """Wait for an operation to complete"""
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                return False, "Operation timed out"
            time.sleep(0.1)
            self.process_gtk_events()
        
        try:
            future.result()
            return True, None
        except Exception as e:
            return False, str(e)
    
    def test_single_file_upload(self, file_path: pathlib.Path):
        """Test uploading a single file"""
        logger.info(f"Testing single file upload: {file_path.name}")
        try:
            remote_path = f"{self.remote_test_dir}/{file_path.name}"
            future = self.manager.upload(file_path, remote_path)
            success, error = self.wait_for_operation(future)
            
            if success:
                logger.info(f"✓ Single file upload succeeded: {file_path.name}")
                self.test_results.append(("single_file_upload", file_path.name, True, None))
            else:
                logger.error(f"✗ Single file upload failed: {file_path.name} - {error}")
                self.test_results.append(("single_file_upload", file_path.name, False, error))
                self.errors.append(f"Single file upload failed for {file_path.name}: {error}")
        except Exception as e:
            logger.error(f"✗ Single file upload exception: {file_path.name} - {e}", exc_info=True)
            self.test_results.append(("single_file_upload", file_path.name, False, str(e)))
            self.errors.append(f"Single file upload exception for {file_path.name}: {e}")
    
    def test_single_dir_upload(self, dir_path: pathlib.Path):
        """Test uploading a single directory"""
        logger.info(f"Testing single directory upload: {dir_path.name}")
        try:
            remote_path = f"{self.remote_test_dir}/{dir_path.name}"
            future = self.manager.upload_directory(dir_path, remote_path)
            success, error = self.wait_for_operation(future)
            
            if success:
                logger.info(f"✓ Single directory upload succeeded: {dir_path.name}")
                self.test_results.append(("single_dir_upload", dir_path.name, True, None))
            else:
                logger.error(f"✗ Single directory upload failed: {dir_path.name} - {error}")
                self.test_results.append(("single_dir_upload", dir_path.name, False, error))
                self.errors.append(f"Single directory upload failed for {dir_path.name}: {error}")
        except Exception as e:
            logger.error(f"✗ Single directory upload exception: {dir_path.name} - {e}", exc_info=True)
            self.test_results.append(("single_dir_upload", dir_path.name, False, str(e)))
            self.errors.append(f"Single directory upload exception for {dir_path.name}: {e}")
    
    def test_multiple_files_upload(self, file_paths: List[pathlib.Path]):
        """Test uploading multiple files"""
        logger.info(f"Testing multiple files upload: {len(file_paths)} files")
        try:
            futures = []
            for file_path in file_paths:
                remote_path = f"{self.remote_test_dir}/{file_path.name}"
                future = self.manager.upload(file_path, remote_path)
                futures.append((file_path.name, future))
            
            # Wait for all to complete
            all_success = True
            errors = []
            for filename, future in futures:
                success, error = self.wait_for_operation(future)
                if not success:
                    all_success = False
                    errors.append(f"{filename}: {error}")
            
            if all_success:
                logger.info(f"✓ Multiple files upload succeeded: {len(file_paths)} files")
                self.test_results.append(("multiple_files_upload", len(file_paths), True, None))
            else:
                logger.error(f"✗ Multiple files upload failed: {errors}")
                self.test_results.append(("multiple_files_upload", len(file_paths), False, "; ".join(errors)))
                self.errors.append(f"Multiple files upload failed: {errors}")
        except Exception as e:
            logger.error(f"✗ Multiple files upload exception: {e}", exc_info=True)
            self.test_results.append(("multiple_files_upload", len(file_paths), False, str(e)))
            self.errors.append(f"Multiple files upload exception: {e}")
    
    def test_multiple_dirs_upload(self, dir_paths: List[pathlib.Path]):
        """Test uploading multiple directories"""
        logger.info(f"Testing multiple directories upload: {len(dir_paths)} directories")
        try:
            futures = []
            for dir_path in dir_paths:
                remote_path = f"{self.remote_test_dir}/{dir_path.name}"
                future = self.manager.upload_directory(dir_path, remote_path)
                futures.append((dir_path.name, future))
            
            # Wait for all to complete
            all_success = True
            errors = []
            for dirname, future in futures:
                success, error = self.wait_for_operation(future)
                if not success:
                    all_success = False
                    errors.append(f"{dirname}: {error}")
            
            if all_success:
                logger.info(f"✓ Multiple directories upload succeeded: {len(dir_paths)} directories")
                self.test_results.append(("multiple_dirs_upload", len(dir_paths), True, None))
            else:
                logger.error(f"✗ Multiple directories upload failed: {errors}")
                self.test_results.append(("multiple_dirs_upload", len(dir_paths), False, "; ".join(errors)))
                self.errors.append(f"Multiple directories upload failed: {errors}")
        except Exception as e:
            logger.error(f"✗ Multiple directories upload exception: {e}", exc_info=True)
            self.test_results.append(("multiple_dirs_upload", len(dir_paths), False, str(e)))
            self.errors.append(f"Multiple directories upload exception: {e}")
    
    def test_mixed_upload(self, file_paths: List[pathlib.Path], dir_paths: List[pathlib.Path]):
        """Test uploading a mix of files and directories"""
        logger.info(f"Testing mixed upload: {len(file_paths)} files + {len(dir_paths)} directories")
        try:
            futures = []
            
            # Upload files
            for file_path in file_paths:
                remote_path = f"{self.remote_test_dir}/{file_path.name}"
                future = self.manager.upload(file_path, remote_path)
                futures.append((file_path.name, future, "file"))
            
            # Upload directories
            for dir_path in dir_paths:
                remote_path = f"{self.remote_test_dir}/{dir_path.name}"
                future = self.manager.upload_directory(dir_path, remote_path)
                futures.append((dir_path.name, future, "dir"))
            
            # Wait for all to complete
            all_success = True
            errors = []
            for name, future, ftype in futures:
                success, error = self.wait_for_operation(future)
                if not success:
                    all_success = False
                    errors.append(f"{name} ({ftype}): {error}")
            
            if all_success:
                logger.info(f"✓ Mixed upload succeeded: {len(file_paths)} files + {len(dir_paths)} directories")
                self.test_results.append(("mixed_upload", f"{len(file_paths)}f+{len(dir_paths)}d", True, None))
            else:
                logger.error(f"✗ Mixed upload failed: {errors}")
                self.test_results.append(("mixed_upload", f"{len(file_paths)}f+{len(dir_paths)}d", False, "; ".join(errors)))
                self.errors.append(f"Mixed upload failed: {errors}")
        except Exception as e:
            logger.error(f"✗ Mixed upload exception: {e}", exc_info=True)
            self.test_results.append(("mixed_upload", f"{len(file_paths)}f+{len(dir_paths)}d", False, str(e)))
            self.errors.append(f"Mixed upload exception: {e}")
    
    def test_download(self, remote_filename: str):
        """Test downloading a file"""
        logger.info(f"Testing download: {remote_filename}")
        try:
            remote_path = f"{self.remote_test_dir}/{remote_filename}"
            local_path = self.local_test_dir / f"downloaded_{remote_filename}"
            
            future = self.manager.download(remote_path, local_path)
            success, error = self.wait_for_operation(future)
            
            if success and local_path.exists():
                logger.info(f"✓ Download succeeded: {remote_filename}")
                self.test_results.append(("download", remote_filename, True, None))
            else:
                logger.error(f"✗ Download failed: {remote_filename} - {error}")
                self.test_results.append(("download", remote_filename, False, error))
                self.errors.append(f"Download failed for {remote_filename}: {error}")
        except Exception as e:
            logger.error(f"✗ Download exception: {remote_filename} - {e}", exc_info=True)
            self.test_results.append(("download", remote_filename, False, str(e)))
            self.errors.append(f"Download exception for {remote_filename}: {e}")
    
    def verify_remote_file(self, filename: str) -> bool:
        """Verify a file exists on the remote server"""
        try:
            remote_path = f"{self.remote_test_dir}/{filename}"
            future = self.manager.path_exists(remote_path)
            success, error = self.wait_for_operation(future, timeout=10)
            if success:
                exists = future.result()
                return exists
            return False
        except Exception as e:
            logger.error(f"Failed to verify remote file {filename}: {e}")
            return False
    
    def run_all_tests(self):
        """Run all test scenarios"""
        logger.info("=" * 80)
        logger.info("Starting comprehensive test suite")
        logger.info("=" * 80)
        
        # Create test files
        files, dirs = self.create_test_files()
        
        if not files and not dirs:
            logger.error("No test files created, aborting tests")
            return
        
        # Test 1: Single file uploads
        logger.info("\n" + "=" * 80)
        logger.info("TEST 1: Single File Uploads")
        logger.info("=" * 80)
        for file_path in files[:5]:  # Test first 5 files
            self.test_single_file_upload(file_path)
            time.sleep(0.5)  # Small delay between uploads
        
        # Test 2: Single directory uploads
        logger.info("\n" + "=" * 80)
        logger.info("TEST 2: Single Directory Uploads")
        logger.info("=" * 80)
        for dir_path in dirs[:3]:  # Test first 3 directories
            self.test_single_dir_upload(dir_path)
            time.sleep(0.5)
        
        # Test 3: Multiple files upload
        logger.info("\n" + "=" * 80)
        logger.info("TEST 3: Multiple Files Upload (simultaneous)")
        logger.info("=" * 80)
        self.test_multiple_files_upload(files[5:10])  # Test next 5 files
        
        # Test 4: Multiple directories upload
        logger.info("\n" + "=" * 80)
        logger.info("TEST 4: Multiple Directories Upload (simultaneous)")
        logger.info("=" * 80)
        self.test_multiple_dirs_upload(dirs[3:6])  # Test next 3 directories
        
        # Test 5: Mixed upload
        logger.info("\n" + "=" * 80)
        logger.info("TEST 5: Mixed Upload (files + directories)")
        logger.info("=" * 80)
        self.test_mixed_upload(files[10:13], dirs[6:8])
        
        # Test 6: Downloads
        logger.info("\n" + "=" * 80)
        logger.info("TEST 6: File Downloads")
        logger.info("=" * 80)
        # Download some uploaded files
        for file_path in files[:3]:
            if self.verify_remote_file(file_path.name):
                self.test_download(file_path.name)
                time.sleep(0.5)
        
        # Print summary
        self.print_summary()
    
    def print_summary(self):
        """Print test summary"""
        logger.info("\n" + "=" * 80)
        logger.info("TEST SUMMARY")
        logger.info("=" * 80)
        
        total = len(self.test_results)
        passed = sum(1 for _, _, success, _ in self.test_results if success)
        failed = total - passed
        
        logger.info(f"Total tests: {total}")
        logger.info(f"Passed: {passed}")
        logger.info(f"Failed: {failed}")
        
        if failed > 0:
            logger.info("\nFailed tests:")
            for test_type, name, success, error in self.test_results:
                if not success:
                    logger.info(f"  - {test_type}: {name}")
                    if error:
                        logger.info(f"    Error: {error}")
        
        if self.errors:
            logger.info(f"\nTotal errors: {len(self.errors)}")
            for error in self.errors[:10]:  # Show first 10 errors
                logger.error(f"  - {error}")
    
    def cleanup(self):
        """Clean up test environment"""
        logger.info("Cleaning up...")
        if self.local_test_dir and self.local_test_dir.exists():
            shutil.rmtree(self.local_test_dir)
            logger.info(f"Removed local test directory: {self.local_test_dir}")
        
        if self.window and self.window._manager:
            try:
                self.window._manager.close()
            except:
                pass


def main():
    """Main test runner"""
    parser = argparse.ArgumentParser(description="Test SFTP file manager")
    parser.add_argument("--connection", default="router", help="Connection nickname (default: router)")
    parser.add_argument("--remote-dir", default="/root/gluetun", help="Remote test directory (default: /root/gluetun)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()
    
    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    tester = FileManagerTester(
        connection_nickname=args.connection,
        remote_test_dir=args.remote_dir
    )
    
    try:
        tester.setup()
        tester.run_all_tests()
    except KeyboardInterrupt:
        logger.info("Tests interrupted by user")
    except Exception as e:
        logger.error(f"Test suite failed: {e}", exc_info=True)
    finally:
        tester.cleanup()
    
    # Exit with error code if tests failed
    if tester.errors:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()

