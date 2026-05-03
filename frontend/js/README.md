# Frontend modules (RFC-1)

This directory is the new home for the AutoTest SPA — split from the
single ~19k-line `index.html`. Phase 1 (the current state) ships only the
**core utilities** that every view needs; views themselves still live
inline in `index.html` for now.

## Why

The existing `index.html` has:

* **400+ global functions** — IDE search/diff overhead grows linearly
* **16+ duplicated CRUD modal blocks** — every change ripples 10x
* **Globals (`window.currentProjectId`, `_caches`)** — race-condition risk
* **No type hints, no error boundary, no code splitting**

Modularising in stages keeps each PR reviewable.

## Layout

```
js/
├── core/
│   ├── api.js          # apiFetch + 401 refresh + ApiError
│   ├── auth.js         # token storage + isLoggedIn / isExpiringSoon
│   └── store.js        # pub/sub state for view -> view comms
└── README.md           # you are here
```

Phase 2 will add `components/` (Modal, Form, Table, Toast). Phase 3 moves
each view (`projects.js`, `testcases.js`, ...) out of `index.html`.

## Backwards compatibility

Every module exports its functions AND attaches them to `window.AutoTest.*`.
That lets the inline scripts in `index.html` opt in incrementally:

```js
// inline script -- before
async function loadCases(pid) { /* ad-hoc fetch */ }

// inline script -- after the migration
async function loadCases(pid) {
  return AutoTest.api.apiFetch(`/api/projects/${pid}/testcases`);
}
```

…with no `<script type="module">` rewiring required.

## How the modules are mounted

`index.html` now loads the Phase 1 core modules near the top of `<body>`:

```html
<script type="module">
  // Side effect: defines window.AutoTest.{auth, api, store}
  import "/js/core/auth.js";
  import "/js/core/api.js";
  import "/js/core/store.js";
</script>
```

After this loads, anywhere in the page can call `AutoTest.api.apiFetch(...)`
or `AutoTest.store.set("currentProjectId", id)`.

## Phase plan (from the RFC)

| Phase | Scope | Days |
|---|---|---|
| **1** | core/ utilities + window.AutoTest shim **← here** | 2 |
| 2 | components/ (Modal/Form/Table/Toast) + login view as white-rabbit | 3 |
| 3 | move 18 views out of index.html, 2-3 per day | 5-7 |
| 4 | swap globals for store.subscribe | 2-3 |
| 5 | optional: add ESLint + Prettier (no build step) | 1-2 |

Each phase ends with index.html still working — never a big-bang rewrite.
