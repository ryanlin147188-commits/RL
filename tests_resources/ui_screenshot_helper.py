"""UI screenshot helper used by Robot Framework test cases.

Provides two Robot keywords (via Robot's "library as Python module" pattern):

* ``Capture Step Screenshot`` — take a Browser screenshot, optionally
  outline a target selector with a red border, then upload bytes via
  the platform's storage_service. Returns the public URL string.
* ``Highlight Target`` — draw a red rectangle around the located
  element for ``duration`` ms; useful when running headed for demos.

The helper is intentionally thin: actual storage backend selection is
delegated to ``app.services.storage_service``. In a Celery worker
container this just imports ``save_bytes``; in a standalone Robot run
(no FastAPI), it falls back to writing under ``./results/screenshots/``.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional

from robot.api.deco import keyword
from robot.libraries.BuiltIn import BuiltIn

try:  # pragma: no cover - optional in standalone Robot runs
    from app.services.storage_service import save_bytes  # type: ignore[import-not-found]

    _STORAGE_AVAILABLE = True
except Exception:  # pragma: no cover
    _STORAGE_AVAILABLE = False
    _LOCAL_DIR = Path(os.environ.get("ROBOT_SCREENSHOT_DIR", "results/screenshots")).resolve()
    _LOCAL_DIR.mkdir(parents=True, exist_ok=True)


HIGHLIGHT_JS = """
([selector, durationMs]) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  const overlay = document.createElement('div');
  overlay.style.cssText = `
    position: fixed; left: ${rect.left}px; top: ${rect.top}px;
    width: ${rect.width}px; height: ${rect.height}px;
    border: 3px solid red; box-sizing: border-box;
    z-index: 2147483647; pointer-events: none;
  `;
  document.body.appendChild(overlay);
  setTimeout(() => overlay.remove(), durationMs);
  return { x: rect.left, y: rect.top, w: rect.width, h: rect.height };
}
""".strip()


def _persist(data: bytes, prefix: str) -> str:
    key = f"{prefix}/{uuid.uuid4().hex}.png"
    if _STORAGE_AVAILABLE:
        return save_bytes(data, key, bucket="results", content_type="image/png")
    target = _LOCAL_DIR / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"/results/{key}"


@keyword("Highlight Target")
def highlight_target(selector: str, duration: int = 400) -> Optional[dict]:
    """Draw a red outline around ``selector`` for ``duration`` ms."""
    if not selector:
        return None
    browser = BuiltIn().get_library_instance("Browser")
    return browser.evaluate_javascript(None, HIGHLIGHT_JS, arg=[selector, duration])


@keyword("Capture Step Screenshot")
def capture_step_screenshot(step_index: int, phase: str, selector: str = "") -> str:
    """Capture a full-page screenshot, returning the stored relative URL."""
    browser = BuiltIn().get_library_instance("Browser")
    if selector:
        highlight_target(selector, duration=400)
    raw_path = browser.take_screenshot(filename="EMBED")  # base64 string
    # Browser library returns a path or a base64 data URL when filename=EMBED
    if isinstance(raw_path, str) and raw_path.startswith("data:image"):
        import base64

        data = base64.b64decode(raw_path.split(",", 1)[1])
    else:
        data = Path(str(raw_path)).read_bytes()
    return _persist(data, prefix=f"step_{int(step_index):03d}_{phase}")
