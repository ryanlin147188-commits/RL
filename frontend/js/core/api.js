// API client with automatic Bearer-token injection and silent 401 refresh.
//
// Replaces the inline `apiFetch` in index.html. Two failure modes:
//   1) 401 with a stored refresh token  -> call /api/auth/refresh, retry once
//   2) 401 without (or refresh fails)   -> clear tokens, dispatch
//      `autotest:auth-required` event so view code can show the login modal.
//
// Single in-flight refresh: if 30 concurrent calls all 401 simultaneously,
// only one /refresh fires; the rest await its result.
//
// Usage:
//   import { apiFetch } from "/js/core/api.js";
//   const cases = await apiFetch("/api/projects/123/testcases");
//   const created = await apiFetch("/api/defects", { method: "POST", body: payload });

import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  setTokens,
} from "/js/core/auth.js";

let _refreshPromise = null;

async function _refreshOnce() {
  // De-dupe concurrent refreshers.
  if (_refreshPromise) return _refreshPromise;
  _refreshPromise = (async () => {
    const refresh = getRefreshToken();
    if (!refresh) throw new Error("no refresh token");
    const resp = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
    if (!resp.ok) throw new Error(`refresh failed: ${resp.status}`);
    const pair = await resp.json();
    setTokens(pair);
    return pair.access_token;
  })().finally(() => {
    _refreshPromise = null;
  });
  return _refreshPromise;
}

/**
 * Fetch wrapper. JSON body in / JSON body out by default.
 *
 * @param {string} url
 * @param {{
 *   method?: string,
 *   body?: any,
 *   headers?: Record<string,string>,
 *   signal?: AbortSignal,
 *   raw?: boolean,             // true -> return Response, don't parse JSON
 *   skipAuth?: boolean,        // for /auth/login etc
 * }} [opts]
 * @returns {Promise<any>}
 */
export async function apiFetch(url, opts = {}) {
  const init = {
    method: opts.method || "GET",
    headers: { ...(opts.headers || {}) },
    signal: opts.signal,
  };

  if (opts.body !== undefined) {
    if (opts.body instanceof FormData || opts.body instanceof Blob) {
      init.body = opts.body;
    } else {
      init.headers["Content-Type"] = init.headers["Content-Type"] || "application/json";
      init.body =
        typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
    }
  }

  if (!opts.skipAuth) {
    const token = getAccessToken();
    if (token) init.headers["Authorization"] = `Bearer ${token}`;
  }

  let resp = await fetch(url, init);

  // Retry once on 401 if we have a refresh token.
  if (resp.status === 401 && !opts.skipAuth && getRefreshToken()) {
    try {
      const newAccess = await _refreshOnce();
      init.headers["Authorization"] = `Bearer ${newAccess}`;
      resp = await fetch(url, init);
    } catch {
      clearTokens();
      window.dispatchEvent(new CustomEvent("autotest:auth-required"));
      throw new ApiError(401, "Session expired");
    }
  }

  if (resp.status === 401 && !opts.skipAuth) {
    clearTokens();
    window.dispatchEvent(new CustomEvent("autotest:auth-required"));
  }

  if (opts.raw) return resp;

  if (!resp.ok) {
    let detail = "";
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      detail = await resp.text();
    }
    throw new ApiError(resp.status, detail);
  }

  if (resp.status === 204) return null;
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp.text();
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(`[${status}] ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// ── Backwards-compat global ──────────────────────────────────────────────
const AutoTest = (window.AutoTest = window.AutoTest || {});
AutoTest.api = { apiFetch, ApiError };
