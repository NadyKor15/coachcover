// Fit the 1920-wide screens into a narrower window (laptops) without browser zoom.
// The page keeps its 1920-px layout; the body is scaled down by s = window width / 1920 from the
// top-left corner, and the document gets the scaled size, so there is no horizontal scroll and a
// taller page still scrolls vertically. At 1920 and wider nothing is changed (scale is exactly 1).
// window.CC_SCALE (and the CSS variable --cc-scale on <html>) hold the current factor; code that
// compares getBoundingClientRect() sizes with layout pixels divides them by it.
// Loaded in <head>, before the page's own scripts.
(function () {
  'use strict';
  var CANVAS = 1920;
  var root = document.documentElement;
  var style = document.createElement('style');
  style.id = 'cc-fit';
  (document.head || root).appendChild(style);
  window.CC_SCALE = 1;

  function layoutHeight(body) {
    // offsetHeight/scrollHeight are layout sizes (a transform does not change them)
    var h = body.offsetHeight;
    if (getComputedStyle(body).overflowY === 'visible') h = Math.max(h, body.scrollHeight);
    return h;
  }

  function apply() {
    var w = root.clientWidth || window.innerWidth;
    var s = w > 0 && w < CANVAS ? w / CANVAS : 1;
    window.CC_SCALE = s;
    root.style.setProperty('--cc-scale', String(s));
    if (s === 1) {
      if (style.textContent) style.textContent = '';
      root.style.removeProperty('width');
      root.style.removeProperty('height');
      root.style.removeProperty('min-height');
      root.style.removeProperty('overflow-x');
      return;
    }
    style.textContent = 'body{transform:scale(' + s + ');transform-origin:0 0}';
    root.style.width = (CANVAS * s) + 'px';
    root.style.minHeight = '0';  // some pages give <html> a 1201/1024-px min-height; the scaled height replaces it
    root.style.overflowX = 'hidden';
    if (document.body) root.style.height = (layoutHeight(document.body) * s) + 'px';
  }

  function fit() {
    // a vertical scrollbar that appears or goes away changes the usable width: settle in a few passes
    for (var i = 0; i < 3; i++) {
      var before = root.clientWidth;
      apply();
      if (root.clientWidth === before) break;
    }
  }

  fit();
  window.addEventListener('resize', fit);
  function watchBody() {
    fit();
    // the body's height changes when the data arrives; keep the scrolled size in step with it
    if (window.ResizeObserver) new ResizeObserver(fit).observe(document.body);
  }
  if (document.body) watchBody();
  else document.addEventListener('DOMContentLoaded', watchBody);
})();
