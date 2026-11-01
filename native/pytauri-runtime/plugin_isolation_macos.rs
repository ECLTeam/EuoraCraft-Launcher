// EuoraCraft Launcher — GPL-3.0, ECLTeam © 2026.
// WKWebView native delegates and a main-frame-only signal, on the AppKit thread.

use crate::{
    plugin_isolation_unix::{notify, Policy},
    tauri_runtime::Runtime,
};
use block::{Block, ConcreteBlock};

#[repr(transparent)]
struct Completion<A, R>(*mut Block<A, R>);

unsafe impl<A, R> objc::Encode for Completion<A, R> {
    fn encode() -> objc::Encoding {
        unsafe { objc::Encoding::from_str("@?") }
    }
}

use objc::{
    class,
    declare::ClassDecl,
    msg_send,
    runtime::{Class, Object, Protocol, Sel, BOOL, NO, YES},
    sel, sel_impl,
};
use pyo3::prelude::*;
use std::{
    cell::Cell,
    ffi::{c_void, CStr},
    ptr,
    sync::{atomic::Ordering, Arc, OnceLock},
};
use tauri::webview::WebviewWindow;

const RULES: &str = r#"[{"trigger":{"url-filter":"^(https?|wss?|file|tauri|asset|ipc):"},"action":{"type":"block"}}]"#;
const SIGNAL: &str = "new MutationObserver(()=>{if(document.title.startsWith('ecl-plugin-message://request/'))window.webkit.messageHandlers.ecl_plugin_signal.postMessage('title');}).observe(document,{subtree:true,childList:true,characterData:true});";

struct Delegate {
    policy: Arc<Policy>,
    view: *mut Object,
    window: WebviewWindow<Runtime>,
    admitted: Cell<bool>,
    pulling: Cell<bool>,
}

unsafe fn string(value: &str) -> *mut Object {
    let object: *mut Object = msg_send![class!(NSString), alloc];
    let object: *mut Object =
        msg_send![object, initWithBytes:value.as_ptr() length:value.len() encoding:4usize];
    msg_send![object, autorelease]
}

unsafe fn text(value: *mut Object) -> String {
    if value.is_null() {
        return String::new();
    }
    let bytes: *const std::ffi::c_char = msg_send![value, UTF8String];
    if bytes.is_null() {
        return String::new();
    }
    CStr::from_ptr(bytes).to_string_lossy().into_owned()
}

unsafe fn url(view: *mut Object) -> String {
    let url: *mut Object = msg_send![view, URL];
    let absolute: *mut Object = msg_send![url, absoluteString];
    text(absolute)
}

unsafe fn delegate_state(this: &Object) -> &Delegate {
    &*(*this.get_ivar::<*mut c_void>("eclState")).cast::<Delegate>()
}

unsafe fn revoke(state: &Delegate) {
    state.policy.alive.store(false, Ordering::SeqCst);
    state.policy.loaded.store(false, Ordering::SeqCst);
    let _ = state.window.close();
}

extern "C" fn dealloc(this: &mut Object, _: Sel) {
    unsafe {
        let pointer = *this.get_ivar::<*mut c_void>("eclState");
        if !pointer.is_null() {
            drop(Box::from_raw(pointer.cast::<Delegate>()));
        }
        let _: () = msg_send![super(this, class!(NSObject)), dealloc];
    }
}

extern "C" fn navigation(
    this: &Object,
    _: Sel,
    _: *mut Object,
    action: *mut Object,
    handler: Completion<(isize,), ()>,
) {
    unsafe {
        let state = delegate_state(this);
        let frame: *mut Object = msg_send![action, targetFrame];
        let main: BOOL = if frame.is_null() {
            NO
        } else {
            msg_send![frame, isMainFrame]
        };
        let request: *mut Object = msg_send![action, request];
        let requested = url(request);
        let allowed = state.policy.alive.load(Ordering::SeqCst)
            && !frame.is_null()
            && if main == YES {
                (requested == "about:blank" && !state.admitted.get())
                    || (requested == state.policy.shell && !state.admitted.replace(true))
            } else {
                matches!(requested.as_str(), "about:blank" | "about:srcdoc")
            };
        (*handler.0).call((if allowed { 1 } else { 0 },));
    }
}

