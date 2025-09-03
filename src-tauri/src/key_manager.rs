use std::path::{Path, PathBuf};
use std::collections::HashMap;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};
use anyhow::{Result, anyhow};
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SSHKey {
    pub id: String,
    pub name: String,
    pub private_path: String,
    pub public_path: String,
    pub key_type: KeyType,
    pub key_size: Option<u32>,
    pub comment: Option<String>,
    pub has_passphrase: bool,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub last_used: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum KeyType {
    Ed25519,
    RSA,
    ECDSA,
    DSA,
}

impl KeyType {
    pub fn as_str(&self) -> &'static str {
        match self {
            KeyType::Ed25519 => "ed25519",
            KeyType::RSA => "rsa",
            KeyType::ECDSA => "ecdsa",
            KeyType::DSA => "dsa",
        }
    }
    
    pub fn from_str(s: &str) -> Option<Self> {
        match s {
            "ed25519" => Some(KeyType::Ed25519),
            "rsa" => Some(KeyType::RSA),
            "ecdsa" => Some(KeyType::ECDSA),
            "dsa" => Some(KeyType::DSA),
            _ => None,
        }
    }
}

pub struct KeyManager {
    ssh_dir: PathBuf,
    keys: HashMap<String, SSHKey>,
}

impl KeyManager {
    pub fn new() -> Self {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        let ssh_dir = PathBuf::from(home).join(".ssh");
        
        Self {
            ssh_dir,
            keys: HashMap::new(),
        }
    }

    pub async fn discover_keys(&mut self) -> Result<Vec<SSHKey>> {
        if !self.ssh_dir.exists() {
            return Ok(Vec::new());
        }
        
        let mut keys = Vec::new();
        
        for entry in std::fs::read_dir(&self.ssh_dir)? {
            let entry = entry?;
            let path = entry.path();
            
            if !path.is_file() {
                continue;
            }
            
            let name = path.file_name().unwrap().to_string_lossy();
            
            // Skip common non-key files
            if name == "config" || name == "known_hosts" || name == "authorized_keys" {
                continue;
            }
            
            // Skip public key files (we'll find them when processing private keys)
            if name.ends_with(".pub") {
                continue;
            }
            
            // Check if public key exists
            let public_path = path.with_extension("pub");
            if !public_path.exists() {
                continue;
            }
            
            // Determine key type from file extension or content
            let key_type = self.detect_key_type(&path)?;
            
            let key = SSHKey {
                id: Uuid::new_v4().to_string(),
                name: name.to_string(),
                private_path: path.to_string_lossy().to_string(),
                public_path: public_path.to_string_lossy().to_string(),
                key_type,
                key_size: None, // Would need to parse key to get size
                comment: None,
                has_passphrase: self.check_key_has_passphrase(&path)?,
                created_at: chrono::Utc::now(), // Would need to get file creation time
                last_used: None,
            };
            
            keys.push(key.clone());
            self.keys.insert(key.id.clone(), key);
        }
        
        Ok(keys)
    }

    fn detect_key_type(&self, path: &Path) -> Result<KeyType> {
        // Try to detect from filename first
        let name = path.file_name().unwrap().to_string_lossy();
        
        if name.contains("ed25519") {
            return Ok(KeyType::Ed25519);
        } else if name.contains("rsa") {
            return Ok(KeyType::RSA);
        } else if name.contains("ecdsa") {
            return Ok(KeyType::ECDSA);
        } else if name.contains("dsa") {
            return Ok(KeyType::DSA);
        }
        
        // Default to Ed25519 for modern keys
        Ok(KeyType::Ed25519)
    }

    fn check_key_has_passphrase(&self, path: &Path) -> Result<bool> {
        // This is a simplified check - in practice you'd need to try to read the key
        // and see if it requires a passphrase
        Ok(false)
    }

