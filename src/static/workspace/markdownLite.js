/**
 * MarkdownLite - a tiny, dependency-free, escape-first Markdown renderer.
 *
 * Security model: the input is UNTRUSTED (LLM output that may echo user/DB
 * text). Every source-derived substring is HTML-escaped BEFORE any markup is
 * added, and the only tags ever emitted are a fixed allowlist with NO
 * attributes carrying source data. Therefore the returned string is safe to
 * assign to innerHTML without a separate sanitizer. Raw HTML in the source is
 * never interpreted - it renders as escaped text.
 *
 * Supported subset: ATX headings (# .. ###### -> h2..h4, never h1), paragraphs,
 * bold (**), italic (*), inline `code`, fenced ``` code blocks, blockquotes,
 * unordered/ordered lists, and GFM pipe tables. Links and images are
 * deliberately NOT supported in v1 (no URL schemes to normalize). Underscores
 * are NOT emphasis, so identifiers like sales_amount stay intact.
 *
 * Parsing is bounded and linear (line-by-line blocks + a single-pass inline
 * tokenizer with non-nested, bounded regexes); input above MAX_INPUT falls back
 * to escaped plain text.
 */
(function () {
    'use strict';

    var MAX_INPUT = 65536;

    function esc(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    // Inline formatting. The input is raw (unescaped) text; every non-markup
    // segment is escaped here, so callers pass raw strings and get safe HTML.
    function inlineMd(raw) {
        // Split out inline code spans first so their contents are never treated
        // as emphasis and are escaped verbatim.
        var parts = String(raw == null ? '' : raw).split(/(`[^`]+`)/g);
        return parts.map(function (seg) {
            if (seg.length >= 2 && seg.charAt(0) === '`' && seg.charAt(seg.length - 1) === '`') {
                return '<code>' + esc(seg.slice(1, -1)) + '</code>';
            }
            var s = esc(seg);
            // Content is already escaped, so the captured groups are safe.
            s = s.replace(/\*\*([^*]+)\*\*/g, function (_, g) { return '<strong>' + g + '</strong>'; });
            s = s.replace(/\*([^*]+)\*/g, function (_, g) { return '<em>' + g + '</em>'; });
            return s;
        }).join('');
    }

    // One table row -> array of raw cell strings (honours escaped \| ).
    function splitRow(line) {
        var s = String(line).trim().replace(/\\\|/g, '\u0001');
        if (s.charAt(0) === '|') s = s.slice(1);
        if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
        return s.split('|').map(function (c) { return c.trim().replace(/\u0001/g, '|'); });
    }

    function renderTable(header, rows) {
        var th = header.map(function (c) { return '<th>' + inlineMd(c) + '</th>'; }).join('');
        var trs = rows.map(function (r) {
            var tds = [];
            for (var k = 0; k < header.length; k++) {
                tds.push('<td>' + inlineMd(r[k] != null ? r[k] : '') + '</td>');
            }
            return '<tr>' + tds.join('') + '</tr>';
        }).join('');
        return '<table><thead><tr>' + th + '</tr></thead><tbody>' + trs + '</tbody></table>';
    }

    var DELIM_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

    function render(src) {
        var text = String(src == null ? '' : src);
        if (text.length > MAX_INPUT) {
            return '<p>' + esc(text).replace(/\n/g, '<br>') + '</p>';
        }
        var lines = text.replace(/\r\n?/g, '\n').split('\n');
        var out = [];
        var para = [];
        var i = 0;

        function flushPara() {
            if (para.length) {
                out.push('<p>' + para.map(inlineMd).join('<br>') + '</p>');
                para = [];
            }
        }

        while (i < lines.length) {
            var line = lines[i];

            // Fenced code block.
            if (/^\s*```/.test(line)) {
                flushPara();
                i++;
                var code = [];
                while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i++; }
                if (i < lines.length) i++; // closing fence
                out.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
                continue;
            }

            // Blank line.
            if (/^\s*$/.test(line)) { flushPara(); i++; continue; }

            // ATX heading -> h2..h4 (never h1, so a card has no page-level heading).
            var hm = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
            if (hm) {
                flushPara();
                var lvl = Math.min(hm[1].length + 1, 4);
                out.push('<h' + lvl + '>' + inlineMd(hm[2]) + '</h' + lvl + '>');
                i++;
                continue;
            }

            // GFM pipe table: a header row followed by a delimiter row.
            if (line.indexOf('|') >= 0 && i + 1 < lines.length && DELIM_RE.test(lines[i + 1])) {
                flushPara();
                var header = splitRow(line);
                i += 2;
                var rows = [];
                while (i < lines.length && lines[i].indexOf('|') >= 0 && !/^\s*$/.test(lines[i])) {
                    rows.push(splitRow(lines[i]));
                    i++;
                }
                out.push(renderTable(header, rows));
                continue;
            }

            // Blockquote.
            if (/^\s*>\s?/.test(line)) {
                flushPara();
                var q = [];
                while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
                    q.push(lines[i].replace(/^\s*>\s?/, ''));
                    i++;
                }
                out.push('<blockquote>' + q.map(inlineMd).join('<br>') + '</blockquote>');
                continue;
            }

            // Unordered list.
            if (/^\s*[-*+]\s+/.test(line)) {
                flushPara();
                var uItems = [];
                while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
                    uItems.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
                    i++;
                }
                out.push('<ul>' + uItems.map(function (it) { return '<li>' + inlineMd(it) + '</li>'; }).join('') + '</ul>');
                continue;
            }

            // Ordered list.
            if (/^\s*\d+[.)]\s+/.test(line)) {
                flushPara();
                var oItems = [];
                while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
                    oItems.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''));
                    i++;
                }
                out.push('<ol>' + oItems.map(function (it) { return '<li>' + inlineMd(it) + '</li>'; }).join('') + '</ol>');
                continue;
            }

            // Otherwise: paragraph text (consecutive lines joined with <br>).
            para.push(line);
            i++;
        }
        flushPara();
        return out.join('\n');
    }

    window.MarkdownLite = { render: render };
})();
