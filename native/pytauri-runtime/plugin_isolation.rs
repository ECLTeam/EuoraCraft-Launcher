// EuoraCraft Launcher — GPL-3.0, ECLTeam © 2026.
// Narrow Windows policy for plugin-v2 windows; runs entirely on the WebView STA.

use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, LazyLock, Mutex,
};
use std::{
    collections::HashMap,
    io::Write,
    net::{Shutdown, TcpListener},
    thread::{self, JoinHandle},
    time::Duration,
};

use pyo3::prelude::*;
use tauri::{webview::WebviewWindow, WindowEvent};
use webview2_com::{
    CallDevToolsProtocolMethodCompletedHandler, CoTaskMemPWSTR, DocumentTitleChangedEventHandler,
    DownloadStartingEventHandler, ExecuteScriptCompletedHandler,
    LaunchingExternalUriSchemeEventHandler, Microsoft::Web::WebView2::Win32::*,
    NavigationCompletedEventHandler, NavigationStartingEventHandler,
    NewWindowRequestedEventHandler, PermissionRequestedEventHandler, ProcessFailedEventHandler,
    WebResourceRequestedEventHandler,
};
use windows::core::{w, Interface, BOOL, PWSTR};

use crate::tauri_runtime::Runtime;

// Chromium arguments are not a stable WebView2 production API. A different
// runtime must pass the native gates and update the reviewed build manifest.
const TESTED_WEBVIEW2_VERSIONS: &str = env!("ECL_EXPECTED_WEBVIEW2_VERSIONS");

struct DenyProxy {
    alive: Arc<AtomicBool>,
    thread: JoinHandle<()>,
}
static PROXIES: LazyLock<Mutex<HashMap<String, DenyProxy>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

pub(crate) fn proxy_arguments(label: &str, existing: &str) -> PyResult<String> {
    if !label.starts_with("plugin-v2-")
        || !matches!(existing, "" | "--remote-debugging-port=0")
        || std::env::var_os("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS").is_some()
        || std::env::var_os("WEBVIEW2_USER_DATA_FOLDER").is_some()
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "PLUGIN_UI_ISOLATION_UNAVAILABLE",
        ));
    }
    let mut proxies = PROXIES.lock().map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE")
    })?;
    if proxies.contains_key(label) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "PLUGIN_UI_ISOLATION_UNAVAILABLE",
        ));
    }
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE")
    })?;
    listener.set_nonblocking(true).map_err(|_| {
        pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE")
    })?;
    let port = listener
        .local_addr()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE"))?
        .port();
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
    }).map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE"))?;
    proxies.insert(label.to_owned(), DenyProxy { alive, thread });
    // One fixed proxy, no DIRECT fallback, and Chromium's implicit loopback bypass is removed.
    Ok(format!("{existing} --proxy-server=http://127.0.0.1:{port} --proxy-bypass-list=<-loopback> --disable-quic --host-resolver-rules=\"MAP * ~NOTFOUND\" --force-webrtc-ip-handling-policy --webrtc-ip-handling-policy=disable_non_proxied_udp"))
}

pub(crate) fn release_proxy(label: &str) {
    if let Ok(mut proxies) = PROXIES.lock() {
        if let Some(proxy) = proxies.remove(label) {
            proxy.alive.store(false, Ordering::SeqCst);
            let _ = proxy.thread.join();
        }
    }
}

pub(crate) fn track_proxy_lifetime(window: &WebviewWindow<Runtime>) {
    let label = window.label().to_owned();
    window.on_window_event(move |event| {
        if matches!(event, WindowEvent::Destroyed) {
            release_proxy(&label);
        }
    });
}

fn notify(callback: &PyObject, success: bool) {
    Python::with_gil(|py| {
        if let Err(error) = callback.bind(py).call1((success,)) {
            error.write_unraisable(py, Some(callback.bind(py)));
        }
    });
}

fn text(
    get: impl FnOnce(*mut PWSTR) -> windows::core::Result<()>,
) -> windows::core::Result<String> {
    let mut value = PWSTR::null();
    get(&mut value)?;
    Ok(CoTaskMemPWSTR::from(value).to_string())
}

#[derive(Default, Clone, Copy)]
struct Tokens {
    navigation: i64,
    frame_navigation: i64,
    resource: i64,
    popup: i64,
    download: i64,
    permission: i64,
    title: i64,
    completed: i64,
    external: i64,
    process: i64,
}

