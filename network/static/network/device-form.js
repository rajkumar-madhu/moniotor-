/* Add / edit device: suggest product image URLs from NetworkProductImage rows (injected as JSON). */
(function () {
  const root = document.querySelector('.nw-form');
  const script = document.getElementById('nw-product-catalog');
  const raw = script ? script.textContent.trim() : '';
  if (!raw) return;
  let catalog;
  try {
    catalog = JSON.parse(raw);
  } catch (e) {
    return;
  }

  const typeEl = root.querySelector('#id_device_type');
  const vendorEl = root.querySelector('#id_vendor');
  const modelEl = root.querySelector('#id_model');
  const imgEl = root.querySelector('#id_product_image_url');
  const pageEl = root.querySelector('#id_product_page_url');
  const preview = root.querySelector('[data-prod-preview]');
  if (!typeEl || !vendorEl || !modelEl || !imgEl || !pageEl) return;

  function score(row, v, m, dt) {
    if (!v || row.vendor.toLowerCase() !== v.toLowerCase()) return -1;
    const rdt = row.device_type || '';
    let typeScore = 0;
    if (rdt) {
      if (rdt === dt) typeScore = 2;
      else return -1;
    }
    const key = (row.model_key || '').trim();
    if (!key) return typeScore;
    const ml = m.toLowerCase();
    const kl = key.toLowerCase();
    if (!ml) return typeScore + 0.1;
    if (ml === kl) return typeScore + 4;
    if (ml.startsWith(kl)) return typeScore + 3;
    if (ml.includes(kl)) return typeScore + 2;
    return -1;
  }

  function resolve() {
    const v = (vendorEl.value || '').trim();
    const m = (modelEl.value || '').trim();
    const dt = typeEl.value || '';
    if (!v) return null;
    let best = null;
    let bestScore = -1;
    catalog.forEach(function (row) {
      const s = score(row, v, m, dt);
      if (s > bestScore) {
        bestScore = s;
        best = row;
      }
    });
    return bestScore >= 0 ? best : null;
  }

  function applyPreview(url) {
    if (!preview) return;
    preview.innerHTML = '';
    if (!url) return;
    const img = document.createElement('img');
    img.src = url;
    img.alt = 'Catalog preview';
    preview.appendChild(img);
  }

  function sync() {
    const row = resolve();
    if (!row) {
      applyPreview((imgEl.value || '').trim());
      return;
    }
    const curImg = (imgEl.value || '').trim();
    const curPage = (pageEl.value || '').trim();
    if (!curImg) imgEl.value = row.product_image_url || '';
    if (!curPage) pageEl.value = row.product_page_url || '';
    applyPreview((imgEl.value || '').trim());
  }

  ['input', 'change'].forEach(function (ev) {
    typeEl.addEventListener(ev, sync);
    vendorEl.addEventListener(ev, sync);
    modelEl.addEventListener(ev, sync);
  });
  [imgEl, pageEl].forEach(function (el) {
    el.addEventListener('input', function () { applyPreview((imgEl.value || '').trim()); });
  });
  sync();
})();
