/* Shared promise API: Firefox keeps its native browser namespace; Chrome gets
 * a small callback bridge for the APIs used by the extension. */
(function (global) {
  const native = global.browser;
  const nativeUrl = native && native.runtime && native.runtime.getURL ? native.runtime.getURL("") : "";
  if (/^moz-extension:/.test(nativeUrl)) return;
  const chromeApi = global.chrome;
  if (!chromeApi) return;
  const message = (error) => error && error.message ? error.message : String(error);
  function promiseMethod(namespace, name) {
    return (...args) => new Promise((resolve, reject) => {
      let done = false;
      const settle = (fn) => (...values) => {
        if (done) return;
        done = true;
        const lastError = chromeApi.runtime && chromeApi.runtime.lastError;
        if (lastError) reject(new Error(lastError.message));
        else fn(values.length > 1 ? values : values[0]);
      };
      try {
        const result = namespace[name].call(namespace, ...args, settle(resolve));
        if (result && typeof result.then === "function") result.then(settle(resolve), settle(reject));
      } catch (error) { settle(reject)(error); }
    });
  }
  function bridge(listener) {
    return (msg, sender, sendResponse) => {
      let result;
      try { result = listener(msg, sender); } catch (error) {
        sendResponse({ ok: false, error: message(error) });
        return true;
      }
      if (result === undefined) return undefined;
      Promise.resolve(result).then(sendResponse, (error) => sendResponse({ ok: false, error: message(error) }));
      return true;
    };
  }
  const browser = Object.create(chromeApi);
  browser.runtime = Object.create(chromeApi.runtime);
  browser.runtime.sendMessage = promiseMethod(chromeApi.runtime, "sendMessage");
  browser.runtime.onMessage = { addListener: (fn) => chromeApi.runtime.onMessage.addListener(bridge(fn)) };
  // Only where Chrome offers it: the background, once the optional nativeMessaging permission is
  // granted. Looked up on chrome.runtime at each use, never copied at load: the popup grants the
  // permission while the service worker runs, and the method appears only then, possibly on a
  // chrome.runtime rebuilt for it that neither a copy nor the prototype above would see; the
  // Start button would read "permission missing" until the worker restarted. background.js
  // treats a missing method as the permission not granted.
  Object.defineProperty(browser.runtime, "sendNativeMessage", {
    configurable: true,
    enumerable: true,
    get() {
      const runtime = chromeApi.runtime;
      return runtime && typeof runtime.sendNativeMessage === "function" ? promiseMethod(runtime, "sendNativeMessage") : undefined;
    },
  });
  if (chromeApi.storage && chromeApi.storage.local) {
    browser.storage = Object.create(chromeApi.storage);
    browser.storage.local = Object.create(chromeApi.storage.local);
    for (const name of ["get", "set"]) browser.storage.local[name] = promiseMethod(chromeApi.storage.local, name);
    // The launch record of the "Start server" button and the update records live here;
    // background.js does without the area when the browser has none.
    if (chromeApi.storage.session) {
      browser.storage.session = Object.create(chromeApi.storage.session);
      for (const name of ["get", "set"]) browser.storage.session[name] = promiseMethod(chromeApi.storage.session, name);
    }
    browser.storage.onChanged = chromeApi.storage.onChanged;
  }
  if (chromeApi.tabs) {
    browser.tabs = Object.create(chromeApi.tabs);
    for (const name of ["query", "reload", "sendMessage", "create"]) browser.tabs[name] = promiseMethod(chromeApi.tabs, name);
  }
  // Which tab the viewer is watching. The events come through the prototype chain, as tabs' do;
  // only WINDOW_ID_NONE and onFocusChanged are read, and both live on the namespace itself.
  if (chromeApi.windows) {
    browser.windows = Object.create(chromeApi.windows);
    for (const name of ["get", "getLastFocused"]) {
      if (chromeApi.windows[name]) browser.windows[name] = promiseMethod(chromeApi.windows, name);
    }
  }
  // The update nudges: the toolbar badge and the system notification. Only where Chrome offers
  // them (the background and the popup); background.js does without either.
  if (chromeApi.action) {
    browser.action = Object.create(chromeApi.action);
    for (const name of ["setBadgeText", "setBadgeBackgroundColor"]) browser.action[name] = promiseMethod(chromeApi.action, name);
  }
  if (chromeApi.notifications) {
    browser.notifications = Object.create(chromeApi.notifications);
    for (const name of ["create", "clear"]) browser.notifications[name] = promiseMethod(chromeApi.notifications, name);
    browser.notifications.onClicked = chromeApi.notifications.onClicked;
  }
  if (chromeApi.permissions) {
    browser.permissions = Object.create(chromeApi.permissions);
    for (const name of ["contains", "request"]) browser.permissions[name] = promiseMethod(chromeApi.permissions, name);
  }
  if (chromeApi.downloads) {
    browser.downloads = Object.create(chromeApi.downloads);
    browser.downloads.download = promiseMethod(chromeApi.downloads, "download");
    browser.downloads.onChanged = chromeApi.downloads.onChanged;
  }
  if (chromeApi.commands) {
    browser.commands = Object.create(chromeApi.commands);
    browser.commands.onCommand = chromeApi.commands.onCommand;
  }
  global.browser = browser;
})(globalThis);