fn clean(core: &ICoreWebView2, tokens: &Tokens) {
    unsafe {
        let _ = core.remove_NavigationStarting(tokens.navigation);
        let _ = core.remove_DocumentTitleChanged(tokens.title);
        let _ = core.remove_NavigationCompleted(tokens.completed);
        let _ = core.remove_ProcessFailed(tokens.process);
        if let Ok(v18) = core.cast::<ICoreWebView2_18>() {
            let _ = v18.remove_LaunchingExternalUriScheme(tokens.external);
        }
        let _ = core.remove_FrameNavigationStarting(tokens.frame_navigation);
        let _ = core.remove_WebResourceRequested(tokens.resource);
        let _ = core.remove_NewWindowRequested(tokens.popup);
        let _ = core.remove_PermissionRequested(tokens.permission);
        if let Ok(v4) = core.cast::<ICoreWebView2_4>() {
            let _ = v4.remove_DownloadStarting(tokens.download);
        }
        // Tokens are removed before releasing the controller. Failed initialization
        // also closes the blank window; no partially configured guest is retained.
    }
}

fn cdp_chain(
    core: ICoreWebView2,
    methods: Vec<(&'static str, &'static str)>,
    ready: Arc<AtomicBool>,
    on_ready: Arc<PyObject>,
    alive: Arc<AtomicBool>,
) -> windows::core::Result<()> {
    let Some((&(method, parameters), rest)) = methods.split_first() else {
        if alive.load(Ordering::SeqCst) {
            ready.store(true, Ordering::SeqCst);
            notify(&on_ready, true);
        }
        return Ok(());
    };
    let remaining = rest.to_vec();
    let next_core = core.clone();
    let method = CoTaskMemPWSTR::from(method);
    let parameters = CoTaskMemPWSTR::from(parameters);
    unsafe {
        core.CallDevToolsProtocolMethod(
            *method.as_ref().as_pcwstr(),
            *parameters.as_ref().as_pcwstr(),
            &CallDevToolsProtocolMethodCompletedHandler::create(Box::new(move |status, _| {
                if !alive.load(Ordering::SeqCst) {
                    return Ok(());
                }
                if status.is_err() {
                    notify(&on_ready, false);
                } else if cdp_chain(next_core, remaining, ready, on_ready.clone(), alive).is_err() {
                    notify(&on_ready, false);
                }
                Ok(())
            })),
        )
    }
}

