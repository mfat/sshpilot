# Port Information Integration in sshPilot

This document describes the integration of port information functionality (inspired by ports-info) into sshPilot to improve port forwarding error handling and user experience.

## Overview

The integration adds comprehensive port conflict detection and resolution capabilities to sshPilot's port forwarding features. Instead of relying solely on SSH's error messages when ports are already in use, sshPilot now proactively checks for conflicts and provides user-friendly feedback.

## Features Added

### 1. Port Availability Checking
- **Real-time port conflict detection** during port forwarding rule creation
- **Pre-flight checks** before establishing SSH connections with port forwarding
- **Cross-platform support** using psutil (with netstat fallback)

### 2. Enhanced User Interface
- **Port Information Dialog** - View all listening ports with process information
- **Real-time validation** in port forwarding rule dialogs
- **Conflict warnings** with suggested alternative ports
- **Process identification** showing which application is using conflicting ports

### 3. Improved Error Handling
- **Proactive conflict detection** before SSH connection attempts
- **User-friendly error messages** instead of raw SSH output
- **Alternative port suggestions** when conflicts are detected
- **Graceful degradation** when port information is unavailable

## Architecture

### Core Components

#### 1. `port_utils.py` - Port Information Module
```python
# Main classes:
- PortInfo: Information about a port and its usage
- PortChecker: Port availability and information checker

# Key functions:
- is_port_available(port, address): Check if a port is available
- get_listening_ports(): Get all currently listening ports
- find_available_port(preferred_port): Find alternative available port
- check_port_conflicts(ports): Check for conflicts with a list of ports
```

#### 2. Enhanced Connection Dialog (`connection_dialog.py`)
- **Port conflict validation**: Real-time checking when adding/editing forwarding rules
- **Port Information Dialog**: "View Port Info" button shows system-wide port usage
- **Integrated UI**: Port checking built directly into the existing forwarding rule editor

#### 3. Terminal Integration (`terminal.py`)
- **Pre-flight port checking**: Validate ports before SSH connection
- **Conflict warnings**: Display user-friendly error dialogs
- **Selective rule application**: Skip conflicting port forwarding rules

### Dependencies

- **psutil >= 5.9.0**: Primary method for cross-platform port information
- **Fallback to netstat**: When psutil is unavailable
- **Socket-based checking**: For basic port availability testing

## Usage Examples

### 1. Basic Port Checking
```python
from sshpilot.port_utils import is_port_available, find_available_port

# Check if port is available
if is_port_available(8080):
    print("Port 8080 is available")
else:
    alt_port = find_available_port(8080)
    print(f"Port 8080 is busy, try {alt_port}")
```

### 2. Port Information Display
```python
from sshpilot.port_utils import get_listening_ports

ports = get_listening_ports()
for port_info in ports:
    print(f"Port {port_info.port}: {port_info.process_name} (PID: {port_info.pid})")
```

### 3. Conflict Detection
```python
from sshpilot.port_utils import check_port_conflicts

conflicts = check_port_conflicts([8080, 3000, 5432])
for port, port_info in conflicts:
    print(f"Port {port} is in use by {port_info.process_name}")
```

## User Interface Changes

### Port Forwarding Rule Dialog
- ✅ **Real-time validation**: Immediate feedback on port conflicts
- ✅ **Conflict warnings**: Shows which process is using the port
- ✅ **Alternative suggestions**: Recommends available ports nearby
- ✅ **Privilege warnings**: Alerts for ports requiring root access

### Port Forwarding Rules List
- ✅ **View Port Information button**: Opens system port information dialog
- ✅ **Enhanced rule display**: Better visualization of forwarding rules

### Port Information Dialog
- ✅ **System port overview**: Lists all listening ports
- ✅ **Process identification**: Shows which application owns each port
- ✅ **Refresh capability**: Real-time port information updates
- ✅ **Security indicators**: Highlights system ports requiring privileges

## Error Handling Improvements

### Before Integration
```
SSH Warning: remote port forwarding failed for listen port 8080
bind: Address already in use
```

### After Integration
```
❌ Port 8080 is already in use by nginx (PID: 1234)
⚠️ Suggested alternative: port 8081
```

### Pre-flight Checks
- **Conflict detection**: Warns users before SSH connection attempts
- **Rule filtering**: Automatically skips conflicting port forwarding rules
- **User notifications**: Shows detailed conflict information with resolution options

## Installation and Dependencies

### System Requirements
```bash
# Debian/Ubuntu
sudo apt install python3-psutil

# Fedora/RHEL
sudo dnf install python3-psutil

# Or via pip (if not system-managed)
pip install psutil>=5.9.0
```

### Graceful Degradation
The system works without psutil by falling back to:
1. **netstat command**: For port information gathering
2. **Socket testing**: For basic availability checking
3. **Limited functionality**: Basic conflict detection only

## Benefits

### For Users
- **Proactive conflict resolution**: Issues detected before connection attempts
- **Clear error messages**: No more cryptic SSH warnings
- **Alternative suggestions**: Automatic recommendations for available ports
- **System visibility**: Easy access to port usage information

### For Developers
- **Modular design**: Port utilities can be extended for other features
- **Cross-platform support**: Works on Linux, macOS, and Windows
- **Robust error handling**: Graceful degradation when dependencies unavailable
- **Comprehensive testing**: Includes test utilities for validation

## Future Enhancements

### Potential Improvements
1. **Automatic port selection**: Option to automatically choose available ports
2. **Port reservation**: Temporary port reservation during connection setup
3. **History tracking**: Remember previously used ports and conflicts
4. **Integration with system firewall**: Check firewall rules affecting ports
5. **Network interface awareness**: Port checking per network interface

### Integration Opportunities
1. **SSH config integration**: Validate ports in SSH config files
2. **Connection templates**: Pre-validate port forwarding in saved connections
3. **Batch operations**: Validate multiple connections simultaneously
4. **Monitoring integration**: Real-time port usage monitoring

## Testing

Run the integration test:
```bash
python3 test_port_integration.py
```

Expected output:
```
Testing sshPilot Port Utilities Integration
==================================================
✓ Port checker initialized
✓ Port 22 available: False
✓ Alternative to port 8080: 8081
✓ Found X listening ports
✓ Port conflicts detected and handled
```

## Conclusion

This integration significantly improves sshPilot's port forwarding capabilities by providing:
- **Proactive conflict detection**
- **User-friendly error handling**
- **System port visibility**
- **Intelligent alternative suggestions**

The modular design ensures the functionality can be extended and maintained independently while providing immediate value to users dealing with port forwarding conflicts.