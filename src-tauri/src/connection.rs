use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};
use anyhow::{Result, anyhow};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Connection {
    pub id: String,
    pub nickname: String,
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: Option<String>,
    pub key_path: Option<String>,
    pub key_passphrase: Option<String>,
    pub auth_method: AuthMethod,
    pub local_command: Option<String>,
    pub remote_command: Option<String>,
    pub extra_ssh_config: Option<String>,
    pub x11_forwarding: bool,
    pub forwarding_rules: Vec<PortForwardingRule>,
    pub group: Option<String>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub last_used: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum AuthMethod {
    Password,
    Key,
    KeyWithPassphrase,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortForwardingRule {
    pub id: String,
    pub local_port: u16,
    pub remote_host: String,
    pub remote_port: u16,
    pub direction: ForwardingDirection,
    pub enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum ForwardingDirection {
    LocalToRemote,
    RemoteToLocal,
    Dynamic,
}

pub struct ConnectionManager {
    connections: Arc<Mutex<HashMap<String, Connection>>>,
    config_path: String,
}

impl ConnectionManager {
    pub fn new() -> Self {
        let config_path = Self::get_config_path();
        Self {
            connections: Arc::new(Mutex::new(HashMap::new())),
            config_path,
        }
    }

    fn get_config_path() -> String {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        format!("{}/.config/sshpilot/connections.json", home)
    }

    pub async fn load_connections(&self) -> Result<()> {
        let config_dir = std::path::Path::new(&self.config_path).parent().unwrap();
        std::fs::create_dir_all(config_dir)?;
        
        if !std::path::Path::new(&self.config_path).exists() {
            return Ok(());
        }
        
        let content = std::fs::read_to_string(&self.config_path)?;
        let connections: Vec<Connection> = serde_json::from_str(&content)?;
        
        let mut conn_map = self.connections.lock().unwrap();
        for conn in connections {
            conn_map.insert(conn.id.clone(), conn);
        }
        
        Ok(())
    }

    pub async fn save_connections(&self) -> Result<()> {
        let config_dir = std::path::Path::new(&self.config_path).parent().unwrap();
        std::fs::create_dir_all(config_dir)?;
        
        let connections = self.connections.lock().unwrap();
        let conn_vec: Vec<&Connection> = connections.values().collect();
        let content = serde_json::to_string_pretty(&conn_vec)?;
        
        std::fs::write(&self.config_path, content)?;
        Ok(())
    }

    pub async fn add_connection(&self, mut connection: Connection) -> Result<String> {
        if connection.id.is_empty() {
            connection.id = Uuid::new_v4().to_string();
        }
        
        connection.created_at = chrono::Utc::now();
        
        let id = connection.id.clone();
        self.connections.lock().unwrap().insert(id.clone(), connection);
        
        self.save_connections().await?;
        Ok(id)
    }

    pub async fn update_connection(&self, connection: Connection) -> Result<()> {
        let mut connections = self.connections.lock().unwrap();
        if let Some(existing) = connections.get_mut(&connection.id) {
            *existing = connection;
            drop(connections);
            self.save_connections().await?;
            Ok(())
        } else {
            Err(anyhow!("Connection not found"))
        }
    }

    pub async fn delete_connection(&self, connection_id: &str) -> Result<()> {
        self.connections.lock().unwrap().remove(connection_id);
        self.save_connections().await?;
        Ok(())
    }

    pub async fn get_connection(&self, connection_id: &str) -> Option<Connection> {
        self.connections.lock().unwrap().get(connection_id).cloned()
    }

    pub async fn list_connections(&self) -> Vec<Connection> {
        let connections = self.connections.lock().unwrap();
        connections.values().cloned().collect()
    }

    pub async fn get_connections_by_group(&self, group: &str) -> Vec<Connection> {
        let connections = self.connections.lock().unwrap();
        connections.values()
            .filter(|conn| conn.group.as_deref() == Some(group))
            .cloned()
            .collect()
    }

    pub async fn search_connections(&self, query: &str) -> Vec<Connection> {
        let query = query.to_lowercase();
        let connections = self.connections.lock().unwrap();
        
        connections.values()
            .filter(|conn| {
                conn.nickname.to_lowercase().contains(&query) ||
                conn.host.to_lowercase().contains(&query) ||
                conn.username.to_lowercase().contains(&query)
            })
            .cloned()
            .collect()
    }
}

#[tauri::command]
pub async fn get_connections(app_handle: AppHandle) -> Result<Vec<Connection>, String> {
    let conn_manager: State<ConnectionManager> = app_handle.state();
    conn_manager.load_connections().await.map_err(|e| e.to_string())?;
    conn_manager.list_connections().await.into_iter().map(|conn| {
        Ok(conn)
    }).collect()
}

#[tauri::command]
pub async fn save_connection(
    app_handle: AppHandle,
    connection: Connection,
) -> Result<String, String> {
    let conn_manager: State<ConnectionManager> = app_handle.state();
    conn_manager.add_connection(connection).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn delete_connection(
    app_handle: AppHandle,
    connection_id: String,
) -> Result<(), String> {
    let conn_manager: State<ConnectionManager> = app_handle.state();
    conn_manager.delete_connection(&connection_id).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn test_connection(
    app_handle: AppHandle,
    host: String,
    port: u16,
    username: String,
    password: Option<String>,
    key_path: Option<String>,
) -> Result<bool, String> {
    // This would typically use the SSH manager to test the connection
    // For now, we'll just validate the input parameters
    if host.is_empty() || username.is_empty() {
        return Err("Host and username are required".to_string());
    }
    
    if port == 0 || port > 65535 {
        return Err("Invalid port number".to_string());
    }
    
    Ok(true)
}