extern "C" fn response(
    this: &Object,
    _: Sel,
    _: *mut Object,
    response: *mut Object,
    handler: Completion<(isize,), ()>,
) {
    unsafe {
        let supported: BOOL = msg_send![response, canShowMIMEType];
        (*handler.0).call((
            if delegate_state(this).policy.alive.load(Ordering::SeqCst) && supported == YES {
                1
            } else {
                0
            },
        ));
    }
}

extern "C" fn committed(this: &Object, _: Sel, view: *mut Object, _: *mut Object) {
    unsafe {
        let state = delegate_state(this);
        if !state.admitted.get() && url(view) == "about:blank" {
            return;
        }
        state.policy.loaded.store(false, Ordering::SeqCst);
        if url(view) != state.policy.shell {
            revoke(state);
        }
    }
}

extern "C" fn finished(this: &Object, _: Sel, view: *mut Object, _: *mut Object) {
    unsafe {
        let state = delegate_state(this);
        if !state.admitted.get() && url(view) == "about:blank" {
            return;
        }
        if state.policy.alive.load(Ordering::SeqCst) && url(view) == state.policy.shell {
            state.policy.loaded.store(true, Ordering::SeqCst);
            state.policy.send("ecl-plugin-ready://loaded");
        } else {
            revoke(state);
        }
    }
}

extern "C" fn terminated(this: &Object, _: Sel, _: *mut Object) {
    unsafe {
        revoke(delegate_state(this));
    }
}

extern "C" fn failed(this: &Object, _: Sel, _: *mut Object, _: *mut Object, _: *mut Object) {
    unsafe {
        revoke(delegate_state(this));
    }
}

extern "C" fn popup(
    _: &Object,
    _: Sel,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
) -> *mut Object {
    ptr::null_mut()
}

extern "C" fn open_panel(
    _: &Object,
    _: Sel,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    handler: Completion<(*mut Object,), ()>,
) {
    unsafe {
        (*handler.0).call((ptr::null_mut(),));
    }
}

extern "C" fn media(
    _: &Object,
    _: Sel,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    _: usize,
    handler: Completion<(isize,), ()>,
) {
    unsafe {
        (*handler.0).call((0,));
    }
}

extern "C" fn alert(
    _: &Object,
    _: Sel,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    handler: Completion<(), ()>,
) {
    unsafe {
        (*handler.0).call(());
    }
}

extern "C" fn confirm(
    _: &Object,
    _: Sel,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    handler: Completion<(BOOL,), ()>,
) {
    unsafe {
        (*handler.0).call((NO,));
    }
}

extern "C" fn prompt(
    _: &Object,
    _: Sel,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    _: *mut Object,
    handler: Completion<(*mut Object,), ()>,
) {
    unsafe {
        (*handler.0).call((ptr::null_mut(),));
    }
}

extern "C" fn signal(this: &Object, _: Sel, _: *mut Object, message: *mut Object) {
    unsafe {
        let state = delegate_state(this);
        let frame: *mut Object = msg_send![message, frameInfo];
        let main: BOOL = msg_send![frame, isMainFrame];
        let sender: *mut Object = msg_send![message, webView];
        if main != YES
            || sender != state.view
            || !state.policy.alive.load(Ordering::SeqCst)
            || !state.policy.loaded.load(Ordering::SeqCst)
            || url(sender) != state.policy.shell
            || state.pulling.replace(true)
        {
            return;
        }
        // Keep the delegate alive through the asynchronous evaluation without a view cycle.
        let owner: *mut Object = msg_send![this, retain];
        let owner = owner as usize;
        let callback = ConcreteBlock::new(move |value: *mut Object, error: *mut Object| {
            let owner = owner as *mut Object;
            let state = delegate_state(&*owner);
            state.pulling.set(false);
            if state.policy.alive.load(Ordering::SeqCst)
                && error.is_null()
                && !value.is_null()
                && state.policy.loaded.load(Ordering::SeqCst)
                && url(state.view) == state.policy.shell
            {
                let is_string: BOOL = msg_send![value, isKindOfClass:class!(NSString)];
                if is_string == YES {
                    state.policy.send(&text(value));
                }
            }
            let _: () = msg_send![owner, release];
        })
        .copy();
        let _: () = msg_send![sender, evaluateJavaScript:string("globalThis.__eclTakeRequest()") completionHandler:&*callback];
    }
}

