/*
 * LEGACY COMPAT FILE (PR2 shim)
 *
 * This file is intentionally kept as a static-asset compatibility shim.
 * The main UI does not load it anymore.
 *
 * Source-of-truth modules are loaded from:
 * - central/src/templates/index.html
 * - central/src/templates/topology-builder.html
 */

(function () {
  // Intentionally empty shim.
  // Keep a marker for diagnostics when fetched directly.
  if (typeof window !== "undefined") {
    window.__VLM_APP_JS_SHIM__ = true;
  }
})();
