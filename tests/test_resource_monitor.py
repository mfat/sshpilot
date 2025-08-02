"""
Tests for resource monitoring functionality
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import time
from collections import deque

from io.github.mfat.sshpilot.resource_monitor import ResourceMonitor, ResourceData
from io.github.mfat.sshpilot.connection_manager import Connection


class TestResourceData:
    """Test ResourceData class"""
    
    def test_resource_data_initialization(self):
        """Test ResourceData initialization"""
        data = ResourceData(max_points=100)
        
        assert data.max_points == 100
        assert len(data.timestamps) == 0
        assert len(data.cpu_usage) == 0
        assert len(data.memory_usage) == 0
        assert isinstance(data.timestamps, deque)
    
    def test_add_data_point(self):
        """Test adding data point"""
        data = ResourceData(max_points=10)
        
        test_data = {
            'cpu_usage': 25.5,
            'memory_usage': 1024*1024*512,  # 512MB
            'memory_total': 1024*1024*1024*2,  # 2GB
            'disk_usage': 1024*1024*1024*10,  # 10GB
            'disk_total': 1024*1024*1024*100,  # 100GB
            'network_rx': 1024*100,  # 100KB/s
            'network_tx': 1024*50,   # 50KB/s
            'load_avg': 1.2
        }
        
        data.add_data_point(test_data)
        
        assert len(data.timestamps) == 1
        assert data.cpu_usage[-1] == 25.5
        assert data.memory_usage[-1] == 1024*1024*512
        assert data.load_avg[-1] == 1.2
    
    def test_max_points_limit(self):
        """Test that data respects max_points limit"""
        data = ResourceData(max_points=3)
        
        # Add more points than limit
        for i in range(5):
            test_data = {'cpu_usage': i * 10}
            data.add_data_point(test_data)
        
        # Should only keep last 3 points
        assert len(data.cpu_usage) == 3
        assert data.cpu_usage[0] == 20  # Point from i=2
        assert data.cpu_usage[-1] == 40  # Point from i=4
    
    def test_get_latest(self):
        """Test getting latest data point"""
        data = ResourceData()
        
        # No data initially
        latest = data.get_latest()
        assert latest == {}
        
        # Add data point
        test_data = {
            'cpu_usage': 30.0,
            'memory_usage': 1024*1024*256,
            'load_avg': 0.8
        }
        data.add_data_point(test_data)
        
        latest = data.get_latest()
        assert latest['cpu_usage'] == 30.0
        assert latest['memory_usage'] == 1024*1024*256
        assert latest['load_avg'] == 0.8
        assert 'timestamp' in latest


class TestResourceMonitor:
    """Test ResourceMonitor class"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.monitor = ResourceMonitor()
        self.connection = Mock()
        self.connection.nickname = 'test-server'
        self.connection.is_connected = True
        self.connection.client = Mock()
    
    def test_monitor_initialization(self):
        """Test ResourceMonitor initialization"""
        assert self.monitor is not None
        assert hasattr(self.monitor, 'monitoring_sessions')
        assert isinstance(self.monitor.monitoring_sessions, dict)
        assert self.monitor.update_interval == 5
    
    def test_monitor_with_config(self):
        """Test ResourceMonitor with config"""
        config = Mock()
        config.get_monitoring_config.return_value = {
            'update_interval': 10,
            'history_length': 500
        }
        
        monitor = ResourceMonitor(config)
        
        assert monitor.update_interval == 10
    
    @patch('threading.Thread')
    def test_start_monitoring(self, mock_thread):
        """Test starting monitoring session"""
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance
        
        result = self.monitor.start_monitoring(self.connection)
        
        assert result is True
        assert self.connection in self.monitor.monitoring_sessions
        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()
    
    def test_start_monitoring_disconnected(self):
        """Test starting monitoring on disconnected connection"""
        self.connection.is_connected = False
        
        result = self.monitor.start_monitoring(self.connection)
        
        assert result is False
        assert self.connection not in self.monitor.monitoring_sessions
    
    def test_start_monitoring_already_monitoring(self):
        """Test starting monitoring when already monitoring"""
        # Set up existing session
        self.monitor.monitoring_sessions[self.connection] = {
            'thread': Mock(),
            'resource_data': Mock(),
            'stop_event': Mock()
        }
        
        result = self.monitor.start_monitoring(self.connection)
        
        assert result is True
    
    def test_stop_monitoring(self):
        """Test stopping monitoring session"""
        # Set up monitoring session
        mock_thread = Mock()
        mock_stop_event = Mock()
        self.monitor.monitoring_sessions[self.connection] = {
            'thread': mock_thread,
            'resource_data': Mock(),
            'stop_event': mock_stop_event
        }
        
        self.monitor.stop_monitoring(self.connection)
        
        mock_stop_event.set.assert_called_once()
        mock_thread.join.assert_called_once_with(timeout=2.0)
        assert self.connection not in self.monitor.monitoring_sessions
    
    def test_get_resource_data(self):
        """Test getting resource data for connection"""
        mock_data = Mock()
        self.monitor.monitoring_sessions[self.connection] = {
            'resource_data': mock_data
        }
        
        result = self.monitor.get_resource_data(self.connection)
        
        assert result == mock_data
    
    def test_get_resource_data_not_monitoring(self):
        """Test getting resource data when not monitoring"""
        result = self.monitor.get_resource_data(self.connection)
        
        assert result is None
    
    def test_is_monitoring(self):
        """Test checking if connection is being monitored"""
        assert not self.monitor.is_monitoring(self.connection)
        
        # Add monitoring session
        self.monitor.monitoring_sessions[self.connection] = {}
        
        assert self.monitor.is_monitoring(self.connection)
    
    def test_fetch_system_info(self):
        """Test fetching system information via SSH"""
        # Mock SSH client responses
        mock_stdout = Mock()
        mock_stdout.read.return_value = Mock()
        mock_stdout.read.return_value.decode.return_value = "cpu line"
        
        mock_stderr = Mock()
        mock_stderr.read.return_value = Mock()
        mock_stderr.read.return_value.decode.return_value = ""
        
        self.connection.client.exec_command.return_value = (Mock(), mock_stdout, mock_stderr)
        
        with patch.object(self.monitor, '_parse_system_info') as mock_parse:
            mock_parse.return_value = {'cpu_usage': 25.0}
            
            result = self.monitor._fetch_system_info(self.connection)
            
            assert result == {'cpu_usage': 25.0}
            # Should call exec_command for each metric
            assert self.connection.client.exec_command.call_count >= 4
    
    def test_parse_system_info_cpu(self):
        """Test parsing CPU information"""
        results = {
            'cpu': 'cpu  123456 0 234567 890123 0 0 0 0 0 0',
            'memory': '',
            'disk': '',
            'network': '',
            'loadavg': '',
            'uptime': ''
        }
        
        parsed = self.monitor._parse_system_info(results)
        
        assert 'cpu_usage' in parsed
        assert isinstance(parsed['cpu_usage'], (int, float))
        assert 0 <= parsed['cpu_usage'] <= 100
    
    def test_parse_system_info_memory(self):
        """Test parsing memory information"""
        results = {
            'cpu': '',
            'memory': 'MemTotal:        8000000 kB\nMemAvailable:    4000000 kB\n',
            'disk': '',
            'network': '',
            'loadavg': '',
            'uptime': ''
        }
        
        parsed = self.monitor._parse_system_info(results)
        
        assert 'memory_total' in parsed
        assert 'memory_usage' in parsed
        assert parsed['memory_total'] == 8000000 * 1024  # Converted to bytes
        assert parsed['memory_usage'] == 4000000 * 1024  # Available -> used
    
    def test_parse_system_info_disk(self):
        """Test parsing disk information"""
        results = {
            'cpu': '',
            'memory': '',
            'disk': '/dev/sda1       100G   50G   45G  53% /',
            'network': '',
            'loadavg': '',
            'uptime': ''
        }
        
        with patch.object(self.monitor, '_parse_size_string') as mock_parse_size:
            mock_parse_size.side_effect = [100*1024**3, 50*1024**3]  # 100GB, 50GB
            
            parsed = self.monitor._parse_system_info(results)
            
            assert 'disk_total' in parsed
            assert 'disk_usage' in parsed
    
    def test_parse_system_info_network(self):
        """Test parsing network information"""
        results = {
            'cpu': '',
            'memory': '',
            'disk': '',
            'network': 'Inter-|   Receive                                                |  Transmit\n'
                      ' face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n'
                      '    lo:   1000     100    0    0    0     0          0         0     1000     100    0    0    0     0       0          0\n'
                      '  eth0: 500000    5000    0    0    0     0          0         0   250000    2500    0    0    0     0       0          0\n',
            'loadavg': '',
            'uptime': ''
        }
        
        parsed = self.monitor._parse_system_info(results)
        
        assert 'network_rx_bytes' in parsed
        assert 'network_tx_bytes' in parsed
        # Should exclude loopback interface
        assert parsed['network_rx_bytes'] == 500000
        assert parsed['network_tx_bytes'] == 250000
    
    def test_parse_system_info_loadavg(self):
        """Test parsing load average"""
        results = {
            'cpu': '',
            'memory': '',
            'disk': '',
            'network': '',
            'loadavg': '1.23 2.34 3.45 2/123 12345',
            'uptime': ''
        }
        
        parsed = self.monitor._parse_system_info(results)
        
        assert 'load_avg' in parsed
        assert parsed['load_avg'] == 1.23
    
    def test_parse_size_string(self):
        """Test parsing size strings"""
        assert self.monitor._parse_size_string('1024') == 1024
        assert self.monitor._parse_size_string('1K') == 1024
        assert self.monitor._parse_size_string('1M') == 1024**2
        assert self.monitor._parse_size_string('1G') == 1024**3
        assert self.monitor._parse_size_string('1T') == 1024**4
        assert self.monitor._parse_size_string('1.5G') == int(1.5 * 1024**3)
    
    def test_parse_size_string_invalid(self):
        """Test parsing invalid size strings"""
        assert self.monitor._parse_size_string('invalid') == 0
        assert self.monitor._parse_size_string('') == 0


