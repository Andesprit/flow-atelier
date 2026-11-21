mod sidecar;

use std::ffi::{c_char, CStr};
use std::sync::{Mutex, OnceLock};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;

static WINDOW: OnceLock<(tauri::WebviewWindow, tauri::Url)> = OnceLock::new();

extern "C" {
    fn start_notices(port: u16);
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
            let (child, port, fallback) = sidecar::start(&helpers, &app.path().home_dir()?)?;
            app.manage(Mutex::new(Some(child)));
            let url: tauri::Url = format!("http://127.0.0.1:{port}/dashboard").parse()?;
            let origin = url.origin();
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.clone()))
                .title("Flow Atelier").inner_size(1120.0, 760.0)
                .on_navigation(move |url| url.origin() == origin)
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
            unsafe { start_notices(port) };
            if fallback {
                app.dialog().message("The login shell did not return PATH. Atelier is using the app’s inherited PATH. If an agent is missing, check your login shell startup files and reopen Flow Atelier.")
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
            if let RunEvent::Exit = event {
                if let Some(child) = app.try_state::<Mutex<Option<sidecar::Sidecar>>>() {
                    child.lock().unwrap().take();
                }
            }
        });
}
