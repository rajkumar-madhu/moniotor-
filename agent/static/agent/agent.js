/* Ops Agent composer: POST via fetch so the page keeps scroll position. */
(function () {
  "use strict";
  const root = document.querySelector(".pg-agent");
  if (!root) return;
  const form = root.querySelector("[data-agent-form]");
  const stream = root.querySelector("[data-stream]");
  const csrf = () => {
    const meta = document.querySelector('meta[name="csrf-cookie-name"]');
    const name = (meta && meta.content) || "linkedeyecsrftoken";
    const match = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]*)"));
    if (match) return decodeURIComponent(match[1]);
    const input = document.querySelector("[name=csrfmiddlewaretoken]");
    return (input && input.value) || "";
  };

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function nl(s) { return esc(s).replace(/\n/g, "<br>"); }

  function citeList(cites) {
    if (!cites || !cites.length) return "";
    const safe = (cites || []).filter(c => c && typeof c.href === "string" && c.href.charAt(0) === "/" && c.href.charAt(1) !== "/");
    if (!safe.length) return "";
    return "<ul class=\"ag-cites\">" + safe.map(c =>
      "<li><a href=\"" + esc(c.href) + "\">" + esc(c.title) + "</a>"
      + (c.site ? " <span class=\"t-mute\">" + esc(c.site) + "</span>" : "") + "</li>"
    ).join("") + "</ul>";
  }

  function append(role, content, cites) {
    const empty = stream.querySelector(".ag-empty");
    if (empty) empty.remove();
    const art = document.createElement("article");
    art.className = "ag-msg ag-" + role;
    art.dataset.role = role;
    art.innerHTML = "<div class=\"ag-who\">" + (role === "user" ? "You" : "Ops Agent") + "</div>"
      + "<div class=\"ag-body\">" + nl(content) + "</div>" + citeList(cites);
    stream.appendChild(art);
    stream.scrollTop = stream.scrollHeight;
  }

  root.querySelectorAll("[data-suggest]").forEach(btn => {
    btn.addEventListener("click", () => {
      const box = form && form.querySelector("textarea[name=content]");
      if (!box) return;
      box.value = btn.getAttribute("data-suggest") || "";
      box.focus();
    });
  });

  if (!form) return;
  form.addEventListener("submit", async (e) => {
    const box = form.querySelector("textarea[name=content]");
    const text = (box && box.value || "").trim();
    if (!text) return;
    e.preventDefault();
    form.classList.add("is-busy");
    const btn = form.querySelector("button[type=submit]");
    if (btn) btn.disabled = true;
    append("user", text, []);
    if (box) box.value = "";
    try {
      const body = new FormData(form);
      body.set("content", text);
      const r = await fetch(form.action, {
        method: "POST",
        headers: { "X-CSRFToken": csrf(), Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
        body,
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.statusText);
      // A new thread needs a full load so the conversation list and title update.
      if (d.url && form.action.indexOf("/agent/new") !== -1) {
        window.location = d.url;
        return;
      }
      if (d.title) {
        const h = root.querySelector(".ag-chat .panel-h h2");
        if (h) h.textContent = d.title;
        const on = root.querySelector(".ag-threads li.on .ag-t-title");
        if (on) on.textContent = d.title;
      }
      if (d.assistant) append("assistant", d.assistant.content || "", d.assistant.citations || []);
      else if (d.url) window.location = d.url;
    } catch (err) {
      append("assistant", "Could not send that: " + err.message, []);
    } finally {
      form.classList.remove("is-busy");
      if (btn) btn.disabled = false;
      if (box) {
        box.style.height = "";
        box.focus();
      }
    }
  });

  const box = form.querySelector("textarea[name=content]");
  function fit() {
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 160) + "px";
  }
  if (box) {
    box.addEventListener("input", fit);
    box.addEventListener("keydown", (e) => {
      // Enter that confirms an IME composition (Chinese, Japanese, Korean...) must not send the half-typed message
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });
  }
  if (stream) stream.scrollTop = stream.scrollHeight;
})();