class TestResourceMonitorIntegration:
    """Integration tests for ResourceMonitor"""
    
    def setup_method(self):
        """Set up test fixtures"""
        self.monitor = ResourceMonitor()
        self.connection = Mock()
        self.connection.nickname = 'test-server'
        self.connection.is_connected = True
        self.connection.client = Mock()
    
    @patch('time.sleep')  # Speed up test
    def test_monitoring_thread_lifecycle(self, mock_sleep):
        """Test complete monitoring thread lifecycle"""
        # Mock SSH command responses
        def mock_exec_command(cmd):
            stdout = Mock()
            stderr = Mock()
            stderr.read.return_value.decode.return_value = ""
            
            if 'stat' in cmd:
                stdout.read.return_value.decode.return_value = "cpu  100 0 200 700 0 0 0 0 0 0"
            elif 'meminfo' in cmd:
                stdout.read.return_value.decode.return_value = "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB"
            elif 'df' in cmd:
                stdout.read.return_value.decode.return_value = "/dev/sda1 100G 50G 45G 53% /"
            elif 'net/dev' in cmd:
                stdout.read.return_value.decode.return_value = "eth0: 1000 10 0 0 0 0 0 0 500 5 0 0 0 0 0 0"
            elif 'loadavg' in cmd:
                stdout.read.return_value.decode.return_value = "1.5 2.0 2.5 1/100 1234"
            else:
                stdout.read.return_value.decode.return_value = ""
            
            return Mock(), stdout, stderr
        
        self.connection.client.exec_command.side_effect = mock_exec_command
        
        # Start monitoring
        result = self.monitor.start_monitoring(self.connection)
        assert result is True
        
        # Wait a bit for thread to collect data
        time.sleep(0.1)
        
        # Check that data was collected
        resource_data = self.monitor.get_resource_data(self.connection)
        assert resource_data is not None
        
        # Stop monitoring
        self.monitor.stop_monitoring(self.connection)
        assert not self.monitor.is_monitoring(self.connection)
    
    def test_monitoring_connection_lost(self):
        """Test monitoring when connection is lost"""
        # Start monitoring
        self.monitor.start_monitoring(self.connection)
        
        # Simulate connection loss
        self.connection.is_connected = False
        self.connection.client = None
        
        # Monitoring should handle this gracefully
        # In real implementation, thread would exit
        assert self.monitor.is_monitoring(self.connection)
        
        # Clean up
        self.monitor.stop_monitoring(self.connection)