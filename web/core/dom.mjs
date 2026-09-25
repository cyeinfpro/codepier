// Reconcile passive management snapshots. Never apply this to the editor, native
// transcript, access forms or in-flight dialogs. Their local state is authoritative.
const generatedId = /^(field|hint|group)-[a-f0-9]{32}$/;
function key(node) {
  if (node.nodeType !== 1) return null;
  return node.dataset.cpKey || (!generatedId.test(node.id) && node.id) || null;
}
function compatible(a, b) {
  return (
    a.nodeType === b.nodeType &&
    (a.nodeType !== 1 || (a.tagName === b.tagName && a.namespaceURI === b.namespaceURI))
  );
}
function reconcile(parent, source) {
  const old = [...parent.childNodes],
    used = new Set();
  const keyed = new Map();
  for (const node of old) {
    const value = key(node);
    if (value) keyed.set(value, keyed.has(value) ? null : node);
  }
  let cursor = parent.firstChild;
  for (const incoming of [...source.childNodes]) {
    const identity = key(incoming);
    let target = identity
      ? keyed.get(identity)
      : old.find((node) => !used.has(node) && !key(node) && compatible(node, incoming));
    if (!target || used.has(target) || !compatible(target, incoming))
      target = incoming.cloneNode(true);
    else patch(target, incoming);
    used.add(target);
    if (target !== cursor) parent.insertBefore(target, cursor);
    cursor = target.nextSibling;
  }
  for (const node of old) if (!used.has(node) && node.parentNode === parent) node.remove();
}
function patch(target, source) {
  if (target.nodeType === 3 || target.nodeType === 8) {
    if (target.nodeValue !== source.nodeValue) target.nodeValue = source.nodeValue;
    return;
  }
  if (target.nodeType !== 1) return;
  // busy() retains the actual child nodes, so a snapshot must not detach them.
  if (target.getAttribute('aria-busy') === 'true') return;
  const active = target.ownerDocument.activeElement === target;
  const input = target.tagName === 'INPUT',
    textarea = target.tagName === 'TEXTAREA',
    select = target.tagName === 'SELECT';
  const checked = input && ['checkbox', 'radio'].includes(target.type);
  const dirty = checked
    ? target.checked !== target.defaultChecked
    : input || textarea
      ? target.value !== target.defaultValue
      : select &&
        [...target.options].some(
          (option, index) =>
            option.selected !==
            (option.defaultSelected ||
              (![...target.options].some((item) => item.defaultSelected) && index === 0)),
        );
  const preserve = (active || dirty) && (input || textarea || select);
  const value = preserve ? target.value : null,
    check = preserve && checked ? target.checked : null;
  const selected =
    preserve && select
      ? [...target.options].filter((option) => option.selected).map((option) => option.value)
      : null;
  for (const attribute of [...target.attributes]) {
    if (attribute.name === 'open' && target.tagName === 'DETAILS') continue;
    if (attribute.name === 'id' && generatedId.test(attribute.value) && !source.id) continue;
    if (!source.hasAttribute(attribute.name)) target.removeAttribute(attribute.name);
  }
  for (const attribute of [...source.attributes]) {
    if (attribute.name === 'open' && target.tagName === 'DETAILS') continue;
    if (target.getAttribute(attribute.name) !== attribute.value)
      target.setAttribute(attribute.name, attribute.value);
  }
  reconcile(target, source);
  if (input) {
    if (checked) target.checked = preserve ? check : source.checked;
    else target.value = preserve ? value : source.value;
  }
  if (textarea) target.value = preserve ? value : source.value;
  if (select) {
    for (const option of target.options)
      option.selected = preserve
        ? selected.includes(option.value)
        : [...source.options].some((item) => item.value === option.value && item.selected);
  }
}
export function replacePage(root, html) {
  const template = root.ownerDocument.createElement('template');
  template.innerHTML = html;
  reconcile(root, template.content);
}
