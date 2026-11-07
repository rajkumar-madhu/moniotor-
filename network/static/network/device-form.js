/* Add / edit device: the photo section.
 *  - previews a file you pick (a local blob: URL, nothing is uploaded until you save);
 *  - suggests the product-catalog photo for the vendor / model / type you typed: it selects that catalog row (shown from
 *    LinkedEye's own copy, never from a vendor site) or, for an older row that only has a link, fills the "fetch" address
 *    so saving downloads and stores it once;
 *  - keeps the three ways to set a photo exclusive (the server refuses more than one).
 * Everything written into the page is a DOM property or textContent. */
(function () {
  'use strict';
  const root = document.querySelector('.nw-form');
  if (!root) return;
  const script = document.getElementById('nw-product-catalog');
  let catalog = [];
  try { catalog = script ? JSON.parse(script.textContent.trim() || '[]') : []; } catch (e) { catalog = []; }

  const typeEl = root.querySelector('#id_device_type');
  const vendorEl = root.querySelector('#id_vendor');
  const modelEl = root.querySelector('#id_model');
  const pageEl = root.querySelector('#id_product_page_url');
  const fileEl = root.querySelector('#id_image_file');
  const urlEl = root.querySelector('#id_image_url');
  const rowEl = root.querySelector('#id_catalog_row');
  const clearEl = root.querySelector('#id_clear_image');
  const preview = root.querySelector('[data-prod-preview]');
  let blobUrl = null, autoRow = false, autoUrl = false;

  function show(src, label) {
    if (!preview) return;
    preview.replaceChildren();
    if (blobUrl && src !== blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
    if (!src) return;
    const img = document.createElement('img');
    img.src = src;
    img.alt = label;
    preview.appendChild(img);
  }

  function score(row, v, m, dt) {
    if (!v || row.vendor.toLowerCase() !== v.toLowerCase()) return -1;
    const rdt = row.device_type || '';
    let typeScore = 0;
    if (rdt) { if (rdt === dt) typeScore = 2; else return -1; }
    const key = (row.model_key || '').trim();
    if (!key) return typeScore;
    const ml = m.toLowerCase(), kl = key.toLowerCase();
    if (!ml) return typeScore + 0.1;
    if (ml === kl) return typeScore + 4;
    if (ml.startsWith(kl)) return typeScore + 3;
    if (ml.includes(kl)) return typeScore + 2;
    return -1;
  }
  function resolve() {
    if (!vendorEl || !modelEl || !typeEl) return null;
    const v = (vendorEl.value || '').trim(), m = (modelEl.value || '').trim(), dt = typeEl.value || '';
    let best = null, bestScore = -1;
    catalog.forEach(function (row) {
      const s = score(row, v, m, dt);
      if (s > bestScore) { bestScore = s; best = row; }
    });
    return bestScore >= 0 ? best : null;
  }

  function userChose() {
    return (fileEl && fileEl.files && fileEl.files.length) || (urlEl && urlEl.value.trim() && !autoUrl) || (rowEl && rowEl.value && !autoRow);
  }

  function suggest() {
    if (root.dataset.hasPhoto || userChose()) return;
    const row = resolve();
    if (rowEl && autoRow) { rowEl.value = ''; autoRow = false; }
    if (urlEl && autoUrl) { urlEl.value = ''; autoUrl = false; }
    show(null);
    if (!row) return;
    if (row.image_url && rowEl && Array.from(rowEl.options).some(function (o) { return o.value === String(row.id); })) {
      rowEl.value = String(row.id);
      autoRow = true;
      show(row.image_url, 'Catalog photo');
    } else if (row.product_image_url && urlEl) {
      urlEl.value = row.product_image_url;         // downloaded once, on save; the link itself is not kept
      autoUrl = true;
    }
    if (pageEl && !pageEl.value.trim() && row.product_page_url) pageEl.value = row.product_page_url;
  }

  if (fileEl) fileEl.addEventListener('change', function () {
    const f = fileEl.files && fileEl.files[0];
    if (!f) { suggest(); return; }
    if (urlEl) { urlEl.value = ''; autoUrl = false; }
    if (rowEl) { rowEl.value = ''; autoRow = false; }
    if (clearEl) clearEl.checked = false;
    if (preview) {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
      blobUrl = /^image\/(png|jpeg|webp)$/.test(f.type) ? URL.createObjectURL(f) : null;
      show(blobUrl, 'Selected photo');
    }
  });
  if (urlEl) urlEl.addEventListener('input', function () {
    autoUrl = false;
    if (urlEl.value.trim()) {
      if (fileEl) fileEl.value = '';
      if (rowEl) { rowEl.value = ''; autoRow = false; }
      show(null);
    } else suggest();
  });
  if (rowEl) rowEl.addEventListener('change', function () {
    autoRow = false;
    if (rowEl.value) {
      if (fileEl) fileEl.value = '';
      if (urlEl) { urlEl.value = ''; autoUrl = false; }
      const row = catalog.filter(function (r) { return String(r.id) === rowEl.value; })[0];
      show(row ? row.image_url : null, 'Catalog photo');
    } else show(null);
  });
  [typeEl, vendorEl, modelEl].forEach(function (el) {
    if (!el) return;
    ['input', 'change'].forEach(function (ev) { el.addEventListener(ev, suggest); });
  });
  if (!root.dataset.editing && !(rowEl && rowEl.value) && !(urlEl && urlEl.value)) suggest();
})();
