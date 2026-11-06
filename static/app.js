const $ = id => document.getElementById(id);
const DESKTOP = document.body.dataset.desktop === '1';

let images = [];
let currentId = null;
let pos = 'br';
let previewToken = 0;
let pollTimer = null;
let iconPath = '';
let signPath = '';
let hasFFmpeg = false;
let hasGPU = false;

/* ------------------------------------------------------------------ utils */

function toast(msg, isError) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('err', !!isError);
  t.hidden = false;
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.hidden = true; }, 3600);
}

async function api(url, body) {
  const opt = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(url, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
  return data;
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function fmtDur(sec) {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

/* ------------------------------------------------------------------- tema */

function setTheme(name) {
  document.documentElement.dataset.theme = name;
  try { localStorage.setItem('wm-theme-v2', name); } catch (e) {}
}
setTheme((() => { try { return localStorage.getItem('wm-theme-v2') || 'light'; } catch (e) { return 'light'; } })());
$('btnTheme').onclick = () =>
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

/* -------------------------------------------------------------- ajustes */

function settings() {
  return {
    text: $('text').value,
    font_path: $('font').value,
    align: document.querySelector('input[name=align]:checked').value,
    pos,
    tile: $('tile').checked,
    tile_gap: +$('tile_gap').value,
    mark_mode: document.querySelector('input[name=mark_mode]:checked').value,
    sign_path: signPath,
    icon_on: $('icon_on').checked,
    icon_path: iconPath,
    icon_pos: document.querySelector('input[name=icon_pos]:checked').value,
    icon_gap: +$('icon_gap').value,
    size_pct: +$('size_pct').value,
    size_px: +$('size_px').value,
    size_base: $('size_base').value,
    margin_x_pct: +$('margin_x_pct').value,
    margin_y_pct: +$('margin_y_pct').value,
    margin_x_px: +$('margin_x_px').value,
    margin_y_px: +$('margin_y_px').value,
    angle: +$('angle').value,
    color: $('color').value,
    opacity: +$('opacity').value,
    line_spacing: +$('line_spacing').value,
    outline_on: $('outline_on').checked,
    outline_width: +$('outline_width').value,
    outline_color: $('outline_color').value,
    outline_opacity: +$('outline_opacity').value,
    shadow_on: $('shadow_on').checked,
    shadow_dx: +$('shadow_dx').value,
    shadow_dy: +$('shadow_dy').value,
    shadow_blur: +$('shadow_blur').value,
    shadow_color: $('shadow_color').value,
    shadow_opacity: +$('shadow_opacity').value,
    keep_exif: $('keep_exif').checked,
  };
}

const FIELDS = ['vmode', 'crf', 'vpreset', 'audio_copy', 'gpu', 'pack', 'text', 'tile', 'tile_gap', 'size_pct', 'size_px', 'size_base',
  'margin_x_pct', 'margin_y_pct', 'margin_x_px', 'margin_y_px', 'margin_link',
  'angle', 'color', 'opacity', 'line_spacing', 'icon_on', 'icon_gap',
  'outline_on', 'outline_width', 'outline_color', 'outline_opacity',
  'shadow_on', 'shadow_dx', 'shadow_dy', 'shadow_blur', 'shadow_color',
  'shadow_opacity', 'keep_exif', 'format', 'quality', 'suffix'];

/* lo que se guarda en un preset: todos los ajustes + la fuente y las rutas */
function fullConfig() {
  return {
    ...settings(), font_path: $('font').value, format: $('format').value,
    quality: +$('quality').value, suffix: $('suffix').value,
    margin_link: $('margin_link').checked,
  };
}

function saveSettings() {
  try {
    localStorage.setItem('wm-settings-v2', JSON.stringify({
      ...settings(), format: $('format').value,
      quality: +$('quality').value, suffix: $('suffix').value,
    }));
  } catch (e) {}
}

function restoreSettings() {
  let s;
  try { s = JSON.parse(localStorage.getItem('wm-settings-v2') || 'null'); } catch (e) {}
  if (!s) return;
  const set = (id, v) => {
    const el = $(id);
    if (!el || v === undefined || v === null) return;
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  };
  FIELDS.forEach(k => set(k, s[k]));
  iconPath = s.icon_path || '';
  signPath = s.sign_path || '';
  showAsset('icon', iconPath);
  showAsset('sign', signPath);
  ['mark_mode', 'icon_pos'].forEach(g => {
    if (!s[g]) return;
    const r = document.querySelector(`input[name=${g}][value="${s[g]}"]`);
    if (r) r.checked = true;
  });
  if (s.pos) setPos(s.pos);
  if (s.align) {
    const r = document.querySelector(`input[name=align][value="${s.align}"]`);
    if (r) r.checked = true;
  }
  if (s.outline_on) $('foldOutline').open = true;
  if (s.shadow_on) $('foldShadow').open = true;
  return s.font_path;
}

/* ------------------------------------------------------------------ lista */

function renderList() {
  const ul = $('filelist');
  ul.innerHTML = '';
  $('listEmpty').hidden = images.length > 0;
  images.forEach(im => {
    const li = document.createElement('li');
    li.className = im.id === currentId ? 'on' : '';
    li.onclick = () => select(im.id);

    const wrap = document.createElement('div');
    wrap.className = 'thumbwrap';
    const img = new Image();
    img.src = `/api/thumb/${im.id}`;
    img.alt = '';
    img.loading = 'lazy';
    wrap.append(img);
    if (im.kind === 'video') {
      const b = document.createElement('span');
      b.className = 'badge';
      b.textContent = 'VID';
      wrap.append(b);
    }

    const extra = im.kind === 'video' ? ` · ${fmtDur(im.duration)}` : '';
    const meta = document.createElement('div');
    meta.className = 'fmeta';
    meta.innerHTML = `<span class="fname"></span><span class="fdim">${im.w}×${im.h}${extra} · ${fmtBytes(im.bytes)}</span>`;
    meta.querySelector('.fname').textContent = im.name;
    meta.title = im.path;

    const del = document.createElement('button');
    del.className = 'fdel';
    del.textContent = '×';
    del.title = 'Quitar de la lista';
    del.onclick = e => { e.stopPropagation(); remove(im.id); };

    li.append(wrap, meta, del);
    ul.append(li);
  });

  const nv = images.filter(x => x.kind === 'video').length;
  const ni = images.length - nv;
  const parts = [];
  if (ni) parts.push(`${ni} ${ni === 1 ? 'imagen' : 'imágenes'}`);
  if (nv) parts.push(`${nv} ${nv === 1 ? 'vídeo' : 'vídeos'}`);
  $('counter').textContent = parts.join(' · ') || 'Sin archivos';
  $('grpVideo').hidden = nv === 0;
  const i = images.findIndex(x => x.id === currentId);
  $('viewerPos').textContent = `${i < 0 ? 0 : i + 1} / ${images.length}`;
  $('btnProcess').disabled = images.length === 0;
}

function setImages(list) {
  images = list;
  if (!images.some(x => x.id === currentId)) currentId = images[0]?.id || null;
  renderList();
  refreshPreview();
}

async function remove(id) {
  setImages((await api('/api/remove', { ids: [id] })).images);
}

function select(id) {
  currentId = id;
  renderList();
  refreshPreview();
}

function step(d) {
  if (!images.length) return;
  let i = images.findIndex(x => x.id === currentId);
  i = (i + d + images.length) % images.length;
  select(images[i].id);
  document.querySelectorAll('.filelist li')[i]
    ?.scrollIntoView({ block: 'nearest' });
}

/* ------------------------------------------------------------------- zoom */

let zoom = 'fit';
const ZOOMS = [0.1, 0.15, 0.25, 0.33, 0.5, 0.66, 1, 1.5, 2, 3, 4, 6, 8];

function applyZoom() {
  const img = $('preview');
  const stage = $('stage');
  if (zoom === 'fit') {
    stage.classList.add('fit');
    img.style.width = '';
    img.style.height = '';
    $('btnZoomFit').textContent = 'Ajustar';
    return;
  }
  stage.classList.remove('fit');
  /* la vista previa viene reducida del servidor: se escala sobre el tamaño
     real de la imagen para que el porcentaje mostrado sea el verdadero */
  const real = img.dataset.fullw ? +img.dataset.fullw : img.naturalWidth;
  img.style.width = Math.max(1, Math.round(real * zoom)) + 'px';
  img.style.height = 'auto';
  $('btnZoomFit').textContent = Math.round(zoom * 100) + '%';
}

function fittedZoom() {
  const img = $('preview');
  const stage = $('stage');
  if (!img.naturalWidth) return 1;
  const real = img.dataset.fullw ? +img.dataset.fullw : img.naturalWidth;
  const ratio = img.naturalHeight / img.naturalWidth;
  return Math.min((stage.clientWidth - 32) / real,
                  (stage.clientHeight - 32) / (real * ratio));
}

function stepZoom(dir) {
  const cur = zoom === 'fit' ? fittedZoom() : zoom;
  const next = dir > 0
    ? ZOOMS.find(z => z > cur + 0.001)
    : [...ZOOMS].reverse().find(z => z < cur - 0.001);
  zoom = next || cur;
  applyZoom();
}

$('btnZoomIn').onclick = () => stepZoom(1);
$('btnZoomOut').onclick = () => stepZoom(-1);
$('btnZoomFit').onclick = () => { zoom = zoom === 'fit' ? 1 : 'fit'; applyZoom(); };

$('stage').addEventListener('wheel', e => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  stepZoom(e.deltaY < 0 ? 1 : -1);
}, { passive: false });

window.addEventListener('resize', () => { if (zoom === 'fit') applyZoom(); });

$('btnPrev').onclick = () => step(-1);
$('btnNext').onclick = () => step(1);
$('btnClear').onclick = async () => setImages((await api('/api/clear', {})).images);

document.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  if (e.key === 'ArrowLeft') step(-1);
  if (e.key === 'ArrowRight') step(1);
  if (e.key === 'Delete' && currentId) remove(currentId);
});