    pub async fn generate_key(
        &mut self,
        name: String,
        key_type: KeyType,
        key_size: Option<u32>,
        comment: Option<String>,
        passphrase: Option<String>,
    ) -> Result<SSHKey> {
        // Ensure SSH directory exists
        std::fs::create_dir_all(&self.ssh_dir)?;
        
        // Set proper permissions on SSH directory
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&self.ssh_dir)?.permissions();
            perms.set_mode(0o700);
            std::fs::set_permissions(&self.ssh_dir, perms)?;
        }
        
        let private_path = self.ssh_dir.join(&name);
        let public_path = private_path.with_extension("pub");
        
        // Check if key already exists
        if private_path.exists() {
            return Err(anyhow!("Key file '{}' already exists", name));
        }
        
        // Generate key using ssh-keygen
        let mut cmd = std::process::Command::new("ssh-keygen");
        cmd.arg("-t").arg(key_type.as_str());
        
        if let Some(size) = key_size {
            if key_type == KeyType::RSA {
                cmd.arg("-b").arg(size.to_string());
            }
        }
        
        if let Some(comment_val) = &comment {
            cmd.arg("-C").arg(comment_val);
        }
        
        if let Some(pass) = &passphrase {
            cmd.arg("-N").arg(pass);
        } else {
            cmd.arg("-N").arg(""); // Empty passphrase
        }
        
        cmd.arg("-f").arg(&private_path);
        
        let output = cmd.output()?;
        
        if !output.status.success() {
            let error = String::from_utf8_lossy(&output.stderr);
            return Err(anyhow!("Failed to generate key: {}", error));
        }
        
        // Set proper permissions on private key
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut perms = std::fs::metadata(&private_path)?.permissions();
            perms.set_mode(0o600);
            std::fs::set_permissions(&private_path, perms)?;
        }
        
        let key = SSHKey {
            id: Uuid::new_v4().to_string(),
            name,
            private_path: private_path.to_string_lossy().to_string(),
            public_path: public_path.to_string_lossy().to_string(),
            key_type,
            key_size,
            comment,
            has_passphrase: passphrase.is_some(),
            created_at: chrono::Utc::now(),
            last_used: None,
        };
        
        self.keys.insert(key.id.clone(), key.clone());
        Ok(key)
    }

    pub async fn delete_key(&mut self, key_id: &str) -> Result<()> {
        if let Some(key) = self.keys.get(key_id) {
            // Remove files
            let _ = std::fs::remove_file(&key.private_path);
            let _ = std::fs::remove_file(&key.public_path);
            
            // Remove from memory
            self.keys.remove(key_id);
        }
        
        Ok(())
    }

    pub async fn list_keys(&self) -> Vec<SSHKey> {
        self.keys.values().cloned().collect()
    }

    pub async fn get_key(&self, key_id: &str) -> Option<SSHKey> {
        self.keys.get(key_id).cloned()
    }
}

#[tauri::command]
pub async fn list_keys(app_handle: AppHandle) -> Result<Vec<SSHKey>, String> {
    let mut key_manager: State<KeyManager> = app_handle.state();
    key_manager.discover_keys().await.map_err(|e| e.to_string())?;
    Ok(key_manager.list_keys().await)
}

#[tauri::command]
pub async fn generate_key(
    app_handle: AppHandle,
    name: String,
    key_type: String,
    key_size: Option<u32>,
    comment: Option<String>,
    passphrase: Option<String>,
) -> Result<SSHKey, String> {
    let mut key_manager: State<KeyManager> = app_handle.state();
    let key_type = KeyType::from_str(&key_type)
        .ok_or_else(|| "Invalid key type".to_string())?;
    
    key_manager.generate_key(name, key_type, key_size, comment, passphrase)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn delete_key(
    app_handle: AppHandle,
    key_id: String,
) -> Result<(), String> {
    let mut key_manager: State<KeyManager> = app_handle.state();
    key_manager.delete_key(&key_id).await.map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn import_key(
    app_handle: AppHandle,
    private_path: String,
    public_path: Option<String>,
) -> Result<SSHKey, String> {
    // This would implement key import functionality
    Err("Key import not yet implemented".to_string())
}

