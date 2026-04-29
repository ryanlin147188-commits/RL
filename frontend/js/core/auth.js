// Auth token storage + login/refresh state management (RFC-1 Phase 1).
//
// Mirrors the `authSetTokens` / `authGetAccessToken` helpers currently inline
// in index.html so future view modules can import them without depending on
// global function names. Backwards compatible: also exposes `window.AutoTest.auth`
// so legacy inline code can call the same functions during the migration.
//
// Storage keys (kept identical to the existing inline implementation):
//   autotest.auth.access_token      -> JWT access token
//   autotest.auth.refresh_token     -> JWT refresh token
//   autotest.auth.access_expires_at -> ms since epoch when access expires
//
// No build step required: drop into <script type="module"> and import.

/** @typedef {{ access_token: string, refresh_token: string, expires_in: number }} TokenPair */

const KEY_ACCESS = "autotest.auth.access_token";
const KEY_REFRESH = "autotest.auth.refresh_token";
const KEY_EXP = "autotest.auth.access_expires_at";

/** Persist the freshly minted token pair from /api/auth/login or /refresh. */
export function setTokens(/** @type {TokenPair} */ pair) {
  localStorage.setItem(KEY_ACCESS, pair.access_token);
  localStorage.setItem(KEY_REFRESH, pair.refresh_token);
  // Server expires_in is seconds; convert to absolute ms-epoch so refresh
  // logic doesn't have to worry about clock drift on the page.
  localStorage.setItem(
    KEY_EXP,
    String(Date.now() + (pair.expires_in || 0) * 1000)
  );
}

export function getAccessToken() {
  return localStorage.getItem(KEY_ACCESS) || "";
}

export function getRefreshToken() {
  return localStorage.getItem(KEY_REFRESH) || "";
}

export function getAccessExpiresAt() {
  const v = localStorage.getItem(KEY_EXP);
  return v ? Number(v) : 0;
}

/** Wipes the stored pair. Caller decides whether to redirect to login. */
export function clearTokens() {
  localStorage.removeItem(KEY_ACCESS);
  localStorage.removeItem(KEY_REFRESH);
  localStorage.removeItem(KEY_EXP);
}

export function isLoggedIn() {
  return !!getAccessToken();
}

/** True if the access token expires within `marginMs` (default: 60s). */
export function isExpiringSoon(marginMs = 60_000) {
  const exp = getAccessExpiresAt();
  if (!exp) return true;
  return exp - Date.now() < marginMs;
}

// ── Backwards-compat global ──────────────────────────────────────────────
// Inline scripts in index.html can call `window.AutoTest.auth.getAccessToken()`
// during the migration. Once index.html is fully modularised this can drop.
const AutoTest = (window.AutoTest = window.AutoTest || {});
AutoTest.auth = {
  setTokens,
  getAccessToken,
  getRefreshToken,
  getAccessExpiresAt,
  clearTokens,
  isLoggedIn,
  isExpiringSoon,
};