/* ------------------------------------------------------------ vista previa */

async function refreshPreview() {
  const img = $('preview');
  if (!currentId) {
    img.hidden = true;
    $('stageEmpty').hidden = false;
    $('metaName').textContent = '—';
    $('metaSize').textContent = '';
    return;
  }
  const token = ++previewToken;
  $('spinner').hidden = false;
  try {
    const d = await api('/api/preview', { id: currentId, settings: settings() });
    if (token !== previewToken) return;
    img.dataset.fullw = d.w;
    img.src = d.image;
    img.hidden = false;
    applyZoom();
    $('stageEmpty').hidden = true;
    $('metaName').textContent = d.name;
    const cur = images.find(x => x.id === currentId);
    $('metaSize').textContent = `${d.w} × ${d.h} px` +
      (cur && cur.kind === 'video' ? ' · fotograma del vídeo' : '');
  } catch (e) {
    if (token === previewToken) toast(e.message, true);
  } finally {
    if (token === previewToken) $('spinner').hidden = true;
  }
}

const queuePreview = debounce(() => { saveSettings(); refreshPreview(); }, 220);

/* --------------------------------------------------------- enlace de UI */

function setPos(p) {
  pos = p;
  document.querySelectorAll('#pad button')
    .forEach(b => b.classList.toggle('on', b.dataset.pos === p));
}
document.querySelectorAll('#pad button').forEach(b => {
  b.onclick = () => { setPos(b.dataset.pos); queuePreview(); };
  b.title = 'Colocar aquí la marca de agua';
});

document.querySelectorAll('.slider').forEach(w => {
  const inp = w.querySelector('input[type=range]');
  const out = w.querySelector('output');
  const suffix = w.dataset.suffix || '';
  const sync = () => {
    const step = +inp.step;
    out.textContent = (step < 1 ? (+inp.value).toFixed(1) : inp.value) + suffix;
  };
  inp.addEventListener('input', () => { sync(); queuePreview(); });
  inp._sync = sync;
  sync();
});

['text', 'font', 'size_base', 'size_px', 'margin_x_px', 'margin_y_px', 'color',
 'outline_color', 'shadow_color', 'outline_on', 'shadow_on', 'tile', 'keep_exif',
 'icon_on']
  .forEach(id => $(id).addEventListener('input', queuePreview));
document.querySelectorAll('input[name=align], input[name=icon_pos]')
  .forEach(r => r.addEventListener('change', queuePreview));
['format', 'quality', 'suffix'].forEach(id => $(id).addEventListener('input', saveSettings));

$('outline_on').addEventListener('change', e => { if (e.target.checked) $('foldOutline').open = true; });
$('shadow_on').addEventListener('change', e => { if (e.target.checked) $('foldShadow').open = true; });
function syncSizeMode() {
  const px = $('size_base').value === 'px';
  const tiled = $('tile').checked;
  $('wrapSizePct').hidden = px;
  $('wrapSizePx').hidden = !px;
  $('wrapGap').hidden = !tiled;
  $('wrapMargin').hidden = tiled || px;
  $('wrapMarginPx').hidden = tiled || !px;
  $('size_px').previousElementSibling.textContent =
    document.querySelector('input[name=mark_mode]:checked').value === 'image'
      ? 'Alto firma px' : 'Alto texto px';
}
$('tile').addEventListener('change', syncSizeMode);
$('size_base').addEventListener('change', syncSizeMode);

function syncPack() {
  const zip = $('pack').checked;
  $('packLabel').textContent = zip ? 'ZIP' : 'Sueltos';
  $('packWrap').title = zip
    ? 'Todo en un solo archivo ZIP sin comprimir'
    : 'Cada archivo por separado';
  $('btnProcess').textContent = zip ? 'Procesar y crear ZIP' : 'Procesar archivos';
  saveSettings();
}
$('pack').addEventListener('change', syncPack);

$('vmode').addEventListener('change', () => {
  const crf = $('vmode').value === 'crf';
  $('wrapCrf').hidden = !crf;
  $('noteSame').hidden = crf;
  saveSettings();
});


/* -------------------------------------------------- iconos, firmas, presets */

function showAsset(kind, path) {
  const thumb = $(kind + 'Thumb');
  const name = $(kind + 'Name');
  if (!path) {
    thumb.hidden = true;
    thumb.removeAttribute('src');
    name.textContent = kind === 'icon' ? 'Ninguno seleccionado' : 'Ninguna seleccionada';
    return;
  }
  thumb.src = '/api/asset-thumb?path=' + encodeURIComponent(path) + '&t=' + Date.now();
  thumb.hidden = false;
  name.textContent = path.split(/[\\/]/).pop();
  name.title = path;
}

