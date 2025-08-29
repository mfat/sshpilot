# Bulk Operations Implementation for sshPilot Groups

## Overview

This implementation provides reliable connect all/kill all functionality for groups while keeping the application responsive. The solution uses async processing, rate limiting, and progress tracking to handle large numbers of connections efficiently.

## Architecture

### Core Components

1. **BulkOperationsManager** (`bulk_operations.py`)
   - Manages async bulk operations with configurable concurrency
   - Supports connect all, disconnect all, and kill all operations
   - Includes retry logic and timeout handling
   - Emits progress signals for UI updates

2. **BulkProgressDialog** (`bulk_progress_dialog.py`)
   - Shows real-time progress with detailed status
   - Displays individual connection results
   - Provides cancel functionality
   - Responsive UI with smooth updates

3. **UI Integration** (modified `window.py`)
   - Context menu actions for groups
   - Helper methods for group connection collection
   - Action handlers for bulk operations

## Key Features

### Responsiveness
- **Async Processing**: All operations run asynchronously without blocking the UI
- **Rate Limiting**: Configurable concurrency limit (default: 5 simultaneous connections)
- **Progress Updates**: Real-time progress feedback via GLib.idle_add()
- **Cancellation**: Users can cancel operations at any time

### Reliability
- **Error Handling**: Individual connection failures don't stop the entire operation
- **Retry Logic**: Configurable retry attempts with delays
- **Timeouts**: Per-connection timeouts prevent hanging
- **State Tracking**: Proper connection state management

### User Experience
- **Progress Dialog**: Shows detailed progress with success/failure counts
- **Individual Results**: Lists each connection's result with error messages
- **Confirmation**: Kill all operations require user confirmation
- **Context Menus**: Easy access via right-click on group headers

## Usage

### Connect All
Right-click on a group header and select "Connect All" to connect to all connections in the group (including nested groups).

### Disconnect All
Right-click on a group header and select "Disconnect All" to gracefully disconnect from all connected connections in the group.

### Kill All
Right-click on a group header and select "Kill All Connections" to forcefully terminate all connections. This shows a confirmation dialog due to its destructive nature.

## Configuration

The bulk operations manager can be configured:

```python
# Set maximum concurrent connections (1-20)
bulk_operations_manager.set_concurrency_limit(10)

# Set timeout per connection (minimum 5 seconds)
bulk_operations_manager.set_connection_timeout(45.0)
```

## Technical Details

### Async Architecture
- Uses asyncio.Semaphore for concurrency control
- Operations are queued and processed with proper backpressure
- GTK operations are dispatched to main thread via GLib.idle_add()

### Error Resilience
- Individual connection failures are isolated
- Operations continue even if some connections fail
- Detailed error reporting for troubleshooting

### Memory Management
- Progress dialog auto-scrolls to show latest results
- Proper cleanup of async tasks and resources
- Weak references where appropriate

## Integration Points

### Connection Manager
- Uses existing Connection objects
- Integrates with current connection state tracking
- Respects authentication methods and SSH config

### Terminal Manager
- Leverages existing terminal creation logic
- Maintains consistency with manual connections
- Proper terminal lifecycle management

### Group Manager
- Recursively collects connections from nested groups
- Respects group hierarchy
- Maintains group state consistency

## Performance Characteristics

- **Small Groups (1-5 connections)**: Near-instant operation with minimal overhead
- **Medium Groups (6-20 connections)**: Smooth progress with default concurrency
- **Large Groups (20+ connections)**: Rate-limited processing maintains responsiveness
- **Memory Usage**: Minimal overhead, scales linearly with connection count
- **UI Responsiveness**: Maintained throughout operations via async architecture

## Error Scenarios Handled

1. **Network Timeouts**: Per-connection timeouts with retry logic
2. **Authentication Failures**: Proper error reporting and continuation
3. **SSH Errors**: Detailed error messages for troubleshooting
4. **Resource Exhaustion**: Rate limiting prevents system overload
5. **User Cancellation**: Clean cancellation with proper cleanup
6. **Connection State Issues**: Robust state checking and recovery

## Future Enhancements

Potential improvements that could be added:

1. **Batch Size Configuration**: Allow users to configure concurrent connection limits
2. **Operation Scheduling**: Queue operations for later execution
3. **Connection Filtering**: Connect only to specific connection types
4. **Custom Timeouts**: Per-connection timeout configuration
5. **Operation History**: Log of previous bulk operations
6. **Export Results**: Save operation results to file

This implementation provides a solid foundation for reliable bulk operations while maintaining the application's responsiveness and user experience.