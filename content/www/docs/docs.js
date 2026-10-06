/* docs.js — interações da wiki CelerOS Hub.
   Zero dependências: reveal-on-scroll, copiar código, TOC scrollspy,
   lightbox de screenshots, menu mobile e botões "testar" de endpoints GET
   públicos (fetch same-origin contra o próprio hub). */
(function () {
  "use strict";

  /* ---------------------------------------------------- menu mobile ----- */
  var btn = document.querySelector(".menu-btn");
  var side = document.querySelector(".sidebar");
  var back = document.querySelector(".backdrop");
  function closeMenu() {
    if (side) side.classList.remove("open");
    if (back) back.classList.remove("show");
  }
  if (btn && side && back) {
    btn.addEventListener("click", function () {
      side.classList.toggle("open");
      back.classList.toggle("show", side.classList.contains("open"));
    });
    back.addEventListener("click", closeMenu);
    side.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", closeMenu);
    });
  }

  /* --------------------------------------------- reveal on scroll ------ */
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { threshold: 0.08 });
    document.querySelectorAll(".rv").forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll(".rv").forEach(function (el) { el.classList.add("in"); });
  }

  /* ------------------------------------------------ copiar código ------ */
  document.querySelectorAll(".code").forEach(function (box) {
    var head = box.querySelector(".code-head");
    var pre = box.querySelector("pre");
    if (!head || !pre) return;
    var b = document.createElement("button");
    b.className = "copy";
    b.type = "button";
    b.textContent = "copiar";
    b.addEventListener("click", function () {
      var txt = pre.innerText;
      var done = function () {
        b.textContent = "copiado ✓";
        b.classList.add("done");
        setTimeout(function () { b.textContent = "copiar"; b.classList.remove("done"); }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done, done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = txt; document.body.appendChild(ta);
        ta.select(); try { document.execCommand("copy"); } catch (e) {}
        document.body.removeChild(ta); done();
      }
    });
    head.appendChild(b);
  });

  /* ------------------------------------------------------ lightbox ----- */
  var lb = document.createElement("div");
  lb.className = "lb";
  lb.innerHTML = '<img alt=""><span class="cap"></span>';
  document.body.appendChild(lb);
  var lbImg = lb.querySelector("img");
  var lbCap = lb.querySelector(".cap");
  lb.addEventListener("click", function () { lb.classList.remove("open"); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") lb.classList.remove("open");
  });
  function zoom(img) {
    lbImg.src = img.src;
    lbImg.style.borderRadius = "";
    var fig = img.closest("figure.shot");
    if (fig && fig.classList.contains("round")) {
      lbImg.style.borderRadius = "50%";
    }
    var cap = img.closest("figure") && img.closest("figure").querySelector("figcaption b");
    lbCap.textContent = cap ? cap.textContent : (img.alt || "");
    lb.classList.add("open");
  }
  document.querySelectorAll(
    "figure.shot img, .mosaic .m img, img[data-zoom]"
  ).forEach(function (img) { img.addEventListener("click", function () { zoom(img); }); });

  /* --------------------------------------------------- TOC scrollspy --- */
  var toc = document.querySelector(".toc");
  if (toc) {
    var links = Array.prototype.slice.call(toc.querySelectorAll("a"));
    var targets = links.map(function (a) {
      return document.getElementById(a.getAttribute("href").slice(1));
    });
    var spy = function () {
      var y = window.scrollY + 120, best = 0;
      targets.forEach(function (t, i) { if (t && t.offsetTop <= y) best = i; });
      links.forEach(function (a, i) { a.classList.toggle("on", i === best); });
    };
    window.addEventListener("scroll", spy, { passive: true });
    spy();
  }

  /* ---------------------------------------------- botões "testar" ----- */
  // <button class="try" data-ep="/api/info">testar</button> → fetch GET e
  // mostra o JSON (truncado) num <pre> seguinte; só endpoints públicos.
  document.querySelectorAll("button.try[data-ep]").forEach(function (b) {
    b.addEventListener("click", function () {
      var ep = b.getAttribute("data-ep");
      var out = b.nextElementSibling;
      if (!out || !out.classList.contains("try-out")) {
        out = document.createElement("pre");
        out.className = "try-out";
        out.style.cssText = "font:12px/1.5 var(--mono,#333);color:#93a3c8;margin:10px 0 0;overflow-x:auto";
        b.parentNode.insertBefore(out, b.nextSibling);
      }
      out.textContent = "→ GET " + ep + " …";
      b.classList.add("out");
      fetch(ep, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.text(); })
        .then(function (t) {
          try { t = JSON.stringify(JSON.parse(t), null, 1); } catch (e) {}
          if (t.length > 1400) t = t.slice(0, 1400) + "\n… (truncado)";
          out.textContent = "→ GET " + ep + "\n" + t;
        })
        .catch(function (e) { out.textContent = "✗ " + e; });
    });
  });

  /* ------------------------------------------- stats vivos (landing) -- */
  var hubEl = document.querySelector("[data-hub-info]");
  if (hubEl) {
    fetch("/api/info").then(function (r) { return r.json(); }).then(function (j) {
      var put = function (sel, val) {
        var el = document.querySelector(sel);
        if (!el || val === undefined || val === null) return;
        // /api/info traz listas em alguns campos (ex.: store.categories e o
        // nome das boards): o card quer a CONTAGEM, não a lista serializada
        // (string sem espaços não quebra linha e estoura o layout no mobile).
        if (Array.isArray(val)) val = val.length;
        el.textContent = val;
      };
      put("[data-st=apps]", j.store && j.store.apps);
      put("[data-st=cats]", j.store && j.store.categories);
      put("[data-st=dl]", j.downloads_total);
      put("[data-st=api]", j.firmware && j.firmware.api_level);
      put("[data-st=ver]", j.version);
      put("[data-st=boards]", j.boards ? Object.keys(j.boards).length : null);
    }).catch(function () {});
  }
})();