async function pickAsset(kind) {
  const nat = await nativeApi();
  if (nat) {
    const r = await nat.pick_asset();
    if (!r) return;
    if (r.error) { toast(r.error, true); return; }
    return applyAsset(kind, r.path);
  }
  /* modo navegador: se sube al servidor y se usa la copia */
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = 'image/*';
  inp.onchange = async () => {
    const f = inp.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    try {
      const r = await fetch('/api/asset-upload', { method: 'POST', body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'Error al subir');
      applyAsset(kind, d.path);
    } catch (e) { toast(e.message, true); }
  };
  inp.click();
}

function applyAsset(kind, path) {
  if (kind === 'icon') { iconPath = path; $('icon_on').checked = true; $('grpIcon').open = true; }
  else signPath = path;
  showAsset(kind, path);
  queuePreview();
}

$('btnIconPick').onclick = () => pickAsset('icon');
$('btnSignPick').onclick = () => pickAsset('sign');
$('btnIconClear').onclick = () => { iconPath = ''; showAsset('icon', ''); queuePreview(); };
$('btnSignClear').onclick = () => { signPath = ''; showAsset('sign', ''); queuePreview(); };

/* tipo de marca: texto o firma */
function syncMarkMode() {
  const img = document.querySelector('input[name=mark_mode]:checked').value === 'image';
  $('grpSign').hidden = !img;
  $('grpText').hidden = img;
  $('grpIcon').hidden = img;
}
document.querySelectorAll('input[name=mark_mode]')
  .forEach(r => r.addEventListener('change', () => { syncMarkMode(); queuePreview(); }));

/* margenes enlazados */
function linkMargins(from) {
  if (!$('margin_link').checked) return;
  const other = from === 'x' ? 'margin_y_pct' : 'margin_x_pct';
  const src = from === 'x' ? 'margin_x_pct' : 'margin_y_pct';
  $(other).value = $(src).value;
  const w = $(other).closest('.slider');
  if (w) w.querySelector('output').textContent = (+$(other).value).toFixed(1) + '%';
}
$('margin_x_pct').addEventListener('input', () => linkMargins('x'));
$('margin_y_pct').addEventListener('input', () => linkMargins('y'));
$('margin_link').addEventListener('change', () => {
  if ($('margin_link').checked) { linkMargins('x'); queuePreview(); }
  saveSettings();
});

/* ------------------------------------------------------------------ presets */

async function loadPresetNames(selected) {
  const names = await api('/api/presets');
  const sel = $('preset');
  sel.innerHTML = '<option value="">— sin preset —</option>';
  names.forEach(n => {
    const o = document.createElement('option');
    o.value = o.textContent = n;
    sel.append(o);
  });
  if (selected && names.includes(selected)) sel.value = selected;
}

$('btnPresetSave').onclick = async () => {
  const suggested = $('preset').value || '';
  const name = prompt('Nombre del preset:', suggested);
  if (!name || !name.trim()) return;
  try {
    await api('/api/presets/save', { name: name.trim(), data: fullConfig() });
    await loadPresetNames(name.trim());
    toast(`Preset «${name.trim()}» guardado`);
  } catch (e) { toast(e.message, true); }
};

$('btnPresetLoad').onclick = async () => {
  const name = $('preset').value;
  if (!name) { toast('Elige un preset de la lista.', true); return; }
  try {
    const d = await api('/api/presets/load', { name });
    applyConfig(d.data);
    toast(`Preset «${name}» cargado`);
  } catch (e) { toast(e.message, true); }
};

$('btnPresetDel').onclick = async () => {
  const name = $('preset').value;
  if (!name) return;
  if (!confirm(`¿Borrar el preset «${name}»?`)) return;
  await api('/api/presets/delete', { name });
  await loadPresetNames();
  toast('Preset borrado');
};

$('preset').addEventListener('change', () => { if ($('preset').value) $('btnPresetLoad').click(); });

/* aplica una configuración completa a la interfaz */
function applyConfig(s) {
  if (!s) return;
  const set = (id, v) => {
    const el = $(id);
    if (!el || v === undefined || v === null) return;
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  };
  FIELDS.forEach(k => set(k, s[k]));
  if (s.font_path) {
    const opt = [...$('font').options].find(o => o.value === s.font_path);
    if (opt) $('font').value = s.font_path;
    else toast('La fuente del preset no está en este equipo; se mantiene la actual.', true);
  }
  ['align', 'mark_mode', 'icon_pos'].forEach(g => {
    if (!s[g]) return;
    const r = document.querySelector(`input[name=${g}][value="${s[g]}"]`);
    if (r) r.checked = true;
  });
  if (s.pos) setPos(s.pos);
  iconPath = s.icon_path || '';
  signPath = s.sign_path || '';
  showAsset('icon', iconPath);
  showAsset('sign', signPath);
  if (s.outline_on) $('foldOutline').open = true;
  if (s.shadow_on) $('foldShadow').open = true;
  if (s.icon_on) $('grpIcon').open = true;
  document.querySelectorAll('.slider input[type=range]').forEach(i => i._sync && i._sync());
  syncSizeMode();
  syncMarkMode();
  saveSettings();
  refreshPreview();
}

/* ---------------------------------------------------------------- fuentes */

async function loadFonts(preferred) {
  const sel = $('font');
  let list = [];
  try {
    list = await api('/api/fonts');
  } catch (e) {
    sel.innerHTML = '<option value="">Error al leer las fuentes</option>';
    toast('No se pudieron leer las fuentes del sistema. Revisa la terminal.', true);
    return;
  }
  sel.innerHTML = '';
  if (!list.length) {
    sel.innerHTML = '<option value="">No se encontraron fuentes — usa el botón +</option>';
    toast('No se encontró ninguna fuente. Agrega un .ttf con el botón «+».', true);
    return;
  }
  list.forEach(f => {
    const o = document.createElement('option');
    o.value = f.path;
    o.textContent = f.label;
    sel.append(o);
  });
  const pick = preferred && list.some(f => f.path === preferred)
    ? preferred
    : (list.find(f => /^(Arial|Segoe UI|DejaVu Sans|Helvetica|Inter)$/i.test(f.family)
        && /regular|book/i.test(f.style)) || list[0]).path;
  sel.value = pick;
}

$('btnAddFont').onclick = async () => {
  const nat = await nativeApi();
  if (!nat) {
    toast('Copia el archivo .ttf en una carpeta «fonts» junto a app.py y reinicia.', true);
    return;
  }
  const f = await nat.pick_font();
  if (!f) return;
  await loadFonts(f.path);
  queuePreview();
  toast(`Fuente añadida: ${f.label}`);
};

/* ------------------------------------------------------ agregar imágenes */

/* pywebview inyecta su API de forma asíncrona: hay que esperarla */
let apiReady = null;
function nativeApi() {
  if (!DESKTOP) return Promise.resolve(null);
  if (apiReady) return apiReady;
  apiReady = new Promise(resolve => {
    if (window.pywebview && window.pywebview.api) return resolve(window.pywebview.api);
    window.addEventListener('pywebviewready', () => resolve(window.pywebview.api), { once: true });
    setTimeout(() => resolve(window.pywebview && window.pywebview.api ? window.pywebview.api : null), 4000);
  });
  return apiReady;
}

async function addPaths(paths) {
  if (!paths || !paths.length) return;
  const d = await api('/api/add-paths', { paths });
  setImages(d.images);
  toast(d.added ? `${d.added} ${d.added === 1 ? 'archivo agregado' : 'archivos agregados'}`
                : 'No se encontraron archivos nuevos en esa ruta.', !d.added);
}

/* extensiones aceptadas: deben coincidir con IMAGE_EXTS | VIDEO_EXTS del backend */
const OK_EXT = /\.(jpe?g|png|webp|bmp|tiff?|mp4|mov|mkv|avi|webm|m4v|mpe?g|wmv|flv|3gp|ts)$/i;

async function uploadFiles(fileList) {
  const all = [...fileList];
  const files = all.filter(f => /^(image|video)\//.test(f.type) || OK_EXT.test(f.name));
  if (!files.length) {
    toast('Ninguno de esos archivos es una imagen o un vídeo.', true);
    return;
  }
  const skipped = all.length - files.length;
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  toast(`Cargando ${files.length}…`);
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const d = await r.json();
    setImages(d.images);
    toast(`${d.added} ${d.added === 1 ? 'archivo agregado' : 'archivos agregados'}` +
          (skipped ? ` · ${skipped} omitido${skipped === 1 ? '' : 's'}` : ''));
  } catch (e) {
    toast('No se pudieron cargar los archivos.', true);
  }
}

async function typePath() {
  const raw = prompt('Pega la ruta de la carpeta o del archivo:\n\nC:\\Users\\gabo\\Pictures\\boda');
  if (!raw) return;
  try {
    const d = await api('/api/add-typed', { path: raw });
    setImages(d.images);
    toast(`${d.added} ${d.added === 1 ? 'archivo agregado' : 'archivos agregados'}`);
  } catch (e) { toast(e.message, true); }
}

$('btnFolder').onclick = async () => {
  const nat = await nativeApi();
  if (nat) {
    try { return await addPaths(await nat.pick_folder()); }
    catch (e) { toast('El diálogo del sistema falló: ' + e, true); }
  }
  typePath();
};

$('btnFiles').onclick = async () => {
  const nat = await nativeApi();
  if (nat) {
    try { return await addPaths(await nat.pick_images()); }
    catch (e) { toast('El diálogo del sistema falló: ' + e, true); }
  }
  $('filePicker').click();
};

$('btnTyped').onclick = typePath;
$('filePicker').onchange = e => { uploadFiles(e.target.files); e.target.value = ''; };

/* arrastrar y soltar — capture:true evita que el visor abra el archivo */
['dragenter', 'dragover'].forEach(ev =>
  document.addEventListener(ev, e => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  }, true));

