// EuoraCraft Launcher — GPL-3.0, ECLTeam © 2026.
// Native WebKitGTK policy; JavaScript in a guest never owns these controls.

use crate::{
    plugin_isolation_unix::{notify, Policy},
    tauri_runtime::Runtime,
};
use glib::{prelude::*, translate::*};
use java_script_core::ValueExt;
use pyo3::prelude::*;
use std::{
    ffi::{c_void, CString},
    fs,
    os::unix::fs::DirBuilderExt,
    path::PathBuf,
    ptr,
    sync::{atomic::Ordering, Arc},
};
use tauri::webview::WebviewWindow;
use webkit2gtk::*;

// Filter compilation is deliberately completed while the only document is blank.
const RULES: &[u8] = br#"[{"trigger":{"url-filter":"^(https?|wss?|file|tauri|asset|ipc):"},"action":{"type":"block"}}]"#;

fn block_existing(object: &impl IsA<glib::Object>, names: &[&str]) {
    for name in names {
        let name = CString::new(*name).unwrap();
        unsafe {
            let signal =
                glib::gobject_ffi::g_signal_lookup(name.as_ptr(), object.type_().into_glib());
            if signal != 0 {
                glib::gobject_ffi::g_signal_handlers_block_matched(
                    object.as_ref().as_ptr(),
                    glib::gobject_ffi::G_SIGNAL_MATCH_ID,
                    signal,
                    0,
                    ptr::null_mut(),
                    ptr::null_mut(),
                    ptr::null_mut(),
                );
            }
        }
    }
}

fn fail(policy: &Policy, ready: &PyObject, close: &WebviewWindow<Runtime>) {
    policy.alive.store(false, Ordering::SeqCst);
    notify(ready, false);
    let _ = close.close();
}

