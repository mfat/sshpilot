use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};
use anyhow::{Result, anyhow};
use std::fs;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppConfig {
    pub theme: Theme,
    pub terminal: TerminalConfig,
    pub ssh: SSHConfig,
    pub ui: UIConfig,
    pub advanced: AdvancedConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum Theme {
    System,
    Light,
    Dark,
}

impl Theme {
    pub fn as_str(&self) -> &'static str {
        match self {
            Theme::System => "system",
            Theme::Light => "light",
            Theme::Dark => "dark",
        }
    }
    
    pub fn from_str(s: &str) -> Self {
        match s {
            "light" => Theme::Light,
            "dark" => Theme::Dark,
            _ => Theme::System,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TerminalConfig {
    pub font_family: String,
    pub font_size: u32,
    pub background_color: String,
    pub foreground_color: String,
    pub cursor_color: String,
    pub scrollback_lines: u32,
    pub bell_style: BellStyle,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum BellStyle {
    None,
    Visual,
    Audible,
    Both,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SSHConfig {
    pub default_port: u16,
    pub connection_timeout: u32,
    pub keepalive_interval: u32,
    pub max_connections: u32,
    pub key_authentication: bool,
    pub password_authentication: bool,
    pub x11_forwarding: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UIConfig {
    pub sidebar_width: u32,
    pub show_connection_status: bool,
    pub show_terminal_tabs: bool,
    pub auto_save_connections: bool,
    pub confirm_connection_close: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AdvancedConfig {
    pub debug_mode: bool,
    pub log_level: String,
    pub max_log_size: u64,
    pub auto_update: bool,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            theme: Theme::System,
            terminal: TerminalConfig {
                font_family: "Monaco, 'DejaVu Sans Mono', monospace".to_string(),
                font_size: 14,
                background_color: "#000000".to_string(),
                foreground_color: "#ffffff".to_string(),
                cursor_color: "#ffffff".to_string(),
                scrollback_lines: 10000,
                bell_style: BellStyle::Visual,
            },
            ssh: SSHConfig {
                default_port: 22,
                connection_timeout: 30,
                keepalive_interval: 60,
                max_connections: 10,
                key_authentication: true,
                password_authentication: true,
                x11_forwarding: false,
            },
            ui: UIConfig {
                sidebar_width: 250,
                show_connection_status: true,
                show_terminal_tabs: true,
                auto_save_connections: true,
                confirm_connection_close: true,
            },
            advanced: AdvancedConfig {
                debug_mode: false,
                log_level: "info".to_string(),
                max_log_size: 10 * 1024 * 1024, // 10MB
                auto_update: true,
            },
        }
    }
}

pub struct Config {
    config_path: PathBuf,
    config: Arc<Mutex<AppConfig>>,
}

impl Config {
    pub fn new(app_handle: AppHandle) -> Self {
        let config_path = Self::get_config_path();
        let config = Arc::new(Mutex::new(AppConfig::default()));
        
        let config_instance = Self {
            config_path,
            config: config.clone(),
        };
        
        // Load configuration in background
        let config_clone = config_instance.clone();
        tokio::spawn(async move {
            if let Err(e) = config_clone.load_config().await {
                eprintln!("Failed to load config: {}", e);
            }
        });
        
        config_instance
    }

    fn get_config_path() -> PathBuf {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home).join(".config").join("sshpilot").join("config.json")
    }

    pub async fn load_config(&self) -> Result<()> {
        let config_dir = self.config_path.parent().unwrap();
        fs::create_dir_all(config_dir)?;
        
        if !self.config_path.exists() {
            // Create default config
            self.save_config().await?;
            return Ok(());
        }
        
        let content = fs::read_to_string(&self.config_path)?;
        let loaded_config: AppConfig = serde_json::from_str(&content)?;
        
        *self.config.lock().unwrap() = loaded_config;
        Ok(())
    }

    pub async fn save_config(&self) -> Result<()> {
        let config_dir = self.config_path.parent().unwrap();
        fs::create_dir_all(config_dir)?;
        
        let config = self.config.lock().unwrap();
        let content = serde_json::to_string_pretty(&*config)?;
        
        fs::write(&self.config_path, content)?;
        Ok(())
    }

    pub async fn get_setting(&self, key: &str) -> Option<String> {
        let config = self.config.lock().unwrap();
        
        match key {
            "theme" => Some(config.theme.as_str().to_string()),
            "terminal.font_family" => Some(config.terminal.font_family.clone()),
            "terminal.font_size" => Some(config.terminal.font_size.to_string()),
            "terminal.background_color" => Some(config.terminal.background_color.clone()),
            "terminal.foreground_color" => Some(config.terminal.foreground_color.clone()),
            "ssh.default_port" => Some(config.ssh.default_port.to_string()),
            "ssh.connection_timeout" => Some(config.ssh.connection_timeout.to_string()),
            "ui.sidebar_width" => Some(config.ui.sidebar_width.to_string()),
            "ui.show_connection_status" => Some(config.ui.show_connection_status.to_string()),
            "advanced.debug_mode" => Some(config.advanced.debug_mode.to_string()),
            _ => None,
        }
    }

    pub async fn set_setting(&self, key: &str, value: &str) -> Result<()> {
        let mut config = self.config.lock().unwrap();
        
        match key {
            "theme" => {
                config.theme = Theme::from_str(value);
            }
            "terminal.font_family" => {
                config.terminal.font_family = value.to_string();
            }
            "terminal.font_size" => {
                config.terminal.font_size = value.parse().unwrap_or(14);
            }
            "terminal.background_color" => {
                config.terminal.background_color = value.to_string();
            }
            "terminal.foreground_color" => {
                config.terminal.foreground_color = value.to_string();
            }
            "ssh.default_port" => {
                config.ssh.default_port = value.parse().unwrap_or(22);
            }
            "ssh.connection_timeout" => {
                config.ssh.connection_timeout = value.parse().unwrap_or(30);
            }
            "ui.sidebar_width" => {
                config.ui.sidebar_width = value.parse().unwrap_or(250);
            }
            "ui.show_connection_status" => {
                config.ui.show_connection_status = value.parse().unwrap_or(true);
            }
            "advanced.debug_mode" => {
                config.advanced.debug_mode = value.parse().unwrap_or(false);
            }
            _ => {
                return Err(anyhow!("Unknown setting key: {}", key));
            }
        }
        
        drop(config);
        self.save_config().await?;
        Ok(())
    }

    pub async fn reset_config(&self) -> Result<()> {
        *self.config.lock().unwrap() = AppConfig::default();
        self.save_config().await?;
        Ok(())
    }

    pub async fn get_config(&self) -> AppConfig {
        self.config.lock().unwrap().clone()
    }
}

impl Clone for Config {
    fn clone(&self) -> Self {
        Self {
            config_path: self.config_path.clone(),
            config: self.config.clone(),
        }
    }
}

#[tauri::command]
pub async fn get_setting(
    app_handle: AppHandle,
    key: String,
) -> Result<Option<String>, String> {
    let config: State<Config> = app_handle.state();
    Ok(config.get_setting(&key).await)
}

#[tauri::command]
pub async fn set_setting(
    app_handle: AppHandle,
    key: String,
    value: String,
) -> Result<(), String> {
    let config: State<Config> = app_handle.state();
    config.set_setting(&key, &value).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn reset_config(app_handle: AppHandle) -> Result<(), String> {
    let config: State<Config> = app_handle.state();
    config.reset_config().await.map_err(|e| e.to_string())
}

