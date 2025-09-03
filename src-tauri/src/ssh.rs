use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use serde::{Deserialize, Serialize};
use ssh2::Session;
use tauri::{AppHandle, State};
use anyhow::{Result, anyhow};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SSHConnection {
    pub id: String,
    pub host: String,
    pub port: u16,
    pub username: String,
    pub password: Option<String>,
    pub key_path: Option<String>,
    pub key_passphrase: Option<String>,
    pub nickname: String,
    pub is_connected: bool,
    pub session: Option<Arc<Mutex<Session>>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SSHCommand {
    pub command: String,
    pub working_directory: Option<String>,
    pub environment: Option<HashMap<String, String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SSHCommandResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

pub struct SSHManager {
    connections: Arc<Mutex<HashMap<String, SSHConnection>>>,
}

impl SSHManager {
    pub fn new() -> Self {
        Self {
            connections: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub async fn connect(
        &self,
        host: String,
        port: u16,
        username: String,
        password: Option<String>,
        key_path: Option<String>,
        key_passphrase: Option<String>,
        nickname: Option<String>,
    ) -> Result<String> {
        let nickname = nickname.unwrap_or_else(|| format!("{}@{}", username, host));
        
        // Create TCP connection
        let tcp = std::net::TcpStream::connect(format!("{}:{}", host, port))?;
        tcp.set_nodelay(true)?;
        
        // Create SSH session
        let mut sess = Session::new()?;
        sess.set_tcp_stream(tcp);
        sess.handshake()?;
        
        // Authenticate
        if let Some(key_path) = key_path {
            if let Some(passphrase) = key_passphrase {
                sess.userauth_pubkey_file(&username, None, &key_path, Some(&passphrase))?;
            } else {
                sess.userauth_pubkey_file(&username, None, &key_path, None)?;
            }
        } else if let Some(password) = password {
            sess.userauth_password(&username, &password)?;
        } else {
            return Err(anyhow!("No authentication method provided"));
        }
        
        // Verify authentication
        if !sess.authenticated() {
            return Err(anyhow!("SSH authentication failed"));
        }
        
        let connection = SSHConnection {
            id: uuid::Uuid::new_v4().to_string(),
            host,
            port,
            username,
            password,
            key_path,
            key_passphrase,
            nickname,
            is_connected: true,
            session: Some(Arc::new(Mutex::new(sess))),
        };
        
        let connection_id = connection.id.clone();
        self.connections.lock().unwrap().insert(connection_id.clone(), connection);
        
        Ok(connection_id)
    }

    pub async fn disconnect(&self, connection_id: &str) -> Result<()> {
        let mut connections = self.connections.lock().unwrap();
        if let Some(connection) = connections.get_mut(connection_id) {
            connection.is_connected = false;
            connection.session = None;
        }
        Ok(())
    }

    pub async fn execute_command(
        &self,
        connection_id: &str,
        command: &str,
    ) -> Result<SSHCommandResult> {
        let connections = self.connections.lock().unwrap();
        let connection = connections.get(connection_id)
            .ok_or_else(|| anyhow!("Connection not found"))?;
        
        let session = connection.session.as_ref()
            .ok_or_else(|| anyhow!("Connection not established"))?;
        
        let session = session.lock().unwrap();
        let mut channel = session.channel_session()?;
        
        channel.exec(command)?;
        
        let mut stdout = String::new();
        let mut stderr = String::new();
        
        channel.read_to_string(&mut stdout)?;
        channel.stderr().read_to_string(&mut stderr)?;
        
        channel.wait_close()?;
        let exit_code = channel.exit_status()?;
        
        Ok(SSHCommandResult {
            stdout,
            stderr,
            exit_code,
        })
    }

    pub async fn get_connection(&self, connection_id: &str) -> Option<SSHConnection> {
        let connections = self.connections.lock().unwrap();
        connections.get(connection_id).cloned()
    }

    pub async fn list_connections(&self) -> Vec<SSHConnection> {
        let connections = self.connections.lock().unwrap();
        connections.values().cloned().collect()
    }
}

#[tauri::command]
pub async fn connect_ssh(
    app_handle: AppHandle,
    host: String,
    port: u16,
    username: String,
    password: Option<String>,
    key_path: Option<String>,
    key_passphrase: Option<String>,
    nickname: Option<String>,
) -> Result<String, String> {
    let ssh_manager: State<SSHManager> = app_handle.state();
    ssh_manager.connect(host, port, username, password, key_path, key_passphrase, nickname)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn disconnect_ssh(
    app_handle: AppHandle,
    connection_id: String,
) -> Result<(), String> {
    let ssh_manager: State<SSHManager> = app_handle.state();
    ssh_manager.disconnect(&connection_id)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn execute_command(
    app_handle: AppHandle,
    connection_id: String,
    command: String,
) -> Result<SSHCommandResult, String> {
    let ssh_manager: State<SSHManager> = app_handle.state();
    ssh_manager.execute_command(&connection_id, &command)
        .await
        .map_err(|e| e.to_string())
}