pub(crate) fn install(
    view: WebView,
    policy: Arc<Policy>,
    ready: PyObject,
    close: WebviewWindow<Runtime>,
) {
    if !view.is_ephemeral() || view.uri().as_deref() != Some("about:blank") {
        fail(&policy, &ready, &close);
        return;
    }
    let Some(settings) = view.settings() else {
        fail(&policy, &ready, &close);
        return;
    };
    settings.set_enable_webrtc(false);
    settings.set_enable_media_stream(false);
    settings.set_allow_file_access_from_file_urls(false);
    settings.set_allow_universal_access_from_file_urls(false);
    settings.set_enable_dns_prefetching(false);
    settings.set_enable_hyperlink_auditing(false);
    settings.set_enable_html5_database(false);
    settings.set_enable_html5_local_storage(false);
    settings.set_enable_developer_extras(false);
    settings.set_javascript_can_access_clipboard(false);
    settings.set_javascript_can_open_windows_automatically(false);
    if settings.enables_webrtc() {
        fail(&policy, &ready, &close);
        return;
    }
    let Some(manager) = view.user_content_manager() else {
        fail(&policy, &ready, &close);
        return;
    };
    manager.unregister_script_message_handler("ipc");
    block_existing(
        &view,
        &[
            "decide-policy",
            "create",
            "permission-request",
            "run-file-chooser",
            "script-dialog",
            "context-menu",
        ],
    );
    view.connect_create(|_, _| None);
    view.connect_permission_request(|_, request| {
        request.deny();
        true
    });
    view.connect_run_file_chooser(|_, request| {
        request.cancel();
        true
    });
    view.connect_script_dialog(|_, dialog| {
        dialog.close();
        true
    });
    let Some(context) = view.context() else {
        fail(&policy, &ready, &close);
        return;
    };
    block_existing(&context, &["download-started"]);
    context.connect_download_started(|_, download| download.cancel());

    let state = policy.clone();
    let admitted = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let initial_load = admitted.clone();
    view.connect_decide_policy(move |_, decision, kind| {
        let uri = decision
            .downcast_ref::<NavigationPolicyDecision>()
            .and_then(|navigation| navigation.navigation_action())
            .and_then(|action| action.request())
            .and_then(|request| request.uri());
        let allowed = state.alive.load(Ordering::SeqCst)
            && kind == PolicyDecisionType::NavigationAction
            && uri.as_deref().is_some_and(|uri| {
                matches!(uri, "about:blank" | "about:srcdoc")
                    || (uri == state.shell && !admitted.swap(true, Ordering::SeqCst))
            });
        // Response policy is only reached for an already admitted navigation.
        if kind == PolicyDecisionType::Response {
            return false;
        }
        if allowed {
            decision.use_();
        } else {
            decision.ignore();
        }
        true
    });
    let state = policy.clone();
    let closing = close.clone();
    view.connect_load_changed(move |view, event| {
        if !state.alive.load(Ordering::SeqCst) {
            return;
        }
        if !initial_load.load(Ordering::SeqCst) && view.uri().as_deref() == Some("about:blank") {
            return;
        }
        match event {
            LoadEvent::Started => {
                state.loaded.store(false, Ordering::SeqCst);
            }
            LoadEvent::Committed if view.uri().as_deref() != Some(state.shell.as_str()) => {
                state.alive.store(false, Ordering::SeqCst);
                let _ = closing.close();
            }
            LoadEvent::Finished if view.uri().as_deref() == Some(state.shell.as_str()) => {
                state.loaded.store(true, Ordering::SeqCst);
                state.send("ecl-plugin-ready://loaded");
            }
            _ => {}
        }
    });
    let state = policy.clone();
    view.connect_title_notify(move |view| {
        if !state.alive.load(Ordering::SeqCst)
            || !state.loaded.load(Ordering::SeqCst)
            || view.uri().as_deref() != Some(state.shell.as_str())
            || !view
                .title()
                .as_deref()
                .is_some_and(|value| value.starts_with("ecl-plugin-message://request/"))
        {
            return;
        }
        let weak = view.downgrade();
        let state = state.clone();
        view.evaluate_javascript(
            "globalThis.__eclTakeRequest()",
            None,
            None,
            None::<&gio::Cancellable>,
            move |result| {
                let Some(view) = weak.upgrade() else {
                    return;
                };
                if !state.loaded.load(Ordering::SeqCst)
                    || view.uri().as_deref() != Some(state.shell.as_str())
                {
                    return;
                }
                if let Ok(value) = result {
                    if value.is_string() {
                        state.send(value.to_str().as_str());
                    }
                }
            },
        );
    });

    let path = std::env::temp_dir().join(format!(
        "ecl-webkit-filter-{}-{}",
        std::process::id(),
        close.label()
    ));
    if fs::DirBuilder::new().mode(0o700).create(&path).is_err() {
        fail(&policy, &ready, &close);
        return;
    }
    let path_c = match CString::new(path.as_os_str().as_encoded_bytes()) {
        Ok(path) => path,
        Err(_) => {
            fail(&policy, &ready, &close);
            return;
        }
    };
    unsafe {
        let store = webkit2gtk::ffi::webkit_user_content_filter_store_new(path_c.as_ptr());
        if store.is_null() {
            let _ = fs::remove_dir(&path);
            fail(&policy, &ready, &close);
            return;
        }
        let bytes = glib::Bytes::from_static(RULES);
        let state = Box::new(Compilation {
            view: view.downgrade(),
            policy,
            ready,
            close,
            path,
            store,
        });
        webkit2gtk::ffi::webkit_user_content_filter_store_save(
            store,
            c"ecl-network-deny".as_ptr(),
            bytes.to_glib_none().0,
            ptr::null_mut(),
            Some(compiled),
            Box::into_raw(state).cast(),
        );
    }
}

struct Compilation {
    view: glib::WeakRef<WebView>,
    policy: Arc<Policy>,
    ready: PyObject,
    close: WebviewWindow<Runtime>,
    path: PathBuf,
    store: *mut webkit2gtk::ffi::WebKitUserContentFilterStore,
}

unsafe extern "C" fn compiled(
    _: *mut glib::gobject_ffi::GObject,
    result: *mut gio::ffi::GAsyncResult,
    state: *mut c_void,
) {
    let state = Box::from_raw(state.cast::<Compilation>());
    let mut error = ptr::null_mut();
    let filter = webkit2gtk::ffi::webkit_user_content_filter_store_save_finish(
        state.store,
        result,
        &mut error,
    );
    let view = state.view.upgrade();
    let success = !filter.is_null()
        && error.is_null()
        && state.policy.alive.load(Ordering::SeqCst)
        && view.is_some();
    if success {
        if let Some(manager) = view.as_ref().and_then(|view| view.user_content_manager()) {
            webkit2gtk::ffi::webkit_user_content_manager_add_filter(
                manager.to_glib_none().0,
                filter,
            );
            notify(&state.ready, true);
        } else {
            fail(&state.policy, &state.ready, &state.close);
        }
    } else {
        fail(&state.policy, &state.ready, &state.close);
    }
    if !filter.is_null() {
        webkit2gtk::ffi::webkit_user_content_filter_unref(filter);
    }
    if !error.is_null() {
        glib::ffi::g_error_free(error);
    }
    glib::gobject_ffi::g_object_unref(state.store.cast());
    // This directory was created exclusively above and holds only compiler cache.
    let _ = fs::remove_dir_all(&state.path);
}
