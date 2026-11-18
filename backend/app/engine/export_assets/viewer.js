// Offline reader for a codehover export. Every piece of repository or model text is inserted with
// textContent, never as HTML; the page loads nothing but its own files (CSP in index.html).
(function () {
  "use strict";
  var data = window.CODEHOVER_DATA;
  var byFile = {};
  var symbolsByFile = {};
  var symbolById = {};
  data.files.forEach(function (f) { byFile[f.id] = f; });
  data.symbols.forEach(function (s) {
    (symbolsByFile[s.file_id] = symbolsByFile[s.file_id] || []).push(s);
    symbolById[s.id] = s;
  });

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function linkButton(label, onClick) {
    var b = el("button", label, "link");
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }
  function go(hash) { window.location.hash = hash; }

  document.getElementById("title").textContent = data.project.name;
  document.getElementById("meta").textContent =
    data.project.repository + " · commit " + data.build.commit_sha.slice(0, 12) +
    " · exported copy; AI-generated text must be checked against the cited source.";

  var guideList = document.getElementById("guide");
  data.guide.forEach(function (page) {
    var li = el("li");
    li.appendChild(linkButton(page.title, function () { go("guide=" + encodeURIComponent(page.slug)); }));
    guideList.appendChild(li);
  });
  if (!data.guide.length) guideList.appendChild(el("li", "No guide in this export.", "muted"));
  var fileList = document.getElementById("files");
  data.files.slice().sort(function (a, b) { return a.path < b.path ? -1 : 1; }).forEach(function (f) {
    var li = el("li");
    li.appendChild(linkButton(f.path, function () { go("file=" + encodeURIComponent(f.id)); }));
    fileList.appendChild(li);
  });

  var main = document.getElementById("main");
  var details = document.getElementById("details");

  // The definition card floats at its name, like the hosted viewer (02 "Hover card"): hovering previews
  // it after 200 ms, clicking pins it (a non-modal dialog with a Close button), Esc closes it.
  var card = { anchor: null, pinned: false, openTimer: 0, closeTimer: 0 };
  function place() {
    if (!card.anchor || details.hidden) return;
    var rect = card.anchor.getBoundingClientRect();
    var width = Math.min(480, window.innerWidth - 24);
    details.style.width = width + "px";
    details.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)) + "px";
    var below = window.innerHeight - rect.bottom;
    if (below < 260 && rect.top > below) {
      details.style.top = "";
      details.style.bottom = (window.innerHeight - rect.top + 6) + "px";
    } else {
      details.style.bottom = "";
      details.style.top = (rect.bottom + 6) + "px";
    }
  }
  function hideSymbol() {
    var anchor = card.anchor, wasPinned = card.pinned;
    details.hidden = true;
    details.textContent = "";
    details.removeAttribute("role");
    details.removeAttribute("aria-modal");
    details.setAttribute("aria-label", "Definition details");
    card.anchor = null;
    card.pinned = false;
    if (wasPinned && anchor) anchor.focus();
  }
  function showSymbol(id, anchor, pinned) {
    var s = symbolById[id];
    details.textContent = "";
    if (!s) { hideSymbol(); return; }
    card.anchor = anchor || main.querySelector('[data-symbol="' + id + '"]');
    card.pinned = !!pinned;
    details.hidden = false;
    details.setAttribute("aria-label", s.kind + " " + s.name);
    if (card.pinned) { details.setAttribute("role", "dialog"); details.setAttribute("aria-modal", "false"); }
    var head = el("div", null, "card-head");
    var title = el("h2", s.name);
    title.tabIndex = -1;
    head.appendChild(el("span", s.kind, "badge"));
    head.appendChild(title);
    if (card.pinned) {
      var close = el("button", "Close", "close");
      close.type = "button";
      close.addEventListener("click", hideSymbol);
      head.appendChild(close);
    }
    details.appendChild(head);
    details.appendChild(el("p", byFile[s.file_id].path + ":" + s.start_line, "muted small"));
    if (s.signature) details.appendChild(el("pre", s.signature, "sig"));
    var info = s.card || {};
    if (info.summary) details.appendChild(el("p", info.summary));
    if (info.details) details.appendChild(el("p", info.details, "muted"));
    (info.params || []).forEach(function (p) { details.appendChild(el("p", p.name + " — " + p.description, "small")); });
    if (info.returns) details.appendChild(el("p", "Returns: " + info.returns, "small"));
    if (s.uses.length) {
      details.appendChild(el("h3", "Uses (matched by name)"));
      s.uses.forEach(function (target) {
        var t = symbolById[target];
        if (t) details.appendChild(linkButton(t.name, function () { go("file=" + t.file_id + "&line=" + t.start_line + "&symbol=" + t.id); }));
      });
    }
    if (!card.anchor) details.classList.add("docked"); else details.classList.remove("docked");
    place();
    if (card.pinned) title.focus();
  }
  details.hidden = true;
  details.addEventListener("mouseenter", function () { window.clearTimeout(card.closeTimer); });
  details.addEventListener("mouseleave", function () {
    if (!card.pinned) card.closeTimer = window.setTimeout(hideSymbol, 150);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !details.hidden) hideSymbol();
  });
  window.addEventListener("scroll", place, true);
  window.addEventListener("resize", place);

  function showFile(id, line, end, symbol) {
    var f = byFile[id];
    main.textContent = "";
    if (!f) { main.appendChild(el("p", "File not in this export.")); return; }
    main.appendChild(el("h2", f.path));
    if (f.explanation && f.explanation.summary) main.appendChild(el("p", f.explanation.summary.split("\n\n")[0], "muted"));
    var defs = {};
    (symbolsByFile[id] || []).forEach(function (s) { defs[s.start_line] = s; });
    var pre = el("pre", null, "code");
    f.lines.forEach(function (text, index) {
      var n = index + 1;
      var row = el("span", null, "line" + (line && n >= line && n <= (end || line) ? " mark" : ""));
      row.id = "L" + n;
      row.appendChild(el("span", String(n), "num"));
      var s = defs[n];
      if (s) {
        // Only the identifier is the control, so its visible text is part of its name (WCAG 2.5.3).
        var at = nameAt(text, s.name);
        var label = "Explain " + s.kind + " " + s.name;
        var b = linkButton(at < 0 ? text : s.name, function () { showSymbol(s.id, b, true); });
        b.classList.add("def");
        b.setAttribute("data-symbol", s.id);
        b.setAttribute("aria-haspopup", "dialog");
        b.addEventListener("mouseenter", function () {
          window.clearTimeout(card.closeTimer);
          if (!card.pinned) card.openTimer = window.setTimeout(function () { showSymbol(s.id, b, false); }, 200);
        });
        b.addEventListener("mouseleave", function () {
          window.clearTimeout(card.openTimer);
          if (!card.pinned) card.closeTimer = window.setTimeout(hideSymbol, 150);
        });
        b.setAttribute("aria-label", at < 0 ? text.trim() + ": " + label : label);
        if (at >= 0) row.appendChild(document.createTextNode(text.slice(0, at)));
        row.appendChild(b);
        if (at >= 0) row.appendChild(document.createTextNode(text.slice(at + s.name.length)));
      } else {
        row.appendChild(document.createTextNode(text));
      }
      pre.appendChild(row);
    });
    main.appendChild(pre);
    if (line) { var target = document.getElementById("L" + line); if (target) target.scrollIntoView({ block: "center" }); }
    if (symbol) showSymbol(symbol, null, true);
    else hideSymbol();
  }

  // Index of the first whole-word occurrence of name in text, or -1.
  function nameAt(text, name) {
    var word = /[\w$]/;
    for (var i = name ? text.indexOf(name) : -1; i >= 0; i = text.indexOf(name, i + 1)) {
      if ((i === 0 || !word.test(text[i - 1])) && !word.test(text[i + name.length] || "")) return i;
    }
    return -1;
  }

  var CITATION = /\[\[([0-9a-f-]{36}):(\d+)-(\d+)\]\]/g;
  function showGuide(slug) {
    var page = data.guide.filter(function (p) { return p.slug === slug; })[0];
    hideSymbol();
    main.textContent = "";
    if (!page) { main.appendChild(el("p", "Page not in this export.")); return; }
    var prose = el("div", null, "prose");
    var last = 0, match;
    CITATION.lastIndex = 0;
    while ((match = CITATION.exec(page.markdown)) !== null) {
      prose.appendChild(document.createTextNode(page.markdown.slice(last, match.index)));
      var f = byFile[match[1]];
      var label = "[" + (f ? f.path : "unknown file") + ":" + match[2] + "–" + match[3] + "]";
      if (f) {
        (function (fileId, a, b) {
          prose.appendChild(linkButton(label, function () { go("file=" + fileId + "&line=" + a + "&end=" + b); }));
        })(match[1], match[2], match[3]);
      } else {
        prose.appendChild(el("span", label, "muted"));
      }
      last = match.index + match[0].length;
    }
    prose.appendChild(document.createTextNode(page.markdown.slice(last)));
    main.appendChild(prose);
    main.appendChild(el("h3", "Claims"));
    page.claims.forEach(function (c) {
      var p = el("p");
      p.appendChild(el("span", c.status + (c.waived ? " (waived)" : ""), "badge"));
      p.appendChild(document.createTextNode(" " + c.text + (c.reason ? " — " + c.reason : "")));
      main.appendChild(p);
    });
  }

  function route() {
    var params = {};
    window.location.hash.replace(/^#/, "").split("&").forEach(function (pair) {
      var kv = pair.split("=");
      if (kv[0]) params[kv[0]] = decodeURIComponent(kv[1] || "");
    });
    if (params.file) showFile(params.file, Number(params.line) || null, Number(params.end) || null, params.symbol);
    else if (params.guide) showGuide(params.guide);
    else if (data.guide.length) showGuide(data.guide[0].slug);
  }
  window.addEventListener("hashchange", route);
  route();
})();
