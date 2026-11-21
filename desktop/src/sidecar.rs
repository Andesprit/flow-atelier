use std::env;
use std::ffi::{OsStr, OsString};
use std::io::{self, BufRead, BufReader, Read};
use std::os::unix::{ffi::OsStringExt, process::CommandExt};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

fn shell_path(shell: &OsStr, script: &str, timeout: Duration) -> Option<OsString> {
    // Finder need not export SHELL. Directory Services knows the account's login shell.
    // Keep that lookup inside the same timeout and process group as shell startup.
    let launcher = r#"
        shell=$1
        if [ -z "$shell" ]; then
            account=$(/usr/bin/id -un) || exit 1
            record=$(/usr/bin/dscl /Search -read "/Users/$account" UserShell) || exit 1
            shell=${record#UserShell: }
        fi
        exec "$shell" -ilc "$2"
    "#;
    let mut child = Command::new("/bin/sh")
        .args(["-c", launcher, "atelier-login"])
        .arg(shell)
        .arg(script)
        .env("DISABLE_AUTO_UPDATE", "true")
        .env("ZSH_TMUX_AUTOSTART", "false")
        .process_group(0)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()
        .ok()?;
    let output = child.stdout.take()?;
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let read = output.take(1024 * 1024).read_to_end(&mut bytes);
        let _ = tx.send(read.map(|_| bytes));
    });
    let deadline = Instant::now() + timeout;
    let bytes = rx.recv_timeout(timeout).ok().and_then(Result::ok);
    while bytes.is_some() && Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            let data = bytes.as_deref()?;
            let value = data.split(|b| *b == 0).nth(1)?;
            let value = value.strip_suffix(b"\n").unwrap_or(value);
            return (status.success() && !value.is_empty())
                .then(|| OsString::from_vec(value.to_vec()));
        }
        thread::sleep(Duration::from_millis(10));
    }
    // A startup file can leave a grandchild holding stdout open.
    let _ = Command::new("/bin/kill")
        .args(["-KILL", "--", &format!("-{}", child.id())])
        .status();
    let _ = child.kill();
    let _ = child.wait();
    None
}

fn ready_port(line: &str) -> Option<u16> {
    line.strip_prefix("atelier serve running at http://127.0.0.1:")?
        .parse()
        .ok()
        .filter(|port| *port != 0)
}

pub struct Sidecar(Child);

impl Drop for Sidecar {
    fn drop(&mut self) {
        // EOF gives the server time to cancel runs in their own process groups.
        drop(self.0.stdin.take());
        let deadline = Instant::now() + Duration::from_secs(12);
        while Instant::now() < deadline {
            if matches!(self.0.try_wait(), Ok(Some(_))) {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let _ = Command::new("/bin/kill")
            .args(["-KILL", "--", &format!("-{}", self.0.id())])
            .stderr(Stdio::null())
            .status();
        let _ = self.0.wait();
    }
}

pub fn start(
    executable: &Path,
    workspace: &Path,
    token: &str,
) -> io::Result<(Sidecar, u16, Option<&'static str>)> {
    let mut cmd = Command::new(executable);
    let shell = env::var_os("SHELL").unwrap_or_default();
    let path = shell_path(
        &shell,
        "printf '\\0'; /usr/bin/printenv PATH; printf '\\0'",
        Duration::from_secs(3),
    );
    let shell_failed = path.is_none();
    if let Some(path) = path {
        cmd.env("PATH", path);
    }
    std::fs::create_dir_all(workspace)?;
    let mut sidecar = Sidecar(
        cmd.args([
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--exit-with-stdin",
            "--log-level",
            "warning",
        ])
        .current_dir(workspace)
        .process_group(0)
        .env("ATELIER_API_TOKEN", token)
        .env("NO_COLOR", "1")
        .env("COLUMNS", "200")
        .env("ATELIER_NO_UPDATE_CHECK", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?,
    );
    let stdout = sidecar.0.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut warning = shell_failed.then_some("The login shell did not return PATH. Atelier is using the app’s inherited PATH. Check your shell startup files and reopen Flow Atelier.");
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if line == "No agent harness found on PATH" {
                warning = Some("Atelier could not find an agent harness on PATH. Check your installation and shell startup files, then reopen Flow Atelier.");
            } else if let Some(port) = ready_port(&line) {
                let _ = tx.send((port, warning));
            }
        }
    });
    let (port, warning) = rx
        .recv_timeout(Duration::from_secs(30))
        .map_err(|e| io::Error::other(format!("Atelier server did not report its port: {e}")))?;
    Ok((sidecar, port, warning))
}

#[cfg(test)]
#[path = "sidecar_tests.rs"]
mod tests;