fn delegate_class() -> &'static Class {
    static CLASS: OnceLock<usize> = OnceLock::new();
    let class = CLASS.get_or_init(|| unsafe {
        let mut class = ClassDecl::new("ECLPluginWebKitDelegateV2", class!(NSObject)).unwrap();
        class.add_ivar::<*mut c_void>("eclState");
        for name in ["WKNavigationDelegate", "WKUIDelegate", "WKScriptMessageHandler"] {
            class.add_protocol(Protocol::get(name).unwrap());
        }
        class.add_method(sel!(dealloc), dealloc as extern "C" fn(_, _));
        class.add_method(sel!(webView:decidePolicyForNavigationAction:decisionHandler:), navigation as extern "C" fn(_, _, _, _, _));
        class.add_method(sel!(webView:decidePolicyForNavigationResponse:decisionHandler:), response as extern "C" fn(_, _, _, _, _));
        class.add_method(sel!(webView:didCommitNavigation:), committed as extern "C" fn(_, _, _, _));
        class.add_method(sel!(webView:didFinishNavigation:), finished as extern "C" fn(_, _, _, _));
        class.add_method(sel!(webView:didFailNavigation:withError:), failed as extern "C" fn(_, _, _, _, _));
        class.add_method(sel!(webView:didFailProvisionalNavigation:withError:), failed as extern "C" fn(_, _, _, _, _));
        class.add_method(sel!(webViewWebContentProcessDidTerminate:), terminated as extern "C" fn(_, _, _));
        class.add_method(sel!(webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:), popup as extern "C" fn(_, _, _, _, _, _) -> _);
        class.add_method(sel!(webView:runOpenPanelWithParameters:initiatedByFrame:completionHandler:), open_panel as extern "C" fn(_, _, _, _, _, _));
        class.add_method(sel!(webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:), media as extern "C" fn(_, _, _, _, _, _, _));
        class.add_method(sel!(webView:runJavaScriptAlertPanelWithMessage:initiatedByFrame:completionHandler:), alert as extern "C" fn(_, _, _, _, _, _));
        class.add_method(sel!(webView:runJavaScriptConfirmPanelWithMessage:initiatedByFrame:completionHandler:), confirm as extern "C" fn(_, _, _, _, _, _));
        class.add_method(sel!(webView:runJavaScriptTextInputPanelWithPrompt:defaultText:initiatedByFrame:completionHandler:), prompt as extern "C" fn(_, _, _, _, _, _, _));
        class.add_method(sel!(userContentController:didReceiveScriptMessage:), signal as extern "C" fn(_, _, _, _));
        class.register() as *const Class as usize
    });
    unsafe { &*(*class as *const Class) }
}