document.addEventListener('drop', async e => {
  e.preventDefault();
  e.stopPropagation();
  const files = e.dataTransfer ? e.dataTransfer.files : null;
  if (!files || !files.length) return;
  /* en Windows el navegador incrustado no expone la ruta real del archivo,
     así que se suben por copia a la carpeta temporal */
  const paths = [...files].map(f => f.path).filter(Boolean);
  if (paths.length === files.length) return addPaths(paths);
  uploadFiles(files);
}, true);

/* ----------------------------------------------------------- procesamiento */

$('btnProcess').onclick = async () => {
  const mode = document.querySelector('input[name=mark_mode]:checked').value;
  if (mode === 'image' && !signPath) { toast('Elige la imagen de firma.', true); return; }
  if (mode === 'text' && !$('text').value.trim() && !(iconPath && $('icon_on').checked)) {
    toast('Escribe el texto de la marca de agua.', true); return;
  }
  $('btnDownload').hidden = true;
  $('btnSaveFiles').hidden = true;
  $('btnProcess').disabled = true;
  $('btnCancel').hidden = false;
  $('progress').hidden = false;
  $('progFill').style.width = '0%';
  try {
    await api('/api/process', {
      settings: settings(), format: $('format').value,
      quality: +$('quality').value, suffix: $('suffix').value,
      pack: $('pack').checked,
      video: {
        vmode: $('vmode').value, crf: +$('crf').value,
        preset: $('vpreset').value, audio_copy: $('audio_copy').checked,
        gpu: hasGPU && $('gpu').checked,
      },
    });
    pollTimer = setInterval(poll, 350);
  } catch (e) {
    toast(e.message, true);
    $('btnProcess').disabled = false;
    $('btnCancel').hidden = true;
    $('progress').hidden = true;
  }
};

