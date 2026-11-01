// EuoraCraft Launcher — GPL-3.0, ECLTeam © 2026.
// Own the deny proxy and the lifetime shared with each native WebKit policy.

use std::{
    collections::HashMap,
    io::Write,
    net::{Shutdown, TcpListener},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, LazyLock, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

use crate::tauri_runtime::Runtime;
use pyo3::prelude::*;
use tauri::{webview::WebviewWindow, WindowEvent};

pub(crate) struct Policy {
    pub alive: AtomicBool,
    pub loaded: AtomicBool,
    pub shell: String,
    pub message: PyObject,
}

impl Policy {
    pub fn send(&self, value: &str) {
        if !self.alive.load(Ordering::SeqCst) || value.len() > 100_000 {
            return;
        }
        if value != "ecl-plugin-ready://loaded"
            && !value.starts_with("ecl-plugin-message://request/")
        {
            return;
        }
        Python::with_gil(|py| {
            if let Err(error) = self.message.bind(py).call1((value,)) {
                error.write_unraisable(py, Some(self.message.bind(py)));
            }
        });
    }
}

struct DenyProxy {
    alive: Arc<AtomicBool>,
    thread: JoinHandle<()>,
    policy: Option<Arc<Policy>>,
}

static PROXIES: LazyLock<Mutex<HashMap<String, DenyProxy>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

pub(crate) fn unavailable() -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE")
}

pub(crate) fn notify(callback: &PyObject, success: bool) {
    Python::with_gil(|py| {
        if let Err(error) = callback.bind(py).call1((success,)) {
            error.write_unraisable(py, Some(callback.bind(py)));
        }
    });
}

pub(crate) fn proxy_url(label: &str, existing: &str) -> PyResult<tauri::Url> {
    if !label.starts_with("plugin-v2-")
        || label.len() > 80
        || !label
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        || !existing.is_empty()
    {
        return Err(unavailable());
    }
    let mut proxies = PROXIES.lock().map_err(|_| unavailable())?;
    if proxies.contains_key(label) {
        return Err(unavailable());
    }
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| unavailable())?;
    listener.set_nonblocking(true).map_err(|_| unavailable())?;
    let port = listener.local_addr().map_err(|_| unavailable())?.port();
    let alive = Arc::new(AtomicBool::new(true));
    let running = alive.clone();
    let thread = thread::Builder::new().name("plugin-deny-proxy".into()).spawn(move || {
        while running.load(Ordering::SeqCst) {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let _ = stream.set_write_timeout(Some(Duration::from_millis(100)));
                    let _ = stream.write_all(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
                    let _ = stream.shutdown(Shutdown::Both);
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(10)),
                Err(_) => break,
            }
        }
    }).map_err(|_| unavailable())?;
    proxies.insert(
        label.to_owned(),
        DenyProxy {
            alive,
            thread,
            policy: None,
        },
    );
    tauri::Url::parse(&format!("http://127.0.0.1:{port}")).map_err(|_| unavailable())
}

pub(crate) fn release_proxy(label: &str) {
    let proxy = PROXIES
        .lock()
        .ok()
        .and_then(|mut proxies| proxies.remove(label));
    if let Some(proxy) = proxy {
        if let Some(policy) = proxy.policy {
            policy.alive.store(false, Ordering::SeqCst);
        }
        proxy.alive.store(false, Ordering::SeqCst);
        let _ = proxy.thread.join();
    }
}

pub(crate) fn track_proxy_lifetime(window: &WebviewWindow<Runtime>) {
    let label = window.label().to_owned();
    window.on_window_event(move |event| {
        if matches!(
            event,
            WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed
        ) {
            release_proxy(&label);
        }
    });
}

pub(crate) fn install(
    window: WebviewWindow<Runtime>,
    shell: String,
    message: PyObject,
    ready: PyObject,
) -> PyResult<()> {
    if shell.len() > 2_000_000
        || !shell.starts_with("data:text/html;base64,")
        || window.url().map_err(|_| unavailable())?.as_str() != "about:blank"
    {
        return Err(unavailable());
    }
    let policy = Arc::new(Policy {
        alive: AtomicBool::new(true),
        loaded: AtomicBool::new(false),
        shell,
        message,
    });
    {
        let mut proxies = PROXIES.lock().map_err(|_| unavailable())?;
        let proxy = proxies.get_mut(window.label()).ok_or_else(unavailable)?;
        if proxy.policy.is_some() {
            return Err(unavailable());
        }
        proxy.policy = Some(policy.clone());
    }
    let close = window.clone();
    window
        .with_webview(move |platform| {
            #[cfg(target_os = "linux")]
            crate::plugin_isolation_gtk::install(platform.inner(), policy, ready, close);
            #[cfg(target_os = "macos")]
            unsafe {
                crate::plugin_isolation_macos::install(platform.inner(), policy, ready, close);
            }
        })
        .map_err(|_| unavailable())
}
