"use strict";
// What htmx doesn't do by itself: the dialog, toasts, list filters and multi-select.
(() => {
  const dialog = () => document.getElementById("dialog");

  // ---- dialog: a request into #dialog-body opens it at once with a spinner (lookups can
  // take a second or two); a failed request closes it again if nothing arrived ----
  const intoDialog = (e) => e.detail.target?.id === "dialog-body";
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (!intoDialog(e) || dialog().open) return;
    e.detail.target.innerHTML = '<div class="empty" data-dialog-loading><span class="spinner"></span>Loading…</div>';
    dialog().showModal();
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    if (intoDialog(e) && !e.detail.successful && e.detail.target.querySelector("[data-dialog-loading]")) dialog().close();
  });
  document.addEventListener("htmx:afterSwap", (e) => {
    if (intoDialog(e) && !dialog().open) dialog().showModal();
  });
  document.addEventListener("click", (e) => {
    // [data-close-dialog], or a click on the backdrop (outside the dialog's content)
    if (e.target.closest("[data-close-dialog]") || e.target === dialog()) dialog().close();
  });
  // The server asks for this after an action that finishes inside the dialog.
  document.body.addEventListener("close-dialog", () => dialog().close());

  // ---- toasts: the server sends HX-Trigger {"toast": {"message", "level"}} ----
  function toast(message, level = "ok") {
    const box = document.getElementById("toasts");
    const el = document.createElement("div");
    el.className = level === "error" ? "toast toast-error" : "toast";
    el.setAttribute("role", level === "error" ? "alert" : "status");
    el.textContent = message;
    box.append(el);
    // Re-opening puts the toasts above an open modal dialog (both live in the top layer).
    if (box.matches(":popover-open")) box.hidePopover();
    box.showPopover();
    setTimeout(() => {
      el.remove();
      if (!box.childElementCount) box.hidePopover();
    }, level === "error" ? 8000 : 4500);
  }
  document.addEventListener("toast", (e) => toast(e.detail.message, e.detail.level));
  document.addEventListener("htmx:sendError", () => toast("Can't reach the server.", "error"));
  document.addEventListener("htmx:responseError", (e) => {
    if (!e.detail.xhr.getResponseHeader("HX-Trigger")) toast(`Request failed (HTTP ${e.detail.xhr.status}).`, "error");
  });

  // ---- filters: inside [data-filter-scope], [data-filter-text] and [data-filter-kind]
  // radios hide the [data-item]s of each [data-filter-group] that don't match ----
  function applyFilter(scope) {
    const text = (scope.querySelector("[data-filter-text]")?.value || "").trim().toLowerCase();
    const kind = scope.querySelector("[data-filter-kind]:checked")?.value || "all";
    for (const group of scope.querySelectorAll("[data-filter-group]")) {
      let shown = 0;
      for (const item of group.querySelectorAll("[data-item]")) {
        const match = (!text || item.dataset.text.includes(text)) && (kind === "all" || item.dataset.kind === kind);
        item.hidden = !match;
        shown += match;
      }
      const empty = group.querySelector("[data-filter-empty]");
      if (empty) empty.hidden = shown > 0;
      if (group.hasAttribute("data-filter-section")) {
        group.hidden = shown === 0 && Boolean(text || kind !== "all");
      }
    }
  }
  const scopeOf = (el) => el.closest?.("[data-filter-scope]");
  for (const type of ["input", "change"]) {
    document.addEventListener(type, (e) => {
      if (e.target.matches("[data-filter-text], [data-filter-kind]")) applyFilter(scopeOf(e.target));
    });
  }

  // ---- multi-select: [data-select-all="#list"] ticks every visible box in #list;
  // [data-selection="#list"] buttons are enabled while something there is ticked ----
  const visibleBoxes = (list) => [...document.querySelectorAll(`${list} input[type=checkbox]`)]
    .filter((box) => !box.closest("[hidden]"));
  function syncSelection() {
    for (const button of document.querySelectorAll("[data-selection]")) {
      const boxes = document.querySelectorAll(`${button.dataset.selection} input[type=checkbox]:checked`);
      const n = new Set([...boxes].map((box) => box.value)).size;  // a title in two playlists counts once
      button.disabled = n === 0;
      const count = button.querySelector("[data-count]");
      if (count) count.textContent = n ? `(${n})` : "";
    }
  }
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-select-all]");
    if (!button) return;
    const boxes = visibleBoxes(button.dataset.selectAll);
    const tick = !boxes.every((box) => box.checked);
    for (const box of boxes) box.checked = tick;
    syncSelection();
  });
  document.addEventListener("change", (e) => {
    if (e.target.matches("input[type=checkbox]")) syncSelection();
    // data-check="id": changing this control (e.g. a season picker) ticks that radio
    if (e.target.dataset.check) document.getElementById(e.target.dataset.check).checked = true;
  });

  // ---- playlists fold like folders; the choice is remembered across reloads ----
  const FOLDED = "watchlist-folded";
  const folded = () => {
    try { return new Set(JSON.parse(localStorage.getItem(FOLDED) || "[]")); } catch { return new Set(); }
  };
  function applyFolds() {
    const shut = folded();
    for (const button of document.querySelectorAll("[data-collapse]")) {
      const key = button.dataset.collapse;
      const closed = shut.has(key);
      button.setAttribute("aria-expanded", String(!closed));
      const body = document.getElementById(`group-${key}`);
      if (body) body.hidden = closed;
    }
  }
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-collapse]");
    if (!button) return;
    const shut = folded();
    const key = button.dataset.collapse;
    if (shut.has(key)) shut.delete(key); else shut.add(key);
    try { localStorage.setItem(FOLDED, JSON.stringify([...shut])); } catch {}
    applyFolds();
  });

  // ---- on phones the nav scrolls sideways: keep the current page's link in view ----
  const revealNav = () => document.querySelector("nav [aria-current=page]")
    ?.scrollIntoView({block: "nearest", inline: "nearest"});
  document.addEventListener("DOMContentLoaded", () => { revealNav(); applyFolds(); });

  // ---- after every swap: re-apply filters, selections, folds, and keep logs scrolled to the end ----
  document.addEventListener("htmx:afterSettle", (e) => {
    if (e.detail.target === document.body) revealNav();  // a boosted page change
    const scope = scopeOf(e.detail.target);
    if (scope) applyFilter(scope);
    syncSelection();
    applyFolds();
    for (const log of document.querySelectorAll(".log[data-tail]")) log.scrollTop = log.scrollHeight;
  });
})();
