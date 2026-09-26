/**
 * Delayed tooltip for any element carrying `data-tip`.
 *
 * One floating bubble on <body> (so a scrolling panel can never clip it),
 * delegated from the document so re-rendered lists need no rebinding.
 * Opens after a short hover or keyboard-focus delay; once a tooltip has been
 * shown, moving to the next trigger swaps the text without waiting again.
 * Stays open while the pointer is over the bubble (WCAG 1.4.13) and closes on
 * leave, blur, scroll, a click, Esc, or when the tab is hidden. Text only —
 * never HTML.
 */
(function () {
    'use strict';

    const SHOW_DELAY_MS = 500;
    const WARM_WINDOW_MS = 300;
    const HIDE_GRACE_MS = 120;
    const EDGE_PX = 8;
    const GAP_PX = 6;

    let bubble = null;
    let trigger = null;
    let showTimer = 0;
    let hideTimer = 0;
    let watchTimer = 0;
    let lastHiddenAt = -Infinity;
    let pointer = { x: -1, y: -1 };
    let savedDescribedBy = null;

    function ensureBubble() {
        if (bubble) return bubble;
        bubble = document.createElement('div');
        bubble.id = 'jeen-tip';
        bubble.className = 'jeen-tip';
        bubble.setAttribute('role', 'tooltip');
        bubble.hidden = true;
        bubble.addEventListener('pointerenter', () => clearTimeout(hideTimer));
        bubble.addEventListener('pointerleave', () => scheduleHide());
        document.body.appendChild(bubble);
        return bubble;
    }

    function tipOf(el) {
        const text = el && el.getAttribute('data-tip');
        return text && text.trim() ? text : '';
    }

    function targetFrom(node) {
        const el = node && node.nodeType === 1 ? node : node && node.parentElement;
        return el && typeof el.closest === 'function' ? el.closest('[data-tip]') : null;
    }

    function place() {
        if (!bubble || !trigger) return;
        const rect = trigger.getBoundingClientRect();
        const vw = document.documentElement.clientWidth || window.innerWidth;
        const vh = document.documentElement.clientHeight || window.innerHeight;
        const w = bubble.offsetWidth;
        const h = bubble.offsetHeight;
        const rtl = (document.documentElement.getAttribute('dir') || '').toLowerCase() === 'rtl';
        let left = rtl ? rect.right - w : rect.left;
        left = Math.max(EDGE_PX, Math.min(left, vw - w - EDGE_PX));
        let top = rect.bottom + GAP_PX;
        if (top + h > vh - EDGE_PX && rect.top - GAP_PX - h >= EDGE_PX) top = rect.top - GAP_PX - h;
        top = Math.max(EDGE_PX, Math.min(top, vh - h - EDGE_PX));
        bubble.style.left = `${Math.round(left)}px`;
        bubble.style.top = `${Math.round(top)}px`;
    }

    function describe(el) {
        savedDescribedBy = el.getAttribute('aria-describedby');
        const ids = (savedDescribedBy || '').split(/\s+/).filter(Boolean);
        if (!ids.includes('jeen-tip')) ids.push('jeen-tip');
        el.setAttribute('aria-describedby', ids.join(' '));
    }

    function undescribe(el) {
        if (!el) return;
        if (savedDescribedBy) el.setAttribute('aria-describedby', savedDescribedBy);
        else el.removeAttribute('aria-describedby');
        savedDescribedBy = null;
    }

    function show(el) {
        const text = tipOf(el);
        if (!text) return;
        ensureBubble();
        if (trigger && trigger !== el) undescribe(trigger);
        trigger = el;
        bubble.textContent = text;
        bubble.hidden = false;
        bubble.style.visibility = 'hidden';
        place();
        bubble.style.visibility = '';
        describe(el);
        startWatch();
    }

    function hide() {
        clearTimeout(showTimer);
        clearTimeout(hideTimer);
        stopWatch();
        if (bubble && !bubble.hidden) {
            bubble.hidden = true;
            lastHiddenAt = performance.now();
        }
        undescribe(trigger);
        trigger = null;
    }

    function scheduleShow(el) {
        clearTimeout(showTimer);
        clearTimeout(hideTimer);
        const warm = (bubble && !bubble.hidden) || performance.now() - lastHiddenAt < WARM_WINDOW_MS;
        if (warm) {
            show(el);
            return;
        }
        showTimer = setTimeout(() => show(el), SHOW_DELAY_MS);
    }

    function scheduleHide() {
        clearTimeout(showTimer);
        clearTimeout(hideTimer);
        hideTimer = setTimeout(hide, HIDE_GRACE_MS);
    }

    // Live lists re-render under a still pointer (a new trace step arrives):
    // follow the replacement element, refresh its text, or close.
    function startWatch() {
        stopWatch();
        watchTimer = setInterval(() => {
            if (!trigger) return stopWatch();
            if (trigger.isConnected) {
                const text = tipOf(trigger);
                if (!text) return hide();
                if (bubble.textContent !== text) {
                    bubble.textContent = text;
                    place();
                }
                return;
            }
            const replacement = pointer.x >= 0 ? targetFrom(document.elementFromPoint(pointer.x, pointer.y)) : null;
            if (replacement) show(replacement);
            else hide();
        }, 250);
    }

    function stopWatch() {
        clearInterval(watchTimer);
        watchTimer = 0;
    }

    function onPointerOver(event) {
        if (event.pointerType === 'touch') return;
        pointer = { x: event.clientX, y: event.clientY };
        const el = targetFrom(event.target);
        if (!el) return;
        if (el === trigger) {
            clearTimeout(hideTimer);
            return;
        }
        scheduleShow(el);
    }

    function onPointerOut(event) {
        if (event.pointerType === 'touch') return;
        const from = targetFrom(event.target);
        if (!from) return;
        const to = event.relatedTarget;
        if (to && (from.contains(to) || (bubble && bubble.contains(to)))) return;
        if (from === trigger || !trigger) scheduleHide();
    }

    function onPointerMove(event) {
        pointer = { x: event.clientX, y: event.clientY };
    }

    function onFocusIn(event) {
        const el = targetFrom(event.target);
        if (el && event.target === el) scheduleShow(el);
    }

    function onFocusOut(event) {
        const el = targetFrom(event.target);
        if (el && (el === trigger || !trigger)) hide();
    }

    function onKeyDown(event) {
        if (event.key === 'Escape' && trigger) hide();
    }

    function init() {
        document.addEventListener('pointerover', onPointerOver, true);
        document.addEventListener('pointerout', onPointerOut, true);
        document.addEventListener('pointermove', onPointerMove, { capture: true, passive: true });
        document.addEventListener('focusin', onFocusIn, true);
        document.addEventListener('focusout', onFocusOut, true);
        document.addEventListener('keydown', onKeyDown, true);
        document.addEventListener('pointerdown', (event) => {
            if (!bubble || !bubble.contains(event.target)) hide();
        }, true);
        window.addEventListener('scroll', (event) => {
            if (bubble && event.target instanceof Node && bubble.contains(event.target)) return;
            if (trigger) hide();
        }, true);
        window.addEventListener('resize', hide);
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) hide();
        });
    }

    if (typeof window !== 'undefined') {
        window.JeenTooltip = { hide };
        if (typeof document !== 'undefined') {
            if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
            else init();
        }
    }
})();
