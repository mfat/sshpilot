"""
Bulk Operations Manager for sshPilot
Handles connect all/disconnect all operations for groups with async processing and progress tracking
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum

from gi.repository import GObject, GLib
from .connection_manager import Connection

logger = logging.getLogger(__name__)


class OperationType(Enum):
    CONNECT = "connect"
    DISCONNECT = "disconnect"


@dataclass
class OperationResult:
    """Result of a single operation"""
    connection: Connection
    success: bool
    error: Optional[str] = None
    duration: float = 0.0


@dataclass
class BulkOperationStatus:
    """Status of a bulk operation"""
    operation_type: OperationType
    total_count: int
    completed_count: int = 0
    successful_count: int = 0
    failed_count: int = 0
    is_running: bool = False
    is_cancelled: bool = False
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    results: List[OperationResult] = None
    
    def __post_init__(self):
        if self.results is None:
            self.results = []
    
    @property
    def progress_percentage(self) -> float:
        if self.total_count == 0:
            return 100.0
        return (self.completed_count / self.total_count) * 100.0
    
    @property
    def elapsed_time(self) -> float:
        if self.start_time is None:
            return 0.0
        end = self.end_time or time.time()
        return end - self.start_time


class BulkOperationsManager(GObject.Object):
    """Manages bulk operations on groups of connections with async processing"""
    
    __gsignals__ = {
        'operation-started': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'operation-progress': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'operation-completed': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        'connection-result': (GObject.SignalFlags.RUN_FIRST, None, (object, object)),  # status, result
    }
    
    def __init__(self, connection_manager, terminal_manager):
        super().__init__()
        self.connection_manager = connection_manager
        self.terminal_manager = terminal_manager
        self.current_operation: Optional[BulkOperationStatus] = None
        self.cancel_event = None
        
        # Configuration
        self.max_concurrent = 10  # Maximum concurrent connections (increased for faster processing)
        self.connection_timeout = 15.0  # Timeout per connection in seconds (reduced for faster failure detection)
        self.retry_attempts = 0  # No retries for faster processing
        self.retry_delay = 1.0  # Delay between retries in seconds
    
    def set_concurrency_limit(self, limit: int):
        """Set the maximum number of concurrent operations"""
        self.max_concurrent = max(1, min(limit, 20))  # Clamp between 1-20
        logger.debug(f"Bulk operations concurrency limit set to {self.max_concurrent}")
    
    def set_connection_timeout(self, timeout: float):
        """Set the timeout for individual connection operations"""
        self.connection_timeout = max(5.0, timeout)
        logger.debug(f"Bulk operations connection timeout set to {self.connection_timeout}s")
    
    async def connect_all(self, connections: List[Connection], 
                         progress_callback: Optional[Callable] = None) -> BulkOperationStatus:
        """Connect to all connections in the list"""
        return await self._execute_bulk_operation(
            OperationType.CONNECT, 
            connections, 
            self._connect_single,
            progress_callback
        )
    
    async def disconnect_all(self, connections: List[Connection],
                           progress_callback: Optional[Callable] = None) -> BulkOperationStatus:
        """Disconnect from all connections in the list"""
        return await self._execute_bulk_operation(
            OperationType.DISCONNECT,
            connections,
            self._disconnect_single,
            progress_callback
        )
    

    
    def cancel_current_operation(self):
        """Cancel the current bulk operation"""
        if self.current_operation and self.current_operation.is_running:
            self.current_operation.is_cancelled = True
            if self.cancel_event:
                self.cancel_event.set()
            logger.info("Bulk operation cancellation requested")
    
    async def _execute_bulk_operation(self, operation_type: OperationType,
                                    connections: List[Connection],
                                    operation_func: Callable,
                                    progress_callback: Optional[Callable] = None) -> BulkOperationStatus:
        """Execute a bulk operation with proper async handling and progress tracking"""
        
        if self.current_operation and self.current_operation.is_running:
            raise RuntimeError("Another bulk operation is already running")
        
        # Initialize operation status
        status = BulkOperationStatus(
            operation_type=operation_type,
            total_count=len(connections),
            start_time=time.time(),
            is_running=True
        )
        self.current_operation = status
        self.cancel_event = asyncio.Event()
        
        logger.info(f"Starting bulk {operation_type.value} operation for {len(connections)} connections")
        
        # Emit operation started signal
        GLib.idle_add(self.emit, 'operation-started', status)
        
        try:
            # Create semaphore to limit concurrent operations
            semaphore = asyncio.Semaphore(self.max_concurrent)
            
            # Create tasks for all connections
            tasks = []
            for connection in connections:
                task = self._execute_single_operation(
                    semaphore, operation_func, connection, status, progress_callback
                )
                tasks.append(task)
            
            # Wait for all tasks to complete
            await asyncio.gather(*tasks, return_exceptions=True)
            
        except Exception as e:
            logger.error(f"Bulk operation failed: {e}", exc_info=True)
        finally:
            # Finalize operation
            status.is_running = False
            status.end_time = time.time()
            self.current_operation = None
            self.cancel_event = None
            
            logger.info(f"Bulk {operation_type.value} completed: {status.successful_count}/{status.total_count} successful, "
                       f"{status.failed_count} failed, {status.elapsed_time:.1f}s elapsed")
            
            # Emit operation completed signal
            GLib.idle_add(self.emit, 'operation-completed', status)
        
        return status
    
    async def _execute_single_operation(self, semaphore: asyncio.Semaphore,
                                      operation_func: Callable,
                                      connection: Connection,
                                      status: BulkOperationStatus,
                                      progress_callback: Optional[Callable] = None):
        """Execute a single operation with semaphore control and error handling"""
        
        async with semaphore:
            if status.is_cancelled:
                return
            
            start_time = time.time()
            result = OperationResult(connection=connection, success=False)
            
            try:
                # Execute the operation with timeout and retries
                for attempt in range(self.retry_attempts + 1):
                    if status.is_cancelled:
                        result.error = "Operation cancelled"
                        break
                    
                    try:
                        await asyncio.wait_for(
                            operation_func(connection),
                            timeout=self.connection_timeout
                        )
                        result.success = True
                        break
                    except asyncio.TimeoutError:
                        result.error = f"Timeout after {self.connection_timeout}s"
                        if attempt < self.retry_attempts:
                            logger.debug(f"Retrying {connection.nickname} (attempt {attempt + 2})")
                            await asyncio.sleep(self.retry_delay)
                    except Exception as e:
                        result.error = str(e)
                        if attempt < self.retry_attempts:
                            logger.debug(f"Retrying {connection.nickname} due to error: {e}")
                            await asyncio.sleep(self.retry_delay)
                        else:
                            logger.error(f"Operation failed for {connection.nickname}: {e}")
                
            except Exception as e:
                result.error = f"Unexpected error: {e}"
                logger.error(f"Unexpected error in bulk operation for {connection.nickname}: {e}")
            
            finally:
                result.duration = time.time() - start_time
                
                # Update status
                status.completed_count += 1
                if result.success:
                    status.successful_count += 1
                else:
                    status.failed_count += 1
                status.results.append(result)
                
                # Emit progress signals
                GLib.idle_add(self.emit, 'connection-result', status, result)
                GLib.idle_add(self.emit, 'operation-progress', status)
                
                if progress_callback:
                    GLib.idle_add(progress_callback, status, result)
    
    async def _connect_single(self, connection: Connection):
        """Connect a single connection"""
        if connection.is_connected:
            logger.debug(f"Connection {connection.nickname} already connected, skipping")
            return
        
        # Use terminal manager for consistency with existing connection flow
        def connect_on_main_thread():
            try:
                self.terminal_manager.connect_to_host(connection, force_new=False)
                return True
            except Exception as e:
                logger.error(f"Failed to connect {connection.nickname}: {e}")
                raise
        
        # Execute on main thread since GTK operations are required
        future = asyncio.Future()
        
        def _execute():
            try:
                result = connect_on_main_thread()
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            return False  # Don't repeat
        
        GLib.idle_add(_execute)
        await future
        
        # Wait a bit for connection to establish
        await asyncio.sleep(0.5)
    
    async def _disconnect_single(self, connection: Connection):
        """Disconnect a single connection"""
        if not connection.is_connected:
            logger.debug(f"Connection {connection.nickname} not connected, skipping")
            return
        
        def disconnect_on_main_thread():
            try:
                # Find and disconnect terminal
                window = self.terminal_manager.window
                if connection in window.active_terminals:
                    terminal = window.active_terminals[connection]
                    terminal.disconnect()
                return True
            except Exception as e:
                logger.error(f"Failed to disconnect {connection.nickname}: {e}")
                raise
        
        future = asyncio.Future()
        
        def _execute():
            try:
                result = disconnect_on_main_thread()
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            return False
        
        GLib.idle_add(_execute)
        await future
        
        # Wait a bit for disconnection to complete
        await asyncio.sleep(0.2)
    
