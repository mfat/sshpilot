use std::collections::HashMap;
use std::net::{TcpListener, TcpStream, SocketAddr};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};
use anyhow::{Result, anyhow};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortInfo {
    pub port: u16,
    pub protocol: String,
    pub pid: Option<u32>,
    pub process_name: Option<String>,
    pub address: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortForwardingRule {
    pub id: String,
    pub name: String,
    pub local_port: u16,
    pub remote_host: String,
    pub remote_port: u16,
    pub direction: ForwardingDirection,
    pub enabled: bool,
    pub connection_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ForwardingDirection {
    LocalToRemote,
    RemoteToLocal,
    Dynamic,
}

impl ForwardingDirection {
    pub fn as_str(&self) -> &'static str {
        match self {
            ForwardingDirection::LocalToRemote => "local-to-remote",
            ForwardingDirection::RemoteToLocal => "remote-to-local",
            ForwardingDirection::Dynamic => "dynamic",
        }
    }
}

pub struct PortManager {
    forwarding_rules: HashMap<String, PortForwardingRule>,
    active_listeners: HashMap<String, TcpListener>,
}

impl PortManager {
    pub fn new() -> Self {
        Self {
            forwarding_rules: HashMap::new(),
            active_listeners: HashMap::new(),
        }
    }

    pub async fn check_port_availability(&self, port: u16, address: &str) -> bool {
        match TcpListener::bind(format!("{}:{}", address, port)) {
            Ok(_) => true,
            Err(_) => false,
        }
    }

    pub async fn find_available_port(&self, start_port: u16, end_port: u16, address: &str) -> Option<u16> {
        for port in start_port..=end_port {
            if self.check_port_availability(port, address).await {
                return Some(port);
            }
        }
        None
    }

    pub async fn get_listening_ports(&self) -> Vec<PortInfo> {
        let mut ports = Vec::new();
        
        // This is a simplified implementation
        // In a real application, you'd use system-specific APIs to get detailed port information
        
        // Check common ports
        let common_ports = vec![22, 80, 443, 8080, 3000, 5000, 5432, 6379, 27017];
        
        for port in common_ports {
            if !self.check_port_availability(port, "127.0.0.1").await {
                ports.push(PortInfo {
                    port,
                    protocol: "tcp".to_string(),
                    pid: None,
                    process_name: None,
                    address: "127.0.0.1".to_string(),
                });
            }
        }
        
        ports
    }

    pub async fn add_forwarding_rule(
        &mut self,
        name: String,
        local_port: u16,
        remote_host: String,
        remote_port: u16,
        direction: ForwardingDirection,
        connection_id: String,
    ) -> Result<String> {
        // Check if local port is available
        if !self.check_port_availability(local_port, "127.0.0.1").await {
            return Err(anyhow!("Local port {} is not available", local_port));
        }
        
        let rule = PortForwardingRule {
            id: Uuid::new_v4().to_string(),
            name,
            local_port,
            remote_host,
            remote_port,
            direction,
            enabled: false,
            connection_id,
        };
        
        let rule_id = rule.id.clone();
        self.forwarding_rules.insert(rule_id.clone(), rule);
        
        Ok(rule_id)
    }

    pub async fn remove_forwarding_rule(&mut self, rule_id: &str) -> Result<()> {
        // Stop the forwarding if it's active
        if let Some(rule) = self.forwarding_rules.get(rule_id) {
            if rule.enabled {
                self.stop_port_forwarding(rule_id).await?;
            }
        }
        
        self.forwarding_rules.remove(rule_id);
        Ok(())
    }

    pub async fn start_port_forwarding(&mut self, rule_id: &str) -> Result<()> {
        let rule = self.forwarding_rules.get_mut(rule_id)
            .ok_or_else(|| anyhow!("Forwarding rule not found"))?;
        
        if rule.enabled {
            return Ok(());
        }
        
        match rule.direction {
            ForwardingDirection::LocalToRemote => {
                self.start_local_to_remote_forwarding(rule).await?;
            }
            ForwardingDirection::RemoteToLocal => {
                self.start_remote_to_local_forwarding(rule).await?;
            }
            ForwardingDirection::Dynamic => {
                self.start_dynamic_forwarding(rule).await?;
            }
        }
        
        rule.enabled = true;
        Ok(())
    }

    pub async fn stop_port_forwarding(&mut self, rule_id: &str) -> Result<()> {
        let rule = self.forwarding_rules.get_mut(rule_id)
            .ok_or_else(|| anyhow!("Forwarding rule not found"))?;
        
        if !rule.enabled {
            return Ok(());
        }
        
        // Remove the listener
        if let Some(listener) = self.active_listeners.remove(rule_id) {
            drop(listener); // This will close the listener
        }
        
        rule.enabled = false;
        Ok(())
    }

    async fn start_local_to_remote_forwarding(&mut self, rule: &PortForwardingRule) -> Result<()> {
        let listener = TcpListener::bind(format!("127.0.0.1:{}", rule.local_port))?;
        self.active_listeners.insert(rule.id.clone(), listener);
        
        // In a real implementation, you'd spawn a task to handle the forwarding
        // This is a simplified version
        
        Ok(())
    }

    async fn start_remote_to_local_forwarding(&mut self, rule: &PortForwardingRule) -> Result<()> {
        // This would require SSH port forwarding support
        // For now, we'll just mark it as started
        Ok(())
    }

    async fn start_dynamic_forwarding(&mut self, rule: &PortForwardingRule) -> Result<()> {
        // This would require SSH dynamic port forwarding support
        // For now, we'll just mark it as started
        Ok(())
    }

    pub async fn list_forwarding_rules(&self) -> Vec<PortForwardingRule> {
        self.forwarding_rules.values().cloned().collect()
    }

    pub async fn get_forwarding_rule(&self, rule_id: &str) -> Option<PortForwardingRule> {
        self.forwarding_rules.get(rule_id).cloned()
    }
}

#[tauri::command]
pub async fn check_port_availability(
    app_handle: AppHandle,
    port: u16,
    address: String,
) -> Result<bool, String> {
    let port_manager: State<PortManager> = app_handle.state();
    Ok(port_manager.check_port_availability(port, &address).await)
}

#[tauri::command]
pub async fn get_listening_ports(app_handle: AppHandle) -> Result<Vec<PortInfo>, String> {
    let port_manager: State<PortManager> = app_handle.state();
    Ok(port_manager.get_listening_ports().await)
}

#[tauri::command]
pub async fn add_forwarding_rule(
    app_handle: AppHandle,
    name: String,
    local_port: u16,
    remote_host: String,
    remote_port: u16,
    direction: String,
    connection_id: String,
) -> Result<String, String> {
    let mut port_manager: State<PortManager> = app_handle.state();
    
    let direction = match direction.as_str() {
        "local-to-remote" => ForwardingDirection::LocalToRemote,
        "remote-to-local" => ForwardingDirection::RemoteToLocal,
        "dynamic" => ForwardingDirection::Dynamic,
        _ => return Err("Invalid forwarding direction".to_string()),
    };
    
    port_manager.add_forwarding_rule(name, local_port, remote_host, remote_port, direction, connection_id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn start_port_forwarding(
    app_handle: AppHandle,
    rule_id: String,
) -> Result<(), String> {
    let mut port_manager: State<PortManager> = app_handle.state();
    port_manager.start_port_forwarding(&rule_id).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn stop_port_forwarding(
    app_handle: AppHandle,
    rule_id: String,
) -> Result<(), String> {
    let mut port_manager: State<PortManager> = app_handle.state();
    port_manager.stop_port_forwarding(&rule_id).await.map_err(|e| e.to_string())
}

