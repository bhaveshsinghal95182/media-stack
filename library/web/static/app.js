"use strict";
// What htmx doesn't do by itself: the dialog, toasts, list filters and multi-select.
(() => {
  const dialog = () => document.getElementById("dialog");

  // ---- dialog: anything swapped into #dialog-body opens it ----
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target.id === "dialog-body" && !dialog().open) dialog().showModal();
  });
  document.addEventListener("click", (e) => {
    // [data-close-dialog], or a click on the backdrop (outside the dialog's content)
    if (e.target.closest("[data-close-dialog]") || e.target === dialog()) dialog().close();
  });

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
      const n = document.querySelectorAll(`${button.dataset.selection} input[type=checkbox]:checked`).length;
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

  // ---- on phones the nav scrolls sideways: keep the current page's link in view ----
  const revealNav = () => document.querySelector("nav [aria-current=page]")
    ?.scrollIntoView({block: "nearest", inline: "nearest"});
  document.addEventListener("DOMContentLoaded", revealNav);

  // ---- after every swap: re-apply filters, selections, and keep logs scrolled to the end ----
  document.addEventListener("htmx:afterSettle", (e) => {
    if (e.detail.target === document.body) revealNav();  // a boosted page change
    const scope = scopeOf(e.detail.target);
    if (scope) applyFilter(scope);
    syncSelection();
    for (const log of document.querySelectorAll(".log[data-tail]")) log.scrollTop = log.scrollHeight;
  });
})();