unsafe fn preferences(view: *mut Object) -> bool {
    if tauri::webview_version().ok().as_deref() != Some(env!("ECL_EXPECTED_WKWEBVIEW_VERSION")) {
        return false;
    }
    let configuration: *mut Object = msg_send![view, configuration];
    let store: *mut Object = msg_send![configuration, websiteDataStore];
    let persistent: BOOL = msg_send![store, isPersistent];
    if persistent != NO {
        return false;
    }
    let preferences: *mut Object = msg_send![configuration, preferences];
    // These SPI selectors have no public equivalent. The exact WebKit build is
    // reviewed and pinned; missing controls close the blank window before guests.
    for selector in [
        sel!(_setPeerConnectionEnabled:),
        sel!(_peerConnectionEnabled),
        sel!(_setMediaDevicesEnabled:),
        sel!(_setScreenCaptureEnabled:),
    ] {
        let present: BOOL = msg_send![preferences, respondsToSelector:selector];
        if present != YES {
            return false;
        }
    }
    let _: () = msg_send![preferences, _setPeerConnectionEnabled:NO];
    let _: () = msg_send![preferences, _setMediaDevicesEnabled:NO];
    let _: () = msg_send![preferences, _setScreenCaptureEnabled:NO];
    let peer: BOOL = msg_send![preferences, _peerConnectionEnabled];
    if peer != NO {
        return false;
    }
    let _: () = msg_send![preferences, setJavaScriptCanOpenWindowsAutomatically:NO];
    let features: *mut Object = msg_send![class!(WKPreferences), _features];
    let count: usize = msg_send![features, count];
    for index in 0..count {
        let feature: *mut Object = msg_send![features, objectAtIndex:index];
        let key: *mut Object = msg_send![feature, key];
        if text(key).contains("WebTransport") {
            let _: () = msg_send![preferences, _setEnabled:NO forFeature:feature];
            let enabled: BOOL = msg_send![preferences, _isEnabledForFeature:feature];
            if enabled != NO {
                return false;
            }
        }
    }
    true
}

pub(crate) unsafe fn install(
    raw: *mut c_void,
    policy: Arc<Policy>,
    ready: PyObject,
    window: WebviewWindow<Runtime>,
) {
    let view = raw.cast::<Object>();
    if view.is_null() || url(view) != "about:blank" || !preferences(view) {
        policy.alive.store(false, Ordering::SeqCst);
        notify(&ready, false);
        let _ = window.close();
        return;
    }
    let configuration: *mut Object = msg_send![view, configuration];
    let manager: *mut Object = msg_send![configuration, userContentController];
    let _: () = msg_send![manager, removeScriptMessageHandlerForName:string("ipc")];
    let delegate: *mut Object = msg_send![delegate_class(), new];
    (*delegate).set_ivar(
        "eclState",
        Box::into_raw(Box::new(Delegate {
            policy: policy.clone(),
            view,
            window: window.clone(),
            admitted: Cell::new(false),
            pulling: Cell::new(false),
        }))
        .cast::<c_void>(),
    );
    let _: () = msg_send![view, setNavigationDelegate:delegate];
    let _: () = msg_send![view, setUIDelegate:delegate];
    // The native controller owns this handler for precisely this webview lifetime.
    let _: () =
        msg_send![manager, addScriptMessageHandler:delegate name:string("ecl_plugin_signal")];
    let script: *mut Object = msg_send![class!(WKUserScript), alloc];
    let script: *mut Object =
        msg_send![script, initWithSource:string(SIGNAL) injectionTime:0usize forMainFrameOnly:YES];
    let _: () = msg_send![manager, addUserScript:script];
    let _: () = msg_send![script, release];
    let _: () = msg_send![delegate, release];

    let store: *mut Object = msg_send![class!(WKContentRuleListStore), defaultStore];
    let identifier = window.label().to_owned();
    let cleanup_identifier = identifier.clone();
    let callback = ConcreteBlock::new(move |filter: *mut Object, error: *mut Object| {
        if !error.is_null() || filter.is_null() || !policy.alive.load(Ordering::SeqCst) {
            policy.alive.store(false, Ordering::SeqCst); notify(&ready, false); let _ = window.close();
        } else {
            // Retrieve the owned controller only while the live policy permits it.
            let configuration: *mut Object = msg_send![view, configuration];
            let manager: *mut Object = msg_send![configuration, userContentController];
            let _: () = msg_send![manager, addContentRuleList:filter];
            notify(&ready, true);
        }
        let cleanup = ConcreteBlock::new(|_: *mut Object| {}).copy();
        let store: *mut Object = msg_send![class!(WKContentRuleListStore), defaultStore];
        let _: () = msg_send![store, removeContentRuleListForIdentifier:string(&cleanup_identifier) completionHandler:&*cleanup];
    }).copy();
    let _: () = msg_send![store, compileContentRuleListForIdentifier:string(&identifier) encodedContentRuleList:string(RULES) completionHandler:&*callback];
}
