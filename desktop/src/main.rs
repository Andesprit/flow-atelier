mod sidecar;

use std::ffi::{c_char, CStr, CString};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

static WINDOW: OnceLock<(tauri::WebviewWindow, tauri::Url)> = OnceLock::new();

extern "C" {
    fn start_notices(port: u16, token: *const c_char) -> bool;
    fn check_quit();
}

static CHECKING_QUIT: AtomicBool = AtomicBool::new(false);
static QUITTING: AtomicBool = AtomicBool::new(false);

#[no_mangle]
extern "C" fn window_visible() -> bool {
    WINDOW
        .get()
        .is_some_and(|(window, _)| window.is_visible().unwrap_or(false))
}

#[no_mangle]
extern "C" fn confirm_quit() -> bool {
    if WINDOW.get().is_none() || QUITTING.load(Ordering::Relaxed) {
        return true;
    }
    if !CHECKING_QUIT.swap(true, Ordering::Relaxed) {
        unsafe { check_quit() };
    }
    false
}

#[no_mangle]
extern "C" fn quit_status(status: i32) {
    let Some((window, _)) = WINDOW.get() else {
        return;
    };
    let finish = move |quit| {
        if quit {
            QUITTING.store(true, Ordering::Relaxed);
            window.app_handle().exit(0);
        } else {
            CHECKING_QUIT.store(false, Ordering::Relaxed);
        }
    };
    if status == 0 {
        finish(true);
        return;
    }
    let message = if status < 0 {
        "Atelier could not check for active runs. Quit anyway?"
    } else {
        "Runs are active. Quitting will stop any runs owned by this app. Quit Flow Atelier?"
    };
    show_window();
    window
        .dialog()
        .message(message)
        .parent(window)
        .title("Quit Flow Atelier")
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Quit".into(),
            "Keep running".into(),
        ))
        .show(finish);
}

fn open_external(url: &tauri::Url) {
    if matches!(url.scheme(), "http" | "https") {
        let _ = std::process::Command::new("/usr/bin/open")
            .arg(url.as_str())
            .status();
    }
}

fn show_window() {
    if let Some((window, _)) = WINDOW.get() {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

// The macOS notification delegate passes the original run ID, including on a cold launch.
#[no_mangle]
extern "C" fn open_run(id: *const c_char) {
    if let Some((window, base)) = WINDOW.get() {
        let Ok(id) = (unsafe { CStr::from_ptr(id) }).to_str() else {
            return;
        };
        let mut url = base.clone();
        url.path_segments_mut()
            .unwrap()
            .clear()
            .extend(["runs", id]);
        let _ = window.navigate(url);
        show_window();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let helpers = app.path().resource_dir()?.join("atelier/atelier");
            let token = uuid::Uuid::new_v4().to_string();
            let workspace = app.path().app_data_dir()?.join("workspace");
            let (child, port, warning) = sidecar::start(&helpers, &workspace, &token)?;
            app.manage(Mutex::new(Some(child)));
            let url: tauri::Url = format!("http://127.0.0.1:{port}/dashboard").parse()?;
            let origin = url.origin();
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.clone()))
                .title("Flow Atelier").inner_size(1120.0, 760.0)
                .initialization_script(format!(
                    "if (location.origin === 'http://127.0.0.1:{port}') sessionStorage.setItem('atelier:api-token', '{token}');"
                ))
                .on_navigation(move |url| {
                    if url.origin() == origin { return true; }
                    open_external(url);
                    false
                })
                .on_new_window(|url, _| {
                    if let Some((window, base)) = WINDOW.get() {
                        if url.origin() == base.origin() {
                            let _ = window.navigate(url);
                        } else {
                            open_external(&url);
                        }
                    }
                    tauri::webview::NewWindowResponse::Deny
                })
                .build()?;
            let _ = WINDOW.set((window, url));
            let show = MenuItem::with_id(app, "show", "Show Flow Atelier", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Flow Atelier", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Flow Atelier")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => show_window(),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            let token = CString::new(token)?;
            if !unsafe { start_notices(port, token.as_ptr()) } {
                return Err("could not install the macOS Quit handler".into());
            }
            if let Some(warning) = warning {
                app.dialog().message(warning)
                    .title("Agent PATH unavailable").show(|_| {});
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("could not start Flow Atelier")
        .run(|app, event| {
            if let RunEvent::Reopen { .. } = event {
                show_window();
            }
            if let RunEvent::ExitRequested { ref api, .. } = event {
                if !confirm_quit() {
                    api.prevent_exit();
                }
            }
            if let RunEvent::Exit = event {
                if let Some(child) = app.try_state::<Mutex<Option<sidecar::Sidecar>>>() {
                    child.lock().unwrap().take();
                }
            }
        });
}
