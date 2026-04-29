// Tiny pub/sub state container. Replaces the `window.currentProjectId` /
// `_caches` / `_refreshing` global soup that drove the inline app.
//
// Usage:
//   import { store } from "/js/core/store.js";
//   store.set("currentProjectId", "abc-123");
//   const off = store.subscribe("currentProjectId", (val) => render(val));
//   off();   // unsubscribe
//
// Intentionally minimal: no actions, no selectors, no immutability checks.
// The point is to give views a single place to read shared state from --
// not to recreate Redux.

const state = {};
const subs = new Map();

export const store = {
  /** Get a key's current value, or `undefined` if never set. */
  get(key) {
    return state[key];
  },

  /** Set a key. Notifies subscribers if the value changed (===). */
  set(key, value) {
    if (state[key] === value) return;
    state[key] = value;
    const handlers = subs.get(key);
    if (handlers) for (const h of handlers) h(value);
  },

  /**
   * Subscribe to changes on `key`. Returns an unsubscribe function.
   * @template T
   * @param {string} key
   * @param {(v: T) => void} handler
   */
  subscribe(key, handler) {
    let handlers = subs.get(key);
    if (!handlers) {
      handlers = new Set();
      subs.set(key, handlers);
    }
    handlers.add(handler);
    return () => handlers.delete(handler);
  },

  /** Snapshot for debugging. */
  snapshot() {
    return { ...state };
  },
};

// ── Backwards-compat global ──────────────────────────────────────────────
const AutoTest = (window.AutoTest = window.AutoTest || {});
AutoTest.store = store;
