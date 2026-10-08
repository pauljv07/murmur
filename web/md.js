// Tiny markdown renderer (headings, bullets, numbered lists, bold, italic, code).
// `mine` is an optional predicate(lineText) -> bool used to shade the user's own points.
function renderMarkdown(src, mine) {
  const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|\W)\*([^*\s][^*]*)\*(?=\W|$)/g, "$1<i>$2</i>");
  const cls = t => mine ? (mine(t) ? ' class="mine"' : ' class="ai"') : "";
  let html = "", list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of src.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)/))) {
      close(); const n = Math.min(m[1].length + 1, 4);
      html += `<h${n}>${inline(m[2])}</h${n}>`;
    } else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      if (list !== "ul") { close(); html += "<ul>"; list = "ul"; }
      html += `<li${cls(m[1])}>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { close(); html += "<ol>"; list = "ol"; }
      html += `<li${cls(m[1])}>${inline(m[1])}</li>`;
    } else if (line.trim() === "") {
      close();
    } else {
      close(); html += `<p${cls(line)}>${inline(line)}</p>`;
    }
  }
  close();
  return html;
}