$('btnCancel').onclick = () => api('/api/cancel', {});

async function poll() {
  const p = await api('/api/progress');
  const frac = p.total ? (p.done + (p.sub || 0)) / p.total : 0;
  $('progFill').style.width = Math.round(frac * 100) + '%';
  const sub = p.sub > 0 ? ` (${Math.round(p.sub * 100)} %)` : '';
  $('progText').textContent = `${p.done} / ${p.total}${p.current ? ' · ' + p.current + sub : ''}`;
  if (p.running) return;

  clearInterval(pollTimer);
  $('btnProcess').disabled = false;
  $('btnCancel').hidden = true;
  $('progress').hidden = true;

  if (p.error) { toast(p.error, true); return; }
  if (p.failed && p.failed.length) toast(`${p.failed.length} archivo(s) fallaron. Revisa la consola.`, true);
  if (p.failed) p.failed.forEach(f => console.warn(f));
  if (p.notes && p.notes.length) {
    p.notes.forEach(n => console.info(n));
    toast(p.notes[0], true);
  }
  if (!p.token) return;

  if (p.mode === 'files') return deliverFiles(p);
  deliverZip(p);
}

async function deliverZip(p) {
  const nat = await nativeApi();
  if (nat) {
    const r = await nat.save_zip(p.token, p.filename);
    if (r.ok) toast(`ZIP guardado en ${r.path}`);
    else if (!r.cancelled) toast(r.error || 'No se pudo guardar el ZIP.', true);
    return;
  }
  const a = $('btnDownload');
  a.href = `/api/download/${p.token}`;
  a.setAttribute('download', p.filename);
  a.textContent = 'Descargar ZIP';
  a.title = p.filename;
  a.hidden = false;
}

