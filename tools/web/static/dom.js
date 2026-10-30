// The one `el(tag, cls, text)` element factory the page's modules share. Every
// module used to redeclare it (ranking/board/collection/park each had a copy);
// this is the single definition they now import.
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// The SVG sibling — same shape, but with a namespace and attributes rather than
// a class/text. Shared by board.js (the editor) and boardtree.js (the read-only
// tree), which both draw the skill-tree geometry.
export function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}
