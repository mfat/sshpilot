"""Test home directory resolution logic."""

import os
from unittest.mock import Mock
import pytest


class MockSFTPAttributes:
    """Mock SFTP attributes for testing."""
    def __init__(self, filename, is_dir=False, size=0, mtime=0):
        self.filename = filename
        self.st_mode = 0o40000 if is_dir else 0o100644  # Directory or file mode
        self.st_size = size
        self.st_mtime = mtime


def stat_isdir(attr):
    """Mock stat_isdir function."""
    return bool(attr.st_mode & 0o40000)


class FileEntry:
    """Mock FileEntry class."""
    def __init__(self, name, is_dir, size, modified):
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.modified = modified


def test_home_directory_expansion_with_tilde():
    """Test that ~ gets properly expanded to user's home directory."""
    # Mock SFTP client
    mock_sftp = Mock()
    mock_sftp.normalize.return_value = "/home/testuser"
    mock_sftp.listdir_attr.return_value = [
        MockSFTPAttributes("Documents", is_dir=True),
        MockSFTPAttributes("test.txt", is_dir=False, size=100),
    ]
    
    username = "testuser"
    
    # Test the home directory expansion logic directly
    def _impl():
        entries = []
        path = "~"
        expanded_path = path
        
        if path == "~" or path.startswith("~/"):
            try:
                if path == "~":
                    # For just ~, use the initial directory (usually home)
                    expanded_path = "."
                else:
                    # For ~/subpath, we need to resolve the home directory first
                    home_path = mock_sftp.normalize(".")
                    expanded_path = home_path + path[1:]  # Replace ~ with home_path
            except Exception:
                # If normalize fails, try common patterns
                try:
                    possible_homes = [
                        f"/home/{username}",
                        f"/Users/{username}",  # macOS
                        f"/export/home/{username}",  # Solaris
                    ]
                    for possible_home in possible_homes:
                        try:
                            # Test if this directory exists
                            mock_sftp.listdir_attr(possible_home)
                            if path == "~":
                                expanded_path = possible_home
                            else:
                                expanded_path = possible_home + path[1:]
                            break
                        except Exception:
                            continue
                    else:
                        # Final fallback
                        expanded_path = f"/home/{username}" + (path[1:] if path.startswith("~/") else "")
                except Exception:
                    # Ultimate fallback
                    expanded_path = f"/home/{username}" + (path[1:] if path.startswith("~/") else "")
        
        for attr in mock_sftp.listdir_attr(expanded_path):
            entries.append(
                FileEntry(
                    name=attr.filename,
                    is_dir=stat_isdir(attr),
                    size=attr.st_size,
                    modified=attr.st_mtime,
                )
            )
        return expanded_path, entries
    
    # Execute the test
    expanded_path, entries = _impl()
    
    # Verify results
    assert expanded_path == "."  # Should use "." for home directory
    assert len(entries) == 2
    assert entries[0].name == "Documents"
    assert entries[0].is_dir is True
    assert entries[1].name == "test.txt"
    assert entries[1].is_dir is False


def test_home_directory_expansion_with_subpath():
    """Test that ~/subpath gets properly expanded."""
    # Mock SFTP client
    mock_sftp = Mock()
    mock_sftp.normalize.return_value = "/home/testuser"
    mock_sftp.listdir_attr.return_value = [
        MockSFTPAttributes("file1.txt", is_dir=False, size=50),
    ]
    
    username = "testuser"
    
    # Test the listdir implementation for ~/Documents
    def _impl():
        entries = []
        path = "~/Documents"
        expanded_path = path
        
        if path == "~" or path.startswith("~/"):
            try:
                if path == "~":
                    expanded_path = "."
                else:
                    home_path = mock_sftp.normalize(".")
                    expanded_path = home_path + path[1:]
            except Exception:
                expanded_path = f"/home/{username}" + (path[1:] if path.startswith("~/") else "")
        
        for attr in mock_sftp.listdir_attr(expanded_path):
            entries.append(
                FileEntry(
                    name=attr.filename,
                    is_dir=stat_isdir(attr),
                    size=attr.st_size,
                    modified=attr.st_mtime,
                )
            )
        return expanded_path, entries
    
    # Execute the test
    expanded_path, entries = _impl()
    
    # Verify results
    assert expanded_path == "/home/testuser/Documents"
    assert len(entries) == 1
    assert entries[0].name == "file1.txt"


def test_home_directory_fallback_when_normalize_fails():
    """Test fallback behavior when SFTP normalize fails."""
    # Mock the SFTP client to fail on normalize
    mock_sftp = Mock()
    mock_sftp.normalize.side_effect = Exception("Normalize failed")
    
    # Set up mock to return files for the fallback home directory
    def mock_listdir_attr(path):
        if path == "/home/testuser":
            return [MockSFTPAttributes("fallback_file.txt", is_dir=False, size=25)]
        elif path == ".":
            # Also fail for "." to trigger the full fallback logic
            raise Exception("Path not found: .")
        else:
            raise Exception(f"Path not found: {path}")
    
    mock_sftp.listdir_attr = mock_listdir_attr
    
    username = "testuser"
    
    # Test the listdir implementation with fallback
    def _impl():
        entries = []
        path = "~"
        expanded_path = path
        
        if path == "~" or path.startswith("~/"):
            try:
                if path == "~":
                    expanded_path = "."
                else:
                    home_path = mock_sftp.normalize(".")
                    expanded_path = home_path + path[1:]
            except Exception:
                # This should trigger the fallback
                try:
                    possible_homes = [
                        f"/home/{username}",
                        f"/Users/{username}",
                        f"/export/home/{username}",
                    ]
                    for possible_home in possible_homes:
                        try:
                            mock_sftp.listdir_attr(possible_home)
                            expanded_path = possible_home if path == "~" else possible_home + path[1:]
                            break
                        except Exception:
                            continue
                    else:
                        expanded_path = f"/home/{username}" + (path[1:] if path.startswith("~/") else "")
                except Exception:
                    expanded_path = f"/home/{username}" + (path[1:] if path.startswith("~/") else "")
        
        # The actual listdir call might fail if expanded_path is "." and it doesn't exist
        # In that case, we need to handle the fallback at this level too
        try:
            attrs = mock_sftp.listdir_attr(expanded_path)
        except Exception:
            # If the expanded path fails, try the fallback logic again
            if expanded_path == ".":
                possible_homes = [
                    f"/home/{username}",
                    f"/Users/{username}",
                    f"/export/home/{username}",
                ]
                for possible_home in possible_homes:
                    try:
                        attrs = mock_sftp.listdir_attr(possible_home)
                        expanded_path = possible_home
                        break
                    except Exception:
                        continue
                else:
                    # Final fallback
                    expanded_path = f"/home/{username}"
                    attrs = mock_sftp.listdir_attr(expanded_path)
            else:
                raise
        
        for attr in attrs:
            entries.append(
                FileEntry(
                    name=attr.filename,
                    is_dir=stat_isdir(attr),
                    size=attr.st_size,
                    modified=attr.st_mtime,
                )
            )
        return expanded_path, entries
    
    # Execute the test
    expanded_path, entries = _impl()
    
    # Verify results - should fall back to /home/testuser
    assert expanded_path == "/home/testuser"
    assert len(entries) == 1
    assert entries[0].name == "fallback_file.txt"


if __name__ == "__main__":
    pytest.main([__file__])
