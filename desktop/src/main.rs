mod sidecar;

use std::sync::Mutex;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let helpers = app.path().resource_dir()?.join("atelier/atelier");
            let (child, port, fallback) = sidecar::start(&helpers, &app.path().home_dir()?)?;
            app.manage(Mutex::new(Some(child)));
            let url: tauri::Url = format!("http://127.0.0.1:{port}/dashboard").parse()?;
            let origin = url.origin();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("Flow Atelier").inner_size(1120.0, 760.0)
                .on_navigation(move |url| url.origin() == origin)
                .build()?;
            if fallback {
                app.dialog().message("The login shell did not return PATH. Atelier is using the app’s inherited PATH. If an agent is missing, check your login shell startup files and reopen Flow Atelier.")
                    .title("Agent PATH unavailable").show(|_| {});
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("could not start Flow Atelier")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(child) = app.try_state::<Mutex<Option<sidecar::Sidecar>>>() {
                    child.lock().unwrap().take();
                }
            }
        });
}