async function deliverFiles(p) {
  const nat = await nativeApi();
  if (nat) {
    const btn = $('btnSaveFiles');
    btn.textContent = `Guardar ${p.count} archivo${p.count === 1 ? '' : 's'}`;
    btn.hidden = false;
    btn.onclick = async () => {
      const r = await nat.save_files(p.token);
      if (r.cancelled) return;
      if (!r.ok) { toast(r.error || 'No se pudieron guardar.', true); return; }
      toast(`${r.copied} archivo${r.copied === 1 ? '' : 's'} en ${r.path}`);
      if (r.errors && r.errors.length) r.errors.forEach(e => console.warn(e));
      btn.hidden = true;
    };
    btn.onclick();   // se abre el diálogo de carpeta directamente
    return;
  }
  /* navegador: se descarga cada archivo por separado, con pausa entre
     ellos para que el navegador no bloquee la tanda */
  try {
    const files = await api(`/api/files/${p.token}`);
    toast(`Descargando ${files.length} archivo${files.length === 1 ? '' : 's'}…`);
    for (const f of files) {
      const a = document.createElement('a');
      a.href = f.url;
      a.download = f.name;
      document.body.append(a);
      a.click();
      a.remove();
      await new Promise(r => setTimeout(r, 450));
    }
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- arranque */

(async function init() {
  const preferredFont = restoreSettings();
  document.querySelectorAll('.slider input[type=range]').forEach(i => i._sync && i._sync());
  syncSizeMode();
  syncMarkMode();
  try { await loadFonts(preferredFont); } catch (e) { toast('No se pudieron leer las fuentes.', true); }
  try { await loadPresetNames(); } catch (e) {}
  try {
    const env = await api('/api/env');
    hasFFmpeg = !!env.ffmpeg;
    $('ffmpegWarn').hidden = hasFFmpeg;
    hasGPU = !!env.gpu;
    $('gpuWrap').hidden = !hasGPU;
    if (hasGPU) {
      $('gpuLabel').textContent = 'GPU';
      $('gpuWrap').title = `Acelerar con ${env.gpu_label} (${env.gpu_codecs.join(', ')})`;
    }
  } catch (e) {}
  syncPack();
  $('wrapCrf').hidden = $('vmode').value !== 'crf';
  $('noteSame').hidden = $('vmode').value === 'crf';
  renderList();
})();

$('btnFfmpegHelp').onclick = () => {
  const win = navigator.platform.toLowerCase().includes('win');
  const mac = navigator.platform.toLowerCase().includes('mac');
  alert(
    'Para procesar vídeos hace falta FFmpeg:\n\n' +
    (win ? 'Windows (PowerShell):\n  winget install Gyan.FFmpeg\n\n'
         : mac ? 'macOS:\n  brew install ffmpeg\n\n'
               : 'Linux:\n  sudo apt install ffmpeg\n\n') +
    'También puedes descargarlo de ffmpeg.org y dejar ffmpeg y ffprobe\n' +
    'en una carpeta llamada «bin» junto a app.py.\n\n' +
    'Después reinicia la aplicación.');
};