pub(crate) fn install(
    window: WebviewWindow<Runtime>,
    shell_url: String,
    on_message: PyObject,
    on_ready: PyObject,
) -> PyResult<()> {
    if !PROXIES
        .lock()
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE"))?
        .contains_key(window.label())
        || !window.label().starts_with("plugin-v2-")
        || !shell_url.starts_with("data:text/html;base64,")
        || shell_url.len() > 2_097_152
        || window
            .url()
            .map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE")
            })?
            .as_str()
            != "about:blank"
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "PLUGIN_UI_ISOLATION_UNAVAILABLE",
        ));
    }
    let close_window = window.clone();
    let lifetime_window = window.clone();
    window.with_webview(move |platform| {
        let alive = Arc::new(AtomicBool::new(true));
        let ready = Arc::new(AtomicBool::new(false));
        let on_ready = Arc::new(on_ready);
        let mut tokens = Tokens::default();
        let controller = platform.controller();
        let result = (|| -> windows::core::Result<ICoreWebView2> {
            let environment = platform.environment();
            let browser_version = text(|value| unsafe { environment.BrowserVersionString(value) })?;
            if !TESTED_WEBVIEW2_VERSIONS.split(',').any(|version| version == browser_version) {
                return Err(windows::core::Error::from_hresult(windows::core::HRESULT(0x80004001u32 as i32)));
            }
            let core = unsafe { controller.CoreWebView2()? };
            // ALL source kinds includes documents, dedicated/shared/service workers.
            let v22 = core.cast::<ICoreWebView2_22>()?;
            let v4 = core.cast::<ICoreWebView2_4>()?;
            let v18 = core.cast::<ICoreWebView2_18>()?;
            let settings = unsafe { core.Settings()? };
            unsafe {
                // This disables Wry/Tauri's generic message transport natively.
                // The trusted shell uses top-level title signals and ExecuteScript with a
                // per-window credential, never general pyfunc or native invoke.
                settings.SetIsWebMessageEnabled(false)?;
                settings.SetAreHostObjectsAllowed(false)?;
                settings.SetAreDefaultContextMenusEnabled(false)?;
                settings.SetAreDevToolsEnabled(false)?;
                settings.SetIsStatusBarEnabled(false)?;
                settings.SetAreDefaultScriptDialogsEnabled(false)?;
                v18.add_LaunchingExternalUriScheme(&LaunchingExternalUriSchemeEventHandler::create(Box::new(|_, args| {
                    if let Some(args) = args { args.SetCancel(true)?; }
                    Ok(())
                })), &mut tokens.external)?;
                let exists = alive.clone();
                let failed_window = close_window.clone();
                let notify_ready = on_ready.clone();
                core.add_ProcessFailed(&ProcessFailedEventHandler::create(Box::new(move |_, _| {
                    exists.store(false, Ordering::SeqCst);
                    notify(&notify_ready, false);
                    let _ = failed_window.close();
                    Ok(())
                })), &mut tokens.process)?;
                settings.cast::<ICoreWebView2Settings4>()?.SetIsGeneralAutofillEnabled(false)?;
                settings.cast::<ICoreWebView2Settings4>()?.SetIsPasswordAutosaveEnabled(false)?;
                let on_message = Arc::new(on_message);
                let expected = shell_url.clone();
                let enabled = ready.clone();
                let exists = alive.clone();
                let navigation_id = Arc::new(AtomicU64::new(0));
                let active_navigation = navigation_id.clone();
                let document_loaded = Arc::new(AtomicBool::new(false));
                let shell_started = Arc::new(AtomicBool::new(false));
                core.add_NavigationStarting(&NavigationStartingEventHandler::create(Box::new(move |_, args| {
                    let Some(args) = args else { return Ok(()); };
                    args.SetCancel(true)?;
                    if !exists.load(Ordering::SeqCst) || !enabled.load(Ordering::SeqCst) { return Ok(()); }
                    let uri = text(|value| args.Uri(value))?;
                    if uri == expected && !shell_started.swap(true, Ordering::SeqCst) {
                        let mut id = 0; args.NavigationId(&mut id)?;
                        active_navigation.store(id, Ordering::SeqCst);
                        return args.SetCancel(false);
                    }
                    Ok(())
                })), &mut tokens.navigation)?;
                let exists = alive.clone();
                let loaded = document_loaded.clone();
                let callback = on_message.clone();
                core.add_NavigationCompleted(&NavigationCompletedEventHandler::create(Box::new(move |_, args| {
                    let Some(args) = args else { return Ok(()); };
                    let mut id = 0; let mut success = BOOL(0);
                    args.NavigationId(&mut id)?; args.IsSuccess(&mut success)?;
                    if exists.load(Ordering::SeqCst) && success.as_bool() && id != 0
                        && id == navigation_id.load(Ordering::SeqCst) {
                        loaded.store(true, Ordering::SeqCst);
                        Python::with_gil(|py| {
                            if let Err(error) = callback.bind(py).call1(("ecl-plugin-ready://loaded",)) {
                                error.write_unraisable(py, Some(callback.bind(py)));
                            }
                        });
                    }
                    Ok(())
                })), &mut tokens.completed)?;
                let exists = alive.clone();
                let loaded = document_loaded.clone();
                core.add_DocumentTitleChanged(&DocumentTitleChangedEventHandler::create(Box::new(move |sender, _| {
                    if !exists.load(Ordering::SeqCst) || !loaded.load(Ordering::SeqCst) { return Ok(()); }
                    let Some(sender) = sender else { return Ok(()); };
                    let title = text(|value| sender.DocumentTitle(value))?;
                    if !title.starts_with("ecl-plugin-message://request/") || title.len() > 200 { return Ok(()); }
                    let callback = on_message.clone();
                    let running = exists.clone();
                    // ExecuteScript always reads the immutable TOP document. Child
                    // frames cannot replace this function through the opaque origin.
                    sender.ExecuteScript(w!("globalThis.__eclTakeRequest()"),
                        &ExecuteScriptCompletedHandler::create(Box::new(move |status, result| {
                            if status.is_ok() && running.load(Ordering::SeqCst) {
                                if let Ok(uri) = serde_json::from_str::<String>(&result) {
                                    if uri.starts_with("ecl-plugin-message://request/") && uri.len() <= 100_000 {
                                        Python::with_gil(|py| {
                                            if let Err(error) = callback.bind(py).call1((uri,)) {
                                                error.write_unraisable(py, Some(callback.bind(py)));
                                            }
                                        });
                                    }
                                }
                            }
                            Ok(())
                        })))?;
                    Ok(())
                })), &mut tokens.title)?;
                core.add_FrameNavigationStarting(&NavigationStartingEventHandler::create(Box::new(move |_, args| {
                    if let Some(args) = args {
                        let uri = text(|value| args.Uri(value))?;
                        // Only opaque inline frames may execute; no local file,
                        // external scheme or external document navigation survives.
                        args.SetCancel(uri != "about:blank" && uri != "about:srcdoc")?;
                    }
                    Ok(())
                })), &mut tokens.frame_navigation)?;
                core.add_WebResourceRequested(&WebResourceRequestedEventHandler::create(Box::new(move |_, args| {
                    if let Some(args) = args {
                        let denied = environment.CreateWebResourceResponse(None, 403, w!("Blocked"), w!("Content-Type: text/plain\r\n"))?;
                        args.SetResponse(&denied)?;
                    }
                    Ok(())
                })), &mut tokens.resource)?;
                v22.AddWebResourceRequestedFilterWithRequestSourceKinds(w!("*"), COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL, COREWEBVIEW2_WEB_RESOURCE_REQUEST_SOURCE_KINDS_ALL)?;
                core.add_NewWindowRequested(&NewWindowRequestedEventHandler::create(Box::new(|_, args| {
                    if let Some(args) = args { args.SetHandled(true)?; }
                    Ok(())
                })), &mut tokens.popup)?;
                v4.add_DownloadStarting(&DownloadStartingEventHandler::create(Box::new(|_, args| {
                    if let Some(args) = args { args.SetCancel(true)?; args.SetHandled(true)?; }
                    Ok(())
                })), &mut tokens.download)?;
                core.add_PermissionRequested(&PermissionRequestedEventHandler::create(Box::new(|_, args| {
                    if let Some(args) = args { args.SetState(COREWEBVIEW2_PERMISSION_STATE_DENY)?; }
                    Ok(())
                })), &mut tokens.permission)?;
            }
            Ok(core)
        })();
        let core = match result {
            Ok(core) => core,
            Err(_) => {
                alive.store(false, Ordering::SeqCst);
                if let Ok(core) = unsafe { controller.CoreWebView2() } { clean(&core, &tokens); }
                notify(&on_ready, false);
                let _ = close_window.close();
                return;
            }
        };
        let exists = alive.clone();
        let cleanup_started = Arc::new(AtomicBool::new(false));
        let cleanup_window = lifetime_window.clone();
        lifetime_window.on_window_event(move |event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if !cleanup_started.swap(true, Ordering::SeqCst) {
                    exists.store(false, Ordering::SeqCst);
                    // Remove tokens while the controller is still alive, then
                    // reissue close. The second request proceeds normally.
                    api.prevent_close();
                    let closed = cleanup_window.clone();
                    let _ = cleanup_window.with_webview(move |platform| {
                        if let Ok(core) = unsafe { platform.controller().CoreWebView2() } { clean(&core, &tokens); }
                        let _ = closed.close();
                    });
                }
            } else if matches!(event, WindowEvent::Destroyed) {
                exists.store(false, Ordering::SeqCst);
            }
        });
        let methods = vec![
            ("Page.setInterceptFileChooserDialog", "{\"enabled\":true}"),
            ("Network.enable", "{}"),
            ("Network.setCacheDisabled", "{\"cacheDisabled\":true}"),
            ("Network.setBlockedURLs", "{\"urls\":[\"http:*\",\"https:*\",\"ws:*\",\"wss:*\",\"file:*\",\"ftp:*\"]}"),
            ("Network.emulateNetworkConditions", "{\"offline\":true,\"latency\":0,\"downloadThroughput\":0,\"uploadThroughput\":0}"),
        ];
        if cdp_chain(core, methods, ready, on_ready.clone(), alive).is_err() {
            notify(&on_ready, false);
            let _ = close_window.close();
        }
    }).map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("PLUGIN_UI_ISOLATION_UNAVAILABLE"))
}
