use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};
use anyhow::{Result, anyhow};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Terminal {
    pub id: String,
    pub connection_id: String,
    pub title: String,
    pub is_active: bool,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub last_activity: chrono::DateTime<chrono::Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalOutput {
    pub terminal_id: String,
    pub data: String,
    pub is_stderr: bool,
}

pub struct TerminalManager {
    terminals: Arc<Mutex<HashMap<String, Terminal>>>,
    active_terminal: Arc<Mutex<Option<String>>>,
}

impl TerminalManager {
    pub fn new() -> Self {
        Self {
            terminals: Arc::new(Mutex::new(HashMap::new())),
            active_terminal: Arc::new(Mutex::new(None)),
        }
    }

    pub async fn create_terminal(&self, connection_id: &str, title: Option<String>) -> Result<String> {
        let title = title.unwrap_or_else(|| format!("Terminal-{}", Uuid::new_v4()));
        
        let terminal = Terminal {
            id: Uuid::new_v4().to_string(),
            connection_id: connection_id.to_string(),
            title,
            is_active: true,
            created_at: chrono::Utc::now(),
            last_activity: chrono::Utc::now(),
        };
        
        let terminal_id = terminal.id.clone();
        self.terminals.lock().unwrap().insert(terminal_id.clone(), terminal);
        
        // Set as active terminal
        *self.active_terminal.lock().unwrap() = Some(terminal_id.clone());
        
        Ok(terminal_id)
    }

    pub async fn close_terminal(&self, terminal_id: &str) -> Result<()> {
        self.terminals.lock().unwrap().remove(terminal_id);
        
        // If this was the active terminal, clear it
        let mut active = self.active_terminal.lock().unwrap();
        if active.as_deref() == Some(terminal_id) {
            *active = None;
        }
        
        Ok(())
    }

    pub async fn set_active_terminal(&self, terminal_id: &str) -> Result<()> {
        let mut active = self.active_terminal.lock().unwrap();
        *active = Some(terminal_id.to_string());
        
        // Update terminal activity
        if let Some(terminal) = self.terminals.lock().unwrap().get_mut(terminal_id) {
            terminal.is_active = true;
            terminal.last_activity = chrono::Utc::now();
        }
        
        Ok(())
    }

    pub async fn get_active_terminal(&self) -> Option<Terminal> {
        let active_id = self.active_terminal.lock().unwrap().clone()?;
        self.terminals.lock().unwrap().get(&active_id).cloned()
    }

    pub async fn list_terminals(&self) -> Vec<Terminal> {
        let terminals = self.terminals.lock().unwrap();
        terminals.values().cloned().collect()
    }

    pub async fn get_terminal(&self, terminal_id: &str) -> Option<Terminal> {
        self.terminals.lock().unwrap().get(terminal_id).cloned()
    }

    pub async fn get_terminals_for_connection(&self, connection_id: &str) -> Vec<Terminal> {
        let terminals = self.terminals.lock().unwrap();
        terminals.values()
            .filter(|term| term.connection_id == connection_id)
            .cloned()
            .collect()
    }

    pub async fn update_terminal_title(&self, terminal_id: &str, title: String) -> Result<()> {
        if let Some(terminal) = self.terminals.lock().unwrap().get_mut(terminal_id) {
            terminal.title = title;
            Ok(())
        } else {
            Err(anyhow!("Terminal not found"))
        }
    }
}

#[tauri::command]
pub async fn create_terminal(
    app_handle: AppHandle,
    connection_id: String,
    title: Option<String>,
) -> Result<String, String> {
    let terminal_manager: State<TerminalManager> = app_handle.state();
    terminal_manager.create_terminal(&connection_id, title).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn close_terminal(
    app_handle: AppHandle,
    terminal_id: String,
) -> Result<(), String> {
    let terminal_manager: State<TerminalManager> = app_handle.state();
    terminal_manager.close_terminal(&terminal_id).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn set_active_terminal(
    app_handle: AppHandle,
    terminal_id: String,
) -> Result<(), String> {
    let terminal_manager: State<TerminalManager> = app_handle.state();
    terminal_manager.set_active_terminal(&terminal_id).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_active_terminal(app_handle: AppHandle) -> Result<Option<Terminal>, String> {
    let terminal_manager: State<TerminalManager> = app_handle.state();
    Ok(terminal_manager.get_active_terminal().await)
}

#[tauri::command]
pub async fn list_terminals(app_handle: AppHandle) -> Result<Vec<Terminal>, String> {
    let terminal_manager: State<TerminalManager> = app_handle.state();
    Ok(terminal_manager.list_terminals().await)
}

