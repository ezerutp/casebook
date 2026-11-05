// Copiar un borrador para pegarlo en Jira o en Slack. Los borradores vienen en markdown, y copiado como texto plano
// el editor de Jira muestra los ** y los * literales: hay que poner HTML en el portapapeles, que Jira y Slack
// respetan al pegar. Junto al HTML va un texto plano por si el destino no acepta HTML: para Jira el markdown tal cual,
// para Slack el formato de Slack (*negrita*, _cursiva_, viñetas •).
// Cubre el markdown que escriben los borradores: párrafos, listas (con anidación), títulos, citas, bloques de código,
// **negrita**, *cursiva*, `código` y [enlaces](url).

const richCopy = (() => {
  const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // Lo que va entre comillas invertidas y los enlaces se apartan antes de aplicar negrita y cursiva, para que un * o un _
  // dentro de ellos no se interprete.
  function inline(text, fmt) {
    const held = [];
    const hold = (s) => `\u0000${held.push(s) - 1}\u0000`;
    let s = text.replace(/\\([\\`*_[\]().#+\-!>])/g, (_, c) => hold(fmt.text(c)));
    s = s.replace(/`([^`]+)`/g, (_, code) => hold(fmt.code(code)));
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => hold(fmt.link(label, url)));
    const italics = (t) => t
      .replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?![*\w])/g, (_, pre, i) => pre + fmt.italic(i))
      .replace(/(^|[^_\w])_(?=[^\s_])([^_]*?[^\s_])_(?![_\w])/g, (_, pre, i) => pre + fmt.italic(i));
    s = fmt.text(s);
    // La negrita ya convertida se aparta: en Slack queda como *x*, que si no se leería después como cursiva.
    s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, (_, b) => hold(fmt.bold(italics(b))));
    s = s.replace(/__(?=\S)([\s\S]*?\S)__/g, (_, b) => hold(fmt.bold(italics(b))));
    s = italics(s);
    // Los apartados pueden anidarse (un enlace dentro de una negrita), así que se restituyen hasta que no quede ninguno.
    while (/\u0000\d+\u0000/.test(s)) s = s.replace(/\u0000(\d+)\u0000/g, (_, n) => held[Number(n)]);
    return s;
  }

  const LIST_ITEM = /^(\s*)([*+-]|\d+[.)])\s+(.*)$/;

  // Bloques: código, títulos, citas, listas y párrafos. Cada uno llega a `out` ya separado.
  function blocks(md, out) {
    const lines = md.replace(/\r\n?/g, "\n").split("\n");
    for (let i = 0; i < lines.length; ) {
      const line = lines[i];
      if (!line.trim()) {
        i++;
        continue;
      }
      const fence = line.match(/^\s*(```|~~~)/);
      if (fence) {
        const code = [];
        for (i++; i < lines.length && !lines[i].trim().startsWith(fence[1]); i++) code.push(lines[i]);
        i++;
        out.code(code.join("\n"));
        continue;
      }
      const heading = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (heading) {
        out.heading(heading[1].length, heading[2]);
        i++;
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quote = [];
        for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ""));
        out.quote(quote);
        continue;
      }
      if (LIST_ITEM.test(line)) {
        const items = [];
        for (; i < lines.length && lines[i].trim(); i++) {
          const m = lines[i].match(LIST_ITEM);
          if (m) items.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]), text: m[3] });
          else if (items.length) items[items.length - 1].text += `\n${lines[i].trim()}`;
        }
        out.list(items);
        continue;
      }
      const para = [];
      for (; i < lines.length && lines[i].trim() && !LIST_ITEM.test(lines[i]) && !/^\s*(```|~~~|#{1,6}\s|>)/.test(lines[i]); i++) {
        para.push(lines[i].trim());
      }
      out.paragraph(para);
    }
  }

  const HTML = {
    text: escapeHtml,
    code: (c) => `<code>${escapeHtml(c)}</code>`,
    link: (label, url) => `<a href="${escapeHtml(url)}">${escapeHtml(label)}</a>`,
    bold: (b) => `<strong>${b}</strong>`,
    italic: (i) => `<em>${i}</em>`,
  };

  // Las listas se anidan por sangría: un ítem más sangrado que el anterior abre una sublista dentro de él.
  function htmlList(items) {
    let html = "";
    const open = [];
    for (const item of items) {
      while (open.length && item.indent < open[open.length - 1].indent) html += `</li></${open.pop().tag}>`;
      const top = open[open.length - 1];
      if (!top || item.indent > top.indent) {
        const tag = item.ordered ? "ol" : "ul";
        open.push({ indent: item.indent, tag });
        html += `<${tag}>`;
      } else {
        html += "</li>";
      }
      html += `<li>${item.text.split("\n").map((l) => inline(l, HTML)).join("<br>")}`;
    }
    while (open.length) html += `</li></${open.pop().tag}>`;
    return html;
  }

  function toHtml(md) {
    const parts = [];
    blocks(md, {
      code: (c) => parts.push(`<pre><code>${escapeHtml(c)}</code></pre>`),
      heading: (level, t) => parts.push(`<h${level}>${inline(t, HTML)}</h${level}>`),
      quote: (lines) => parts.push(`<blockquote>${toHtml(lines.join("\n"))}</blockquote>`),
      list: (items) => parts.push(htmlList(items)),
      paragraph: (lines) => parts.push(`<p>${lines.map((l) => inline(l, HTML)).join("<br>")}</p>`),
    });
    return parts.join("\n");
  }

  const SLACK = {
    text: (s) => s,
    code: (c) => `\`${c}\``,
    link: (label, url) => (label === url ? url : `${label} (${url})`),
    bold: (b) => `*${b}*`,
    italic: (i) => `_${i}_`,
  };

  function toSlack(md) {
    const parts = [];
    blocks(md, {
      code: (c) => parts.push(`\`\`\`\n${c}\n\`\`\``),
      heading: (_, t) => parts.push(`*${inline(t, SLACK).replace(/^\*(.*)\*$/, "$1")}*`),
      quote: (lines) => parts.push(toSlack(lines.join("\n")).split("\n").map((l) => `> ${l}`).join("\n")),
      list: (items) => {
        const base = Math.min(...items.map((it) => it.indent));
        const counters = [];
        parts.push(items.map((it) => {
          const depth = Math.round((it.indent - base) / 2);
          counters.length = depth + 1;
          counters[depth] = (counters[depth] || 0) + 1;
          const marker = it.ordered ? `${counters[depth]}.` : depth ? "◦" : "•";
          return `${"    ".repeat(depth)}${marker} ${it.text.split("\n").map((l) => inline(l, SLACK)).join("\n")}`;
        }).join("\n"));
      },
      paragraph: (lines) => parts.push(lines.map((l) => inline(l, SLACK)).join("\n")),
    });
    return parts.join("\n\n");
  }

  // HTML y texto plano juntos; si el navegador no deja escribir HTML en el portapapeles, al menos el texto plano.
  async function write(html, plain) {
    if (window.ClipboardItem && navigator.clipboard.write) {
      try {
        await navigator.clipboard.write([new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([plain], { type: "text/plain" }),
        })]);
        return;
      } catch {
        // Firefox viejo o permiso denegado: se cae al texto plano.
      }
    }
    await navigator.clipboard.writeText(plain);
  }

  return {
    toHtml,
    toSlack,
    forJira: (md) => write(toHtml(md), md),
    forSlack: (md) => write(toHtml(md), toSlack(md)),
  };
})();

if (typeof module !== "undefined") module.exports = richCopy;
