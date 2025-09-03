// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{CustomMenuItem, Menu, Submenu};

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! Welcome to SSHPilot", name)
}

#[tauri::command]
fn get_ssh_keys() -> Vec<String> {
    // Placeholder for SSH key discovery
    vec![
        "id_rsa".to_string(),
        "id_ed25519".to_string(),
        "id_ecdsa".to_string(),
    ]
}

#[tauri::command]
fn test_connection(host: &str, port: u16) -> bool {
    // Placeholder for connection testing
    println!("Testing connection to {}:{}", host, port);
    true
}

fn main() {
    // Create application menu
    let quit = CustomMenuItem::new("quit".to_string(), "Quit");
    let close = CustomMenuItem::new("close".to_string(), "Close");
    let file_menu = Submenu::new("File", Menu::new().add_item(quit).add_item(close));

    let new_connection = CustomMenuItem::new("new_connection".to_string(), "New Connection");
    let new_terminal = CustomMenuItem::new("new_terminal".to_string(), "New Terminal");
    let new_key = CustomMenuItem::new("new_key".to_string(), "Generate Key");
    let connection_menu = Submenu::new("Connection", Menu::new().add_item(new_connection).add_item(new_terminal).add_item(new_key));

    let preferences = CustomMenuItem::new("preferences".to_string(), "Preferences");
    let about = CustomMenuItem::new("about".to_string(), "About");
    let help = CustomMenuItem::new("help".to_string(), "Help");
    let tools_menu = Submenu::new("Tools", Menu::new().add_item(preferences).add_item(about).add_item(help));

    let menu = Menu::new()
        .add_submenu(file_menu)
        .add_submenu(connection_menu)
        .add_submenu(tools_menu);

    tauri::Builder::default()
        .menu(menu)
        .invoke_handler(tauri::generate_handler![
            greet,
            get_ssh_keys,
            test_connection
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
