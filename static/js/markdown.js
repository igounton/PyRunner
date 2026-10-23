/*
 * PyRunMarkdown — a small, escaped Markdown subset for AI answers (Py AI, Ask
 * brain, AI provider Test). Input is escaped first, so model output can never
 * inject markup. Code spans are kept literal: emphasis rules never run inside
 * `code` (2.0 release test U58: `25 * 1024 * 1024` lost its asterisks).
 * Style the output with templates/cpanel/_markdown_style.html (.pyrun-md).
 */
(function () {
    "use strict";

    function escapeHtml(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function emphasis(s) {
        s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
        s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        return s;
    }

    function inline(s) {
        // s is already HTML-escaped. Split out code spans first so their
        // content is printed as-is and emphasis/link rules skip it.
        return s.split(/(`[^`\n]+`)/).map(function (piece, i) {
            return i % 2 === 1 ? "<code>" + piece.slice(1, -1) + "</code>" : emphasis(piece);
        }).join("");
    }

    function render(text) {
        const out = [];
        const parts = escapeHtml(String(text || "").replace(/\r\n/g, "\n")).split(/```/);
        parts.forEach(function (part, i) {
            if (i % 2 === 1) {
                const body = part.replace(/^[a-zA-Z0-9_-]*\n/, "").replace(/\n$/, "");
                out.push("<pre><code>" + body + "</code></pre>");
                return;
            }
            part.split(/\n{2,}/).forEach(function (block) {
                const lines = block.split("\n").filter(function (l) { return l.trim() !== ""; });
                if (!lines.length) return;
                if (lines.every(function (l) { return /^\s*[-*]\s+/.test(l); })) {
                    out.push("<ul>" + lines.map(function (l) { return "<li>" + inline(l.replace(/^\s*[-*]\s+/, "")) + "</li>"; }).join("") + "</ul>");
                } else if (lines.every(function (l) { return /^\s*\d+[.)]\s+/.test(l); })) {
                    out.push("<ol>" + lines.map(function (l) { return "<li>" + inline(l.replace(/^\s*\d+[.)]\s+/, "")) + "</li>"; }).join("") + "</ol>");
                } else if (lines.length >= 2 && /^\s*\|/.test(lines[0]) && /^\s*\|?\s*:?-{2,}/.test(lines[1])) {
                    const cells = function (l) { return l.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return inline(c.trim()); }); };
                    const head = cells(lines[0]);
                    const rows = lines.slice(2).map(cells);
                    out.push("<table><thead><tr>" + head.map(function (c) { return "<th>" + c + "</th>"; }).join("") + "</tr></thead><tbody>" +
                        rows.map(function (r) { return "<tr>" + r.map(function (c) { return "<td>" + c + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table>");
                } else if (/^#{1,6}\s+/.test(lines[0])) {
                    out.push("<h3>" + inline(lines[0].replace(/^#{1,6}\s+/, "")) + "</h3>");
                    if (lines.length > 1) out.push("<p>" + lines.slice(1).map(inline).join("<br>") + "</p>");
                } else if (lines.length === 1 && /^(-{3,}|\*{3,})$/.test(lines[0].trim())) {
                    out.push("<hr>");
                } else {
                    out.push("<p>" + lines.map(inline).join("<br>") + "</p>");
                }
            });
        });
        return out.join("");
    }

    window.PyRunMarkdown = { render: render, escapeHtml: escapeHtml };
})();
