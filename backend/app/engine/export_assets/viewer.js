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

  function showSymbol(id) {
    var s = symbolById[id];
    details.textContent = "";
    if (!s) return;
    details.appendChild(el("h2", s.name));
    details.appendChild(el("p", s.kind + " · " + byFile[s.file_id].path + ":" + s.start_line, "muted"));
    if (s.signature) details.appendChild(el("pre", s.signature, "code"));
    var card = s.card || {};
    if (card.summary) details.appendChild(el("p", card.summary));
    if (card.details) details.appendChild(el("p", card.details, "muted"));
    (card.params || []).forEach(function (p) { details.appendChild(el("p", p.name + " — " + p.description)); });
    if (card.returns) details.appendChild(el("p", "Returns: " + card.returns));
    if (s.uses.length) {
      details.appendChild(el("h3", "Uses (matched by name)"));
      s.uses.forEach(function (target) {
        var t = symbolById[target];
        if (t) details.appendChild(linkButton(t.name, function () { go("file=" + t.file_id + "&line=" + t.start_line + "&symbol=" + t.id); }));
      });
    }
  }

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
        var b = linkButton(at < 0 ? text : s.name, function () { showSymbol(s.id); });
        b.classList.add("def");
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
    if (symbol) showSymbol(symbol);
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
