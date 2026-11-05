/* ============================================================================
   ウッドプラスチックテクノロジーポータル v3 — 最小限のJS

   ここでやることは2つだけ。ページに該当する部品がなければ何もしない。
     1. スマホでのメニュー開閉
     2. トップのメインビジュアル（3枚を順に切り替える）

   data/*.json を読んで表示する処理は、それぞれのページのHTMLの中に書いてある。
   （ニュース一覧の絞り込みと、エントリーフォームの下書きもそちら）
   ============================================================================ */

(function () {
  "use strict";

  var $$ = function (s, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(s));
  };

  /* --- 1. スマホのメニュー開閉 ---------------------------------------- */

  var toggle = document.querySelector(".nav-toggle");

  if (toggle) {
    var setOpen = function (open) {
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", String(open));
    };

    toggle.addEventListener("click", function () {
      setOpen(!document.body.classList.contains("nav-open"));
    });

    // メニューの中のリンクを押したとき（同じページ内の移動を含む）と、Esc で閉じる
    $$(".hd-nav a").forEach(function (a) {
      a.addEventListener("click", function () { setOpen(false); });
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") setOpen(false);
    });
  }

  /* --- 2. トップのメインビジュアル ------------------------------------ */
  // 6秒ごとに切り替える。点を押したらその写真に移り、そこから数え直す。
  // 別のタブを見ている間は止める

  var slides = $$(".slides .slide");
  var dots = $$(".slides .dots button");

  if (slides.length > 1) {
    var cur = 0, timer = null;

    var show = function (n) {
      slides[cur].classList.remove("is-on");
      if (dots[cur]) dots[cur].classList.remove("is-on");
      cur = (n + slides.length) % slides.length;
      slides[cur].classList.add("is-on");
      if (dots[cur]) dots[cur].classList.add("is-on");
    };
    var start = function () {
      clearInterval(timer);
      timer = setInterval(function () { show(cur + 1); }, 6000);
    };

    dots.forEach(function (d, n) {
      d.addEventListener("click", function () { show(n); start(); });
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) clearInterval(timer); else start();
    });
    start();
  }
})();
