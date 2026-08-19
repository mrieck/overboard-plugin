"use strict";

// ---- prompt field with completion (CommandPromptField port) ----------------
// A monospace textarea that completes the leading /slash-command (user,
// project and plugin commands + skills) and ~/ ./ / paths anywhere, and
// warns when the command's plugin isn't enabled in the run folder — with an
// "Enable in <folder>" button that fixes it. Suggestions come from the
// Python side (complete_prompt / command_enablement); this file only does
// keys, caret math and rendering. Used by the slot editor and the dispatcher.
//
//   const pf = promptField({ value, cwd: () => folder, placeholder, onChange,
//                            onSubmit });   // onSubmit: ⌘/Ctrl+Enter
//   host.appendChild(pf.root);  pf.getValue();  pf.setValue(v);  pf.focus();

function promptField(opts) {
  opts = opts || {};
  const root = el("div", "pf");
  const ta = document.createElement("textarea");
  ta.className = "pf-text";
  ta.rows = opts.rows || 5;
  ta.placeholder = opts.placeholder || "";
  ta.value = opts.value || "";
  ta.spellcheck = false;
  root.appendChild(ta);
  const menu = el("div", "pf-menu"); menu.hidden = true;
  root.appendChild(menu);
  const notice = el("div", "pf-notice"); notice.hidden = true;
  root.appendChild(notice);

  let items = [];          // current suggestions
  let span = null;         // {start, end} the accepted insertion replaces
  let sel = 0;
  let dismissedFor = null; // text for which Esc hid the menu
  let reqSeq = 0;          // drop stale completion responses
  let timer = null, noticeTimer = null;
  const cwd = () => (typeof opts.cwd === "function" ? opts.cwd() : opts.cwd) || null;

  function hide() { items = []; span = null; menu.hidden = true; menu.textContent = ""; }

  function renderMenu() {
    menu.textContent = "";
    if (!items.length) { menu.hidden = true; return; }
    items.forEach((it, i) => {
      const row = el("div", "pf-item" + (i === sel ? " on" : ""));
      row.appendChild(el("span", "pf-label", it.label));
      if (it.detail) row.appendChild(el("span", "pf-detail", it.detail));
      row.addEventListener("mousedown", (e) => { e.preventDefault(); accept(i); });
      row.addEventListener("mousemove", () => { if (sel !== i) { sel = i; renderMenu(); } });
      menu.appendChild(row);
    });
    menu.hidden = false;
    const on = menu.querySelector(".pf-item.on");
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest" });
  }

  function accept(i) {
    const it = items[i];
    if (!it || !span) return;
    const v = ta.value;
    ta.value = v.slice(0, span.start) + it.insertion + v.slice(span.end);
    const caret = span.start + it.insertion.length;
    ta.setSelectionRange(caret, caret);
    hide();
    dismissedFor = null;
    ta.focus();
    changed();
    // A path that ended in "/" can keep completing.
    if (it.insertion.endsWith("/")) schedule();
  }

  async function fetchSuggestions() {
    const text = ta.value;
    if (dismissedFor === text) { hide(); return; }
    const seq = ++reqSeq;
    let res;
    try { res = await call("complete_prompt", { text, cursor: ta.selectionStart, cwd: cwd() }); }
    catch (_) { return; }
    if (seq !== reqSeq || !root.isConnected) return;
    if (!res || !res.items || !res.items.length || document.activeElement !== ta) { hide(); return; }
    items = res.items; span = { start: res.start, end: res.end }; sel = 0;
    renderMenu();
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(fetchSuggestions, 120);
  }

  // Enablement notice for the leading command, once the user has moved on
  // (whitespace after the token) — never mid-word.
  async function checkNotice() {
    const text = ta.value;
    if (!/^\/\S+\s/.test(text)) { notice.hidden = true; return; }
    let res;
    try { res = await call("command_enablement", { text, cwd: cwd() }); } catch (_) { return; }
    if (!root.isConnected || ta.value !== text) return;
    notice.textContent = "";
    notice.className = "pf-notice";
    if (!res || (res.ok && !res.unknown)) { notice.hidden = true; return; }
    if (res.unknown) {
      notice.classList.add("muted");
      notice.textContent = `no command named /${res.command} found — it will be sent as typed`;
      notice.hidden = false;
      return;
    }
    notice.classList.add("amber");
    const folder = res.folder_name || "this folder";
    notice.appendChild(el("span", null,
      `⚠ /${res.command} comes from ${res.plugin_name}, which isn't enabled in ${folder}`));
    if (res.folder) {
      const btn = el("button", "btn ghost small", `Enable in ${folder}`);
      btn.addEventListener("click", async () => {
        btn.disabled = true; btn.textContent = "Enabling…";
        try {
          await call("plugin_action", { action: "enable", plugin_id: res.plugin_id,
                                        scope: "project", cwd: res.folder });
        } catch (_) { /* fall through to the poll */ }
        // The CLI job runs in the background — poll until the settings file flips.
        for (let i = 0; i < 10; i++) {
          await new Promise((r) => setTimeout(r, 1000));
          let again;
          try { again = await call("command_enablement", { text: ta.value, cwd: cwd() }); } catch (_) { break; }
          if (again && again.ok) { notice.hidden = true; return; }
        }
        btn.disabled = false; btn.textContent = `Enable in ${folder}`;
      });
      notice.appendChild(btn);
    }
    notice.hidden = false;
  }
  function scheduleNotice() {
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(checkNotice, 400);
  }

  function changed() {
    if (opts.onChange) opts.onChange(ta.value);
    scheduleNotice();
  }

  ta.addEventListener("input", () => { dismissedFor = null; changed(); schedule(); });
  ta.addEventListener("click", schedule);
  ta.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== ta) hide(); }, 120));
  ta.addEventListener("keydown", (e) => {
    const open = !menu.hidden && items.length;
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      if (opts.onSubmit) { e.preventDefault(); opts.onSubmit(ta.value); }
      return;
    }
    if (!open) {
      if (e.key === "Escape") return;  // let panels.js handle it
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); sel = (sel + 1) % items.length; renderMenu(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = (sel - 1 + items.length) % items.length; renderMenu(); }
    else if (e.key === "Tab" || e.key === "Enter") { e.preventDefault(); accept(sel); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); dismissedFor = ta.value; hide(); }
  });

  if (ta.value) scheduleNotice();
  return {
    root, textarea: ta,
    getValue: () => ta.value,
    setValue: (v) => { ta.value = v || ""; hide(); changed(); },
    focus: () => ta.focus(),
    recheck: () => { scheduleNotice(); },
  };
}
