/**
 * topology-builder.js — Visual CLAB Topology Editor
 * Dépendances : Cytoscape.js (chargé en global)
 */
'use strict';

// ─── State ───────────────────────────────────────────────────────────────────

const AppState = {
  vms: [],
  families: [],
  selectedVm: null,
  dockerImages: [],   // images fetched from the selected agent VM
  nodes: [],        // { id, name, family, image, x, y }
  links: [],        // { id, src, srcIface, dst, dstIface }
  selectedElement: null,  // id of selected node or link
  nodeCounter: 0,
  linkCounter: 0,
  pendingLinkSrc: null,   // node id waiting for link target (set via right-click context menu)
  cy: null,
  lastYaml: '',
  importedNodeIds: new Set(),  // node ids that came from the last YAML import
  importedLinkIds: new Set(),  // link ids that came from the last YAML import
  passthroughLinks: [],        // raw link defs with external endpoints (e.g. "host:eth0") preserved for round-trip
};

// ─── Utils - API ─────────────────────────────────────────────────────────────

async function apiFetch(url, options = {}) {
  try {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      let msg;
      if (Array.isArray(data?.detail?.errors) && data.detail.errors.length) {
        msg = data.detail.errors.join('\n');
      } else if (Array.isArray(data?.detail?.validation?.errors) && data.detail.validation.errors.length) {
        msg = data.detail.validation.errors.join('\n');
      } else if (typeof data?.detail?.error === 'string' && data.detail.error) {
        msg = data.detail.error;
      } else if (typeof data?.detail === 'string' && data.detail) {
        msg = data.detail;
      } else if (data?.detail) {
        msg = JSON.stringify(data.detail);
      } else {
        msg = `HTTP ${resp.status}`;
      }
      const apiErr = new Error(msg);
      apiErr.responseData = data;
      throw apiErr;
    }
    return data;
  } catch (err) {
    if (err instanceof TypeError) throw new Error('Impossible de contacter le serveur.');
    throw err;
  }
}

// ─── Toast notifications ─────────────────────────────────────────────────────

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const colors = {
    info:    'bg-blue-900/90 border-blue-500 text-blue-200',
    success: 'bg-green-900/90 border-green-500 text-green-200',
    error:   'bg-red-900/90 border-red-500 text-red-200',
    warn:    'bg-yellow-900/90 border-yellow-500 text-yellow-200',
  };
  const toast = document.createElement('div');
  toast.className = `mb-2 px-4 py-3 rounded border text-sm max-w-sm shadow-lg transition-all duration-300 ${colors[type] || colors.info}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, 4000);
}
// ─── IP subnet validation ─────────────────────────────────────────────────────

function isIpInSubnet(ip, cidr) {
  try {
    const [netAddr, bits] = cidr.split('/');
    const prefixLen = parseInt(bits, 10);
    if (isNaN(prefixLen) || prefixLen < 0 || prefixLen > 32) return true;
    const mask   = prefixLen === 0 ? 0 : (~0 << (32 - prefixLen)) >>> 0;
    const ipInt  = ip.split('.').reduce((acc, oct) => (acc << 8) | parseInt(oct, 10), 0) >>> 0;
    const netInt = netAddr.split('.').reduce((acc, oct) => (acc << 8) | parseInt(oct, 10), 0) >>> 0;
    return (ipInt & mask) === (netInt & mask);
  } catch {
    return true; // parse error: skip validation
  }
}
// ─── Sidebar panels ──────────────────────────────────────────────────────────

function showPanel(id) {
  ['panel-vm', 'panel-node-editor', 'panel-link-editor', 'panel-info'].forEach(p => {
    const el = document.getElementById(p);
    if (el) el.classList.add('hidden');
  });
  const target = document.getElementById(id);
  if (target) target.classList.remove('hidden');
}

// ─── Metadata loading ────────────────────────────────────────────────────────

async function loadMetadata() {
  try {
    const data = await apiFetch('/api/topology-builder/metadata');
    AppState.vms = data.vms || [];
    AppState.families = data.families || [];
    populateVmSelector();
    showPanel('panel-vm');
  } catch (e) {
    showToast('Erreur chargement métadonnées : ' + e.message, 'error');
  }
}

function populateVmSelector() {
  const sel = document.getElementById('vm-select');
  sel.innerHTML = '<option value="">-- Sélectionner une VM --</option>';
  AppState.vms.forEach(vm => {
    const opt = document.createElement('option');
    opt.value = vm.id;
    opt.textContent = `${vm.name} (${vm.id}) — ${vm.management_subnet || 'subnet ?'}`;
    sel.appendChild(opt);
  });
}

function onVmSelect() {
  const vmId = document.getElementById('vm-select').value;
  AppState.selectedVm = AppState.vms.find(v => v.id === vmId) || null;
  AppState.dockerImages = [];

  const subnetInfo = document.getElementById('vm-subnet-info');
  if (AppState.selectedVm) {
    subnetInfo.textContent = `Subnet management : ${AppState.selectedVm.management_subnet || 'non défini'}`;
    subnetInfo.classList.remove('hidden');

    const imgStatusEl = document.getElementById('vm-images-status');
    const refreshBtn  = document.getElementById('btn-refresh-images');
    if (imgStatusEl) imgStatusEl.classList.remove('hidden');
    if (refreshBtn)  refreshBtn.classList.remove('hidden');

    // Auto-remplir le champ subnet si vide
    const subnetField = document.getElementById('field-mgmt-subnet');
    if (!subnetField.value && AppState.selectedVm.management_subnet) {
      subnetField.value = AppState.selectedVm.management_subnet;
    }

    fetchVmDockerImages(vmId);
  } else {
    subnetInfo.classList.add('hidden');
    document.getElementById('vm-images-status')?.classList.add('hidden');
    document.getElementById('btn-refresh-images')?.classList.add('hidden');
  }
  updateCreateSandboxButtonState();
}

async function fetchVmDockerImages(vmId, forceRefresh = false) {
  const imgStatusEl = document.getElementById('vm-images-status');
  if (imgStatusEl) {
    imgStatusEl.textContent = 'Chargement des images\u2026';
    imgStatusEl.classList.remove('hidden', 'text-green-400', 'text-red-400', 'text-slate-500');
    imgStatusEl.classList.add('text-slate-400');
  }
  try {
    const url = `/api/topology-builder/images/${encodeURIComponent(vmId)}${forceRefresh ? '?refresh=true' : ''}`;
    const data = await apiFetch(url);
    AppState.dockerImages = data.images || [];
    const source = data.source || 'cache';
    const count = AppState.dockerImages.length;

    if (imgStatusEl) {
      imgStatusEl.textContent = count > 0
        ? `${count} image${count > 1 ? 's' : ''} Docker (${source === 'live' ? 'actualis\u00e9' : 'cache'})`
        : 'Aucune image trouv\u00e9e sur cette VM.';
      imgStatusEl.classList.remove('text-slate-400', 'text-red-400');
      imgStatusEl.classList.add(count > 0 ? 'text-green-400' : 'text-slate-500');
    }

    // Refresh image select if a node is currently being edited
    const nodeId = document.getElementById('edit-node-id').value;
    if (nodeId) {
      const node = AppState.nodes.find(n => n.id === nodeId);
      if (node) populateImageSelect(node.family, node.image);
    }
  } catch (e) {
    AppState.dockerImages = [];
    if (imgStatusEl) {
      imgStatusEl.textContent = 'Impossible de charger les images.';
      imgStatusEl.classList.remove('text-slate-400', 'text-green-400');
      imgStatusEl.classList.add('text-red-400');
    }
  }
}

// ─── Family helpers ──────────────────────────────────────────────────────────

function imagesForFamily(familyId) {
  const fam = AppState.families.find(f => f.id === familyId);
  return fam ? fam.images : [];
}

function interfacesForFamily(familyId, count = 8) {
  const fam = AppState.families.find(f => f.id === familyId);
  if (!fam) return Array.from({ length: count }, (_, i) => `eth${i}`);
  const pattern = fam.interface_pattern;
  const max = Math.min(count, fam.max_interfaces);
  const start = fam.interface_start ?? 0;
  const result = [];
  for (let i = 0; i < max; i++) {
    let name = pattern;
    if (pattern.includes('{slot}') && pattern.includes('{n}')) {
      const slot = Math.floor(i / 4);
      const port = i % 4;
      name = pattern.replace('{slot}', slot).replace('{n}', port);
    } else {
      name = pattern.replace('{n}', i + start);
    }
    result.push(name);
  }
  return result;
}

// ─── Cytoscape initialization ─────────────────────────────────────────────────

function initCytoscape() {
  AppState.cy = cytoscape({
    container: document.getElementById('cy-canvas'),
    style: [
      {
        selector: 'node',
        style: {
          'width': 58,
          'height': 58,
          'shape': 'ellipse',
          'background-color': '#1f2937',
          'background-opacity': 1,
          'border-width': 1.5,
          'border-color': '#334155',
          'border-opacity': 0.7,
          'label': 'data(label)',
          'color': '#e2e8f0',
          'font-size': '11px',
          'text-valign': 'bottom',
          'text-margin-y': 6,
          'background-image': 'data(icon)',
          // background-fit:contain uses the full bounding box (node + label below) which
          // shifts the icon upward. Explicit size + position centers it inside the disc.
          'background-fit': 'none',
          'background-width': '74%',
          'background-height': '74%',
          'background-position-x': '50%',
          'background-position-y': '50%',
          'background-clip': 'node',
          'background-image-opacity': 1,
        },
      },
      // Keep router node fill neutral so family color comes from the icon itself.
      { selector: 'node[family="linux"]', style: { 'background-color': '#374151', 'color': '#d1d5db', 'shape': 'round-rectangle', 'width': 76, 'height': 56 } },
      {
        selector: 'node:selected',
        style: {
          'border-width': 2.5,
          'border-color': '#f8fafc',
          'border-opacity': 0.9,
          'overlay-opacity': 0,
        },
      },
      {
        selector: 'node.pending-link',
        style: {
          'border-width': 2.5,
          'border-color': '#facc15',
          'border-opacity': 1,
          'overlay-opacity': 0,
        },
      },
      {
        selector: 'edge',
        style: {
          'width': 2,
          'line-color': '#475569',
          'target-arrow-color': '#475569',
          'curve-style': 'bezier',
          'label': 'data(label)',
          'font-size': '9px',
          'color': '#94a3b8',
          'text-rotation': 'autorotate',
          'text-background-color': '#0f172a',
          'text-background-opacity': 0.8,
          'text-background-padding': '2px',
        },
      },
      {
        selector: 'edge:selected',
        style: {
          'line-color': '#60a5fa',
          'width': 3,
        },
      },
    ],
    layout: { name: 'preset' },
    userZoomingEnabled: true,
    userPanningEnabled: true,
  });

  // Click on canvas background → deselect
  AppState.cy.on('tap', evt => {
    if (evt.target === AppState.cy) {
      hideNodeContextMenu();
      clearPendingLink();
      deselectAll();
      showPanel('panel-info');
    }
  });

  // Click on node
  AppState.cy.on('tap', 'node', evt => {
    hideNodeContextMenu();
    const nodeId = evt.target.id();
    if (AppState.pendingLinkSrc !== null) {
      if (AppState.pendingLinkSrc !== nodeId) {
        finishLink(nodeId);
      } else {
        clearPendingLink();
        showToast('Sélectionnez un nœud différent comme destination.', 'warn');
      }
    } else {
      selectNode(nodeId);
    }
  });

  // Click on edge
  AppState.cy.on('tap', 'edge', evt => {
    hideNodeContextMenu();
    clearPendingLink();
    const linkId = evt.target.id();
    selectLink(linkId);
  });

  // Right-click on node → context menu
  AppState.cy.on('cxttap', 'node', evt => {
    const nodeId = evt.target.id();
    showNodeContextMenu(evt.renderedPosition, nodeId);
  });

  // Right-click on background → dismiss context menu
  AppState.cy.on('cxttap', evt => {
    if (evt.target === AppState.cy) hideNodeContextMenu();
  });
}

// ─── Node management ─────────────────────────────────────────────────────────

function addNode(familyId) {
  const fam = AppState.families.find(f => f.id === familyId);
  if (!fam) return;

  const id = `n${++AppState.nodeCounter}`;
  const images = imagesForFamily(familyId);
  const defaultImage = images[0]?.image || '';

  const node = {
    id,
    name: `${familyId.toUpperCase()}-${AppState.nodeCounter}`,
    family: familyId,
    image: defaultImage,
    mgmt_ip: '',
    original_kind: fam.clab_kind || familyId,  // use canonical ContainerLab kind
    extra_params: null,
  };
  AppState.nodes.push(node);

  const icon = iconForFamily(familyId);
  const pos = randomCanvasPos();
  AppState.cy.add({
    group: 'nodes',
    data: { id, label: node.name, family: familyId, icon },
    position: pos,
  });

  selectNode(id);
  updateYamlPreview();
}

// ─── Icon helpers ─────────────────────────────────────────────────────────────

/**
 * Encode an SVG string as a percent-encoded data URI.
 * More reliable than base64 in Cytoscape (avoids btoa multi-byte issues).
 */
function _svgToDataUri(svgString) {
  return 'data:image/svg+xml,' + encodeURIComponent(svgString);
}

// ── Vendor SVG icons ──────────────────────────────────────────────────────────
// All icons: 100×100 viewBox, white shapes on transparent bg.
// Thick strokes / large fills required — effective render size ≈ 40 px inside a 58 px disc.

/**
 * Generic router — classic 4-port router symbol:
 * horizontal cylinder body + 4 interface stubs (up/down/left/right).
 */
function _mkRouterIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    // Body: rounded rectangle
    `<rect x="20" y="36" width="60" height="28" rx="8" fill="none" stroke="white" stroke-width="7"/>` +
    // 4 stubs
    `<line x1="50" y1="14" x2="50" y2="36" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    `<line x1="50" y1="64" x2="50" y2="86" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    `<line x1="20" y1="50" x2="5"  y2="50" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    `<line x1="80" y1="50" x2="95" y2="50" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    // 2 port dots inside body
    `<circle cx="38" cy="50" r="5" fill="white"/>` +
    `<circle cx="62" cy="50" r="5" fill="white"/>` +
    `</svg>`
  );
}

/**
 * Cisco (IOS / IOS-XR) — 3 fat rounded pillars in a symmetric arch.
 * Short–Tall–Short cadence is the universally recognised Cisco logo motif.
 * Made 3 bars instead of 5–7 so each bar is thick enough to read at 58 px.
 */
function _mkCiscoIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    `<rect x="10" y="42" width="20" height="30" rx="10" fill="white"/>` +
    `<rect x="40" y="16" width="20" height="68" rx="10" fill="white"/>` +
    `<rect x="70" y="42" width="20" height="30" rx="10" fill="white"/>` +
    `</svg>`
  );
}

/**
 * Juniper (JUNOS / cRPD / vJunos) — bold "J" lettermark.
 * Thick stroke, rounded cap, clear hook at bottom.
 * Immediately read as "Juniper" by any network engineer.
 */
function _mkJuniperIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    `<path d="M60,14 L60,72 Q60,94 40,92 Q18,90 18,72"` +
    ` fill="none" stroke="white" stroke-width="14" stroke-linecap="round" stroke-linejoin="round"/>` +
    `</svg>`
  );
}

/**
 * Nokia SR Linux — bold "N" lettermark.
 * Clean Nokia brand initial; two thick verticals joined by a diagonal.
 * Distinct from the SROS broadcast-arc icon on the same canvas.
 */
function _mkNokiaSrlIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    `<path d="M14,86 L14,14 L86,86 L86,14"` +
    ` fill="none" stroke="white" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>` +
    `</svg>`
  );
}

/**
 * Nokia SR OS — wireless broadcast / carrier icon.
 * 3 concentric upward arcs + base dot.
 * Nokia SR OS = carrier-grade routing → transmission tower motif.
 * Clearly distinct from the SRL "N" and zero resemblance to a DB stack.
 */
function _mkNokiaSrosIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    `<circle cx="50" cy="82" r="7" fill="white"/>` +
    `<path d="M30,68 Q50,50 70,68" fill="none" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    `<path d="M16,52 Q50,26 84,52" fill="none" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    `<path d="M4,37  Q50,6  96,37" fill="none" stroke="white" stroke-width="7" stroke-linecap="round"/>` +
    `</svg>`
  );
}

/**
 * Arista cEOS — bold "A" lettermark.
 * Two wide diagonals + elevated crossbar; thick stroke reads cleanly at small sizes.
 */
function _mkAristaIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">` +
    `<path d="M50,12 L12,88 M50,12 L88,88 M25,58 L75,58"` +
    ` fill="none" stroke="white" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/>` +
    `</svg>`
  );
}

/**
 * Server / Linux — 3× rack-unit rows with LED indicators.
 * ViewBox 100×74 matches the 76×56 round-rectangle node shape.
 */
function _mkServerIcon() {
  return _svgToDataUri(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 74">` +
    `<rect x="5" y="5"  width="90" height="18" rx="2" fill="rgba(255,255,255,0.18)" stroke="rgba(255,255,255,0.65)" stroke-width="1.5"/>` +
    `<rect x="9" y="8"  width="38" height="12" rx="1" fill="rgba(255,255,255,0.3)"/>` +
    `<circle cx="63" cy="14" r="3.5" fill="#4ade80"/>` +
    `<circle cx="72" cy="14" r="3.5" fill="rgba(255,255,255,0.5)"/>` +
    `<rect x="5" y="28" width="90" height="18" rx="2" fill="rgba(255,255,255,0.18)" stroke="rgba(255,255,255,0.65)" stroke-width="1.5"/>` +
    `<rect x="9" y="31" width="38" height="12" rx="1" fill="rgba(255,255,255,0.3)"/>` +
    `<circle cx="63" cy="37" r="3.5" fill="#4ade80"/>` +
    `<circle cx="72" cy="37" r="3.5" fill="rgba(255,255,255,0.5)"/>` +
    `<rect x="5" y="51" width="90" height="18" rx="2" fill="rgba(255,255,255,0.18)" stroke="rgba(255,255,255,0.65)" stroke-width="1.5"/>` +
    `<rect x="9" y="54" width="38" height="12" rx="1" fill="rgba(255,255,255,0.3)"/>` +
    `<circle cx="63" cy="60" r="3.5" fill="#4ade80"/>` +
    `<circle cx="72" cy="60" r="3.5" fill="rgba(255,255,255,0.5)"/>` +
    `</svg>`
  );
}

// Pre-built icon data URIs (static, computed once at load time)
// IconsDB router icon with color variants, embedded locally (no hotlinking).
const _ROUTER_ICON     = 'data:image/png;base64,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';
const _CISCO_ICON      = 'data:image/png;base64,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';
const _JUNIPER_ICON    = 'data:image/png;base64,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';
const _NOKIA_SRL_ICON  = 'data:image/png;base64,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';
const _NOKIA_SROS_ICON = 'data:image/png;base64,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';
const _ARISTA_ICON     = 'data:image/png;base64,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';
const _SERVER_ICON     = _mkServerIcon();

function iconForFamily(familyId) {
  switch (familyId) {
    case 'ios':
    case 'iosxr':   return _CISCO_ICON;
    case 'junos':
    case 'crpd':
    case 'vjunos':  return _JUNIPER_ICON;
    case 'srl':     return _NOKIA_SRL_ICON;
    case 'sros':    return _NOKIA_SROS_ICON;
    case 'ceos':    return _ARISTA_ICON;
    case 'linux':   return _SERVER_ICON;
    default:        return _ROUTER_ICON;
  }
}

function randomCanvasPos() {
  const extent = AppState.cy.extent();
  const w = extent.x2 - extent.x1 || 600;
  const h = extent.y2 - extent.y1 || 400;
  return {
    x: extent.x1 + 80 + Math.random() * (w - 160),
    y: extent.y1 + 80 + Math.random() * (h - 160),
  };
}

function removeSelectedNode() {
  if (!AppState.selectedElement) return;
  const nodeId = AppState.selectedElement;
  // Remove connected links
  AppState.links = AppState.links.filter(l => {
    if (l.src === nodeId || l.dst === nodeId) {
      AppState.cy.getElementById(l.id).remove();
      return false;
    }
    return true;
  });
  AppState.nodes = AppState.nodes.filter(n => n.id !== nodeId);
  AppState.cy.getElementById(nodeId).remove();
  AppState.selectedElement = null;
  showPanel('panel-info');
  updateYamlPreview();
}

// ─── Node selection & editor ──────────────────────────────────────────────────

function selectNode(nodeId) {
  AppState.selectedElement = nodeId;
  AppState.cy.$(':selected').unselect();
  AppState.cy.getElementById(nodeId).select();

  const node = AppState.nodes.find(n => n.id === nodeId);
  if (!node) return;

  // Populate editor fields
  document.getElementById('edit-node-id').value = nodeId;
  document.getElementById('edit-node-name').value = node.name;

  // Family select
  const famSel = document.getElementById('edit-node-family');
  famSel.innerHTML = '';
  AppState.families.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f.id;
    opt.textContent = f.label;
    if (f.id === node.family) opt.selected = true;
    famSel.appendChild(opt);
  });

  populateImageSelect(node.family, node.image);
  document.getElementById('edit-node-mgmt-ip').value = node.mgmt_ip || '';

  showPanel('panel-node-editor');
}

function onEditFamilyChange() {
  const familyId = document.getElementById('edit-node-family').value;
  populateImageSelect(familyId, null);
}

function populateImageSelect(familyId, currentImage) {
  const imgSel = document.getElementById('edit-node-image');
  imgSel.innerHTML = '';

  // Optionnel : champ libre
  const manualOpt = document.createElement('option');
  manualOpt.value = '__manual__';
  manualOpt.textContent = '-- Image personnalisée --';
  imgSel.appendChild(manualOpt);

  // Use real docker images from the agent if available, else fall back to family defaults
  const images = AppState.dockerImages.length > 0
    ? AppState.dockerImages.map(img => ({ image: img.image, label: img.image }))
    : imagesForFamily(familyId);

  images.forEach(img => {
    const opt = document.createElement('option');
    opt.value = img.image;
    opt.textContent = img.label || img.image;
    if (img.image === currentImage) opt.selected = true;
    imgSel.appendChild(opt);
  });

  if (currentImage && !images.find(i => i.image === currentImage)) {
    // Image non listée → sélect manuel
    const opt = document.createElement('option');
    opt.value = currentImage;
    opt.textContent = currentImage;
    opt.selected = true;
    imgSel.appendChild(opt);
  }

  onImageSelectChange(currentImage);
}

function onImageSelectChange(forceValue = null) {
  const imgSel = document.getElementById('edit-node-image');
  const manualDiv = document.getElementById('edit-node-image-manual-wrap');
  const manualInput = document.getElementById('edit-node-image-manual');
  const val = forceValue !== null ? forceValue : imgSel.value;
  // Use the same image source as populateImageSelect: docker images if available, else family defaults
  const currentImages = AppState.dockerImages.length > 0
    ? AppState.dockerImages
    : imagesForFamily(document.getElementById('edit-node-family').value);
  if (val === '__manual__' || (forceValue && !currentImages.find(i => i.image === forceValue))) {
    manualDiv.classList.remove('hidden');
    if (forceValue && forceValue !== '__manual__') manualInput.value = forceValue;
  } else {
    manualDiv.classList.add('hidden');
    manualInput.value = '';
  }
}

function saveNodeEdits() {
  const nodeId = document.getElementById('edit-node-id').value;
  const node = AppState.nodes.find(n => n.id === nodeId);
  if (!node) return;

  const name    = document.getElementById('edit-node-name').value.trim();
  const family  = document.getElementById('edit-node-family').value;
  const imgSel  = document.getElementById('edit-node-image').value;
  const manual  = document.getElementById('edit-node-image-manual').value.trim();
  const image   = imgSel === '__manual__' ? manual : imgSel;
  const mgmt_ip = document.getElementById('edit-node-mgmt-ip').value.trim();

  if (!name) { showToast('Le nom du nœud est obligatoire.', 'warn'); return; }
  if (!image) { showToast('Une image est requise.', 'warn'); return; }

  // Valider l'IP de management par rapport au subnet de la VM
  if (mgmt_ip && AppState.selectedVm?.management_subnet) {
    const subnet = AppState.selectedVm.management_subnet;
    if (!isIpInSubnet(mgmt_ip, subnet)) {
      showToast(`IP ${mgmt_ip} hors du subnet de management (${subnet}).`, 'error');
      return;
    }
  }

  node.name    = name;
  node.family  = family;
  node.image   = image;
  node.mgmt_ip = mgmt_ip;

  AppState.cy.getElementById(nodeId).data({
    label: name,
    family,
    icon: iconForFamily(family),
  });

  showToast(`Nœud '${name}' mis à jour.`, 'success');
  updateYamlPreview();
}

// ─── Interface helpers ────────────────────────────────────────────────────────

function getUsedInterfaces(nodeId, excludeLinkId = null) {
  const used = new Set();
  AppState.links.forEach(l => {
    if (l.id === excludeLinkId) return;
    if (l.src === nodeId && l.srcIface) used.add(l.srcIface);
    if (l.dst === nodeId && l.dstIface) used.add(l.dstIface);
  });
  return used;
}

function getNextAvailableInterface(nodeId, familyId, excludeLinkId = null) {
  const used = getUsedInterfaces(nodeId, excludeLinkId);
  const ifaces = interfacesForFamily(familyId, 32);
  return ifaces.find(i => !used.has(i)) || (ifaces[0] || 'eth0');
}

// ─── Link management ──────────────────────────────────────────────────────────

function startLinkFromNode(nodeId) {
  if (AppState.nodes.length < 2) {
    showToast('Ajoutez au moins 2 nœuds pour créer un lien.', 'warn');
    return;
  }
  clearPendingLink();
  AppState.pendingLinkSrc = nodeId;
  AppState.cy.getElementById(nodeId).addClass('pending-link');
  const name = AppState.nodes.find(n => n.id === nodeId)?.name || nodeId;
  showToast(`Source : ${name}. Cliquez sur le nœud destination.`, 'info');
}

function clearPendingLink() {
  AppState.pendingLinkSrc = null;
  AppState.cy.nodes().removeClass('pending-link');
}

// ─── Context menu (right-click on node) ───────────────────────────────────────

function showNodeContextMenu(renderedPos, nodeId) {
  const canvas = document.getElementById('cy-canvas');
  const menu = document.getElementById('node-context-menu');
  if (!canvas || !menu) return;
  menu.dataset.nodeId = nodeId;
  const rect = canvas.getBoundingClientRect();
  const menuW = 180;
  const menuH = 142;
  let left = renderedPos.x;
  let top  = renderedPos.y;
  if (left + menuW > rect.width)  left = left - menuW;
  if (top  + menuH > rect.height) top  = top  - menuH;
  menu.style.left = `${Math.max(0, left)}px`;
  menu.style.top  = `${Math.max(0, top)}px`;
  menu.classList.remove('hidden');
}

function hideNodeContextMenu() {
  const menu = document.getElementById('node-context-menu');
  if (menu) menu.classList.add('hidden');
}

function ctxMenuStartLink() {
  const menu = document.getElementById('node-context-menu');
  if (!menu) return;
  const nodeId = menu.dataset.nodeId;
  hideNodeContextMenu();
  startLinkFromNode(nodeId);
}

function ctxMenuDeleteNode() {
  const menu = document.getElementById('node-context-menu');
  if (!menu) return;
  const nodeId = menu.dataset.nodeId;
  hideNodeContextMenu();
  if (!nodeId) return;
  AppState.selectedElement = nodeId;
  removeSelectedNode();
}

function ctxMenuDuplicateNode() {
  const menu = document.getElementById('node-context-menu');
  if (!menu) return;
  const nodeId = menu.dataset.nodeId;
  hideNodeContextMenu();
  if (!nodeId) return;
  const src = AppState.nodes.find(n => n.id === nodeId);
  if (!src) return;
  const id = `n${++AppState.nodeCounter}`;
  const node = { ...src, id, name: `${src.name}-copy`, mgmt_ip: '' };
  AppState.nodes.push(node);
  const srcPos = AppState.cy.getElementById(nodeId).position();
  AppState.cy.add({
    group: 'nodes',
    data: { id, label: node.name, family: node.family, icon: iconForFamily(node.family) },
    position: { x: srcPos.x + 80, y: srcPos.y + 60 },
  });
  selectNode(id);
  updateYamlPreview();
  showToast(`Nœud '${node.name}' créé.`, 'success');
}

function finishLink(dstNodeId) {
  const srcNodeId = AppState.pendingLinkSrc;
  clearPendingLink();

  const srcNode = AppState.nodes.find(n => n.id === srcNodeId);
  const dstNode = AppState.nodes.find(n => n.id === dstNodeId);
  if (!srcNode || !dstNode) return;

  const linkId = `e${++AppState.linkCounter}`;
  const link = {
    id: linkId,
    src: srcNodeId,
    srcIface: '',
    dst: dstNodeId,
    dstIface: '',
  };
  AppState.links.push(link);

  // Auto-assign first available interface on each side
  link.srcIface = getNextAvailableInterface(srcNodeId, srcNode.family, linkId);
  link.dstIface = getNextAvailableInterface(dstNodeId, dstNode.family, linkId);

  AppState.cy.add({
    group: 'edges',
    data: {
      id: linkId,
      source: srcNodeId,
      target: dstNodeId,
      label: `${link.srcIface} ↔ ${link.dstIface}`,
    },
  });

  selectLink(linkId);
  updateYamlPreview();
}

function selectLink(linkId) {
  AppState.selectedElement = linkId;
  AppState.cy.$(':selected').unselect();
  AppState.cy.getElementById(linkId).select();

  const link = AppState.links.find(l => l.id === linkId);
  if (!link) return;

  const srcNode = AppState.nodes.find(n => n.id === link.src);
  const dstNode = AppState.nodes.find(n => n.id === link.dst);

  document.getElementById('edit-link-id').value = linkId;

  // Label with free-interface count badge
  const srcFreeCount = interfacesForFamily(srcNode?.family || 'linux', 32).filter(
    i => !getUsedInterfaces(link.src, linkId).has(i)
  ).length;
  const dstFreeCount = interfacesForFamily(dstNode?.family || 'linux', 32).filter(
    i => !getUsedInterfaces(link.dst, linkId).has(i)
  ).length;
  document.getElementById('edit-link-src-label').textContent =
    `${srcNode?.name || link.src}  (${srcFreeCount} libres)`;
  document.getElementById('edit-link-dst-label').textContent =
    `${dstNode?.name || link.dst}  (${dstFreeCount} libres)`;

  // Populate interface selects (passing nodeId so used interfaces can be highlighted)
  populateLinkIfaceSelect('edit-link-src-iface', link.src, srcNode?.family || 'linux', link.srcIface, linkId);
  populateLinkIfaceSelect('edit-link-dst-iface', link.dst, dstNode?.family || 'linux', link.dstIface, linkId);

  showPanel('panel-link-editor');
}

function populateLinkIfaceSelect(selectId, nodeId, familyId, currentIface, excludeLinkId = null) {
  const sel = document.getElementById(selectId);
  sel.innerHTML = '';
  const ifaces = interfacesForFamily(familyId, 32);
  const used   = getUsedInterfaces(nodeId, excludeLinkId);

  // ── Free interfaces ──────────────────────────────────────────────────────
  const freeIfaces = ifaces.filter(i => !used.has(i));

  // If currentIface is not in the generated list (e.g. imported non-standard name)
  // add it as first option so the current value is always visible and selectable
  if (currentIface && !ifaces.includes(currentIface) && currentIface !== '__custom__') {
    const opt = document.createElement('option');
    opt.value = currentIface;
    opt.textContent = currentIface;
    opt.selected = true;
    sel.appendChild(opt);
  }

  freeIfaces.forEach(iface => {
    const opt = document.createElement('option');
    opt.value = iface;
    opt.textContent = iface;
    if (iface === currentIface) opt.selected = true;
    sel.appendChild(opt);
  });

  // ── Already-used interfaces (disabled, at bottom for reference) ──────────
  const usedIfaces = ifaces.filter(i => used.has(i));
  if (usedIfaces.length) {
    const grp = document.createElement('optgroup');
    grp.label = `── Déjà utilisées (${usedIfaces.length})`;
    usedIfaces.forEach(iface => {
      const opt = document.createElement('option');
      opt.value = iface;
      opt.textContent = `${iface}  ✗`;
      opt.disabled = true;
      grp.appendChild(opt);
    });
    sel.appendChild(grp);
  }

  // ── Custom entry ─────────────────────────────────────────────────────────
  const customOpt = document.createElement('option');
  customOpt.value = '__custom__';
  customOpt.textContent = '── Nom personnalisé';
  sel.appendChild(customOpt);
}

function saveLinkEdits() {
  const linkId = document.getElementById('edit-link-id').value;
  const link = AppState.links.find(l => l.id === linkId);
  if (!link) return;

  const srcIface = document.getElementById('edit-link-src-iface').value;
  const dstIface = document.getElementById('edit-link-dst-iface').value;

  const newSrcIface = srcIface === '__custom__' ? '' : srcIface;
  const newDstIface = dstIface === '__custom__' ? '' : dstIface;

  // Vérifier l'unicité des interfaces (en excluant le lien courant)
  if (newSrcIface) {
    const usedSrc = getUsedInterfaces(link.src, linkId);
    if (usedSrc.has(newSrcIface)) {
      const srcName = AppState.nodes.find(n => n.id === link.src)?.name || link.src;
      showToast(`Interface ${newSrcIface} déjà utilisée sur ${srcName}.`, 'error');
      return;
    }
  }
  if (newDstIface) {
    const usedDst = getUsedInterfaces(link.dst, linkId);
    if (usedDst.has(newDstIface)) {
      const dstName = AppState.nodes.find(n => n.id === link.dst)?.name || link.dst;
      showToast(`Interface ${newDstIface} déjà utilisée sur ${dstName}.`, 'error');
      return;
    }
  }

  link.srcIface = newSrcIface;
  link.dstIface = newDstIface;

  const label = `${link.srcIface || '?'} ↔ ${link.dstIface || '?'}`;
  AppState.cy.getElementById(linkId).data({ label });

  showToast('Lien mis à jour.', 'success');
  updateYamlPreview();
}

function removeSelectedLink() {
  if (!AppState.selectedElement) return;
  const linkId = AppState.selectedElement;
  AppState.links = AppState.links.filter(l => l.id !== linkId);
  AppState.cy.getElementById(linkId).remove();
  AppState.selectedElement = null;
  showPanel('panel-info');
  updateYamlPreview();
}

// ─── Deselect ─────────────────────────────────────────────────────────────────

function deselectAll() {
  AppState.selectedElement = null;
  AppState.cy.$(':selected').unselect();
}

// ─── Topology helpers ─────────────────────────────────────────────────────────

function buildTopologyPayload() {
  // Place imported nodes/links first, new additions last — preserves diff clarity
  const importedNodes = AppState.nodes.filter(n => AppState.importedNodeIds.has(n.id));
  const newNodes      = AppState.nodes.filter(n => !AppState.importedNodeIds.has(n.id));
  const sortedNodes   = [...importedNodes, ...newNodes];

  const importedLinks = AppState.links.filter(l => AppState.importedLinkIds.has(l.id));
  const newLinks      = AppState.links.filter(l => !AppState.importedLinkIds.has(l.id));
  const sortedLinks   = [...importedLinks, ...newLinks];

  const hasImport = AppState.importedNodeIds.size > 0 || AppState.importedLinkIds.size > 0;
  const nodePositions = {};
  sortedNodes.forEach(n => {
    const pos = AppState.cy.getElementById(n.id)?.position?.();
    if (!pos) return;
    const x = Number(pos.x);
    const y = Number(pos.y);
    if (Number.isFinite(x) && Number.isFinite(y)) {
      nodePositions[n.name] = { x, y };
    }
  });

  const payload = {
    lab_name:     document.getElementById('field-lab-name').value.trim(),
    mgmt_network: document.getElementById('field-mgmt-network').value.trim(),
    mgmt_subnet:  document.getElementById('field-mgmt-subnet').value.trim(),
    nodes: sortedNodes.map(n => ({
      name:          n.name,
      family:        n.family,
      image:         n.image,
      mgmt_ip:       n.mgmt_ip || null,
      original_kind: n.original_kind || null,
      extra_params:  n.extra_params || null,
    })),
    links: sortedLinks.map(l => ({
      src_node:  AppState.nodes.find(n => n.id === l.src)?.name || l.src,
      src_iface: l.srcIface || null,
      dst_node:  AppState.nodes.find(n => n.id === l.dst)?.name || l.dst,
      dst_iface: l.dstIface || null,
    })),
    node_positions: nodePositions,
    passthrough_links: AppState.passthroughLinks.slice(),
  };
  if (hasImport) {
    payload.imported_node_count = importedNodes.length;
    payload.imported_link_count = importedLinks.length;
  }
  return payload;
}

// ─── YAML Preview ─────────────────────────────────────────────────────────────

async function updateYamlPreview() {
  const payload = buildTopologyPayload();

  // Client-side quick check
  if (!payload.lab_name || AppState.nodes.length === 0) {
    document.getElementById('yaml-preview').textContent = '# Définissez un lab et ajoutez des nœuds...';
    return;
  }

  try {
    const data = await apiFetch('/api/topology-builder/generate-yaml', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    AppState.lastYaml = data.yaml;
    document.getElementById('yaml-preview').textContent = data.yaml;
    document.getElementById('yaml-errors').textContent = '';
    document.getElementById('yaml-errors').classList.add('hidden');
    document.getElementById('btn-download-yaml').disabled = false;
    document.getElementById('btn-create-sandbox').disabled = false;
    updateCreateSandboxButtonState();
  } catch (err) {
    document.getElementById('yaml-preview').textContent = '# Erreurs de validation — corrigez la topologie';
    const errBox = document.getElementById('yaml-errors');
    errBox.textContent = err.message;
    errBox.classList.remove('hidden');
    document.getElementById('btn-download-yaml').disabled = true;
    document.getElementById('btn-create-sandbox').disabled = true;
    AppState.lastYaml = '';
  }
}

function updateCreateSandboxButtonState() {
  const btn = document.getElementById('btn-create-sandbox');
  if (btn) btn.disabled = !(AppState.selectedVm && AppState.lastYaml);
}

// ─── Download YAML ────────────────────────────────────────────────────────────

function downloadYaml() {
  if (!AppState.lastYaml) return;
  const labName = document.getElementById('field-lab-name').value.trim() || 'topology';
  const blob = new Blob([AppState.lastYaml], { type: 'text/yaml' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${labName}.yaml`;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * Commit the current topology YAML back to the source sandbox lab.
 * Only available when the page was opened with ?source=sandbox&vm_id=X&lab_name=Y
 */

// Module-level handles for the async confirm step inside commitToLab
let _commitConfirmResolve = null;
let _commitConfirmReject  = null;

function onCommitConfirm() {
  const res = _commitConfirmResolve;
  _commitConfirmResolve = null;
  _commitConfirmReject  = null;
  if (res) res();
}

function onCommitCancel() {
  const panel = document.getElementById('commit-steps-panel');
  if (panel) panel.classList.add('hidden');
  const rej = _commitConfirmReject;
  _commitConfirmResolve = null;
  _commitConfirmReject  = null;
  if (rej) rej(new Error('Annulé'));
}

async function commitToLab() {
  if (!AppState.lastYaml) {
    showToast('Générez d\'abord un YAML valide.', 'error');
    return;
  }
  const btn        = document.getElementById('btn-commit-to-lab');
  const feedback   = document.getElementById('commit-feedback');
  const panel      = document.getElementById('commit-steps-panel');
  const stepsList  = document.getElementById('commit-steps-list');
  const stepsTitle = document.getElementById('commit-steps-title');
  const stepsAct   = document.getElementById('commit-steps-actions');

  // Notify parent that commit is starting
  if (window.opener) {
    window.opener.postMessage({
      type: 'topology-commit-start',
      vmId: AppState.sourceVmId,
      labName: AppState.sourceLabName,
      timestamp: new Date().toISOString(),
    }, '*');
  }

  if (btn) btn.disabled = true;
  if (feedback) { feedback.textContent = ''; feedback.className = 'text-xs'; }

  // Show the commit log panel
  if (panel)      panel.classList.remove('hidden');
  if (stepsTitle) { stepsTitle.textContent = 'Validation en cours…'; stepsTitle.className = 'text-xs font-medium text-sky-300'; }
  if (stepsList)  stepsList.innerHTML = '<p class="text-xs text-slate-500">Veuillez patienter…</p>';
  if (stepsAct)   stepsAct.classList.add('hidden');

  try {
    // Step 1 — validate (apply=false)
    const validateResp = await apiFetch(
      `/api/vms/${encodeURIComponent(AppState.sourceVmId)}/labs/${encodeURIComponent(AppState.sourceLabName)}/sandbox-yaml`,
      { method: 'POST', body: JSON.stringify({ yaml_text: AppState.lastYaml, apply: false, node_positions: buildTopologyPayload().node_positions }) },
    );
    renderCommitSteps(stepsList, validateResp?.result?.steps || []);

    const validation = validateResp?.result?.validation || {};
    const errors   = Array.isArray(validation.errors)   ? validation.errors   : [];
    const warnings = Array.isArray(validation.warnings) ? validation.warnings : [];

    if (errors.length) {
      if (stepsTitle) { stepsTitle.textContent = `Validation KO — ${errors.join(' | ')}`; stepsTitle.className = 'text-xs font-medium text-rose-300'; }
      if (feedback)   { feedback.textContent = 'Validation KO'; feedback.className = 'text-xs text-rose-300'; }
      if (btn) btn.disabled = false;
      return;
    }

    if (stepsTitle) {
      stepsTitle.textContent = warnings.length
        ? `Validation OK (${warnings.length} avertissement(s)) — Confirmez ?`
        : `Validation OK — Confirmez le déploiement ?`;
      stepsTitle.className = 'text-xs font-medium text-amber-300';
    }
    if (stepsAct) stepsAct.classList.remove('hidden');
    if (btn) btn.disabled = false;

    // Step 2 — wait for user confirmation via commit panel buttons
    await new Promise((resolve, reject) => {
      _commitConfirmResolve = resolve;
      _commitConfirmReject  = reject;
    });

    // Step 3 — apply
    if (btn) btn.disabled = true;
    if (stepsTitle) { stepsTitle.textContent = 'Application en cours…'; stepsTitle.className = 'text-xs font-medium text-sky-300'; }
    if (stepsAct)   stepsAct.classList.add('hidden');
    if (stepsList)  stepsList.innerHTML = '<p class="text-xs text-slate-500">Déploiement en cours…</p>';

    const applyResp = await apiFetch(
      `/api/vms/${encodeURIComponent(AppState.sourceVmId)}/labs/${encodeURIComponent(AppState.sourceLabName)}/sandbox-yaml`,
      { method: 'POST', body: JSON.stringify({ yaml_text: AppState.lastYaml, apply: true, node_positions: buildTopologyPayload().node_positions }) },
    );
    renderCommitSteps(stepsList, applyResp?.result?.steps || []);
    if (stepsTitle) { stepsTitle.textContent = `✓ Committé sur « ${AppState.sourceLabName} »`; stepsTitle.className = 'text-xs font-medium text-emerald-300'; }
    showToast(`YAML commité sur « ${AppState.sourceLabName} » ✓`, 'success');
    if (feedback) { feedback.textContent = `✓ Committé sur ${AppState.sourceLabName}`; feedback.className = 'text-xs text-emerald-300'; }

    // Notify the parent/central that the topology builder commit succeeded
    if (window.opener) {
      window.opener.postMessage({
        type: 'topology-commit-success',
        vmId: AppState.sourceVmId,
        labName: AppState.sourceLabName,
        timestamp: new Date().toISOString(),
      }, '*');
    }

  } catch (err) {
    if (err.message !== 'Annulé') {
      const errMsg = err.message || 'inconnue';
      if (stepsTitle) { stepsTitle.textContent = `Erreur : ${errMsg}`; stepsTitle.className = 'text-xs font-medium text-rose-300'; }
      const errSteps = err.responseData?.detail?.steps || err.responseData?.result?.steps || [];
      if (stepsList) renderCommitSteps(stepsList, errSteps);
      showToast(errMsg, 'error');
      if (feedback) { feedback.textContent = errMsg; feedback.className = 'text-xs text-rose-300'; }

      // Notify parent that commit failed
      if (window.opener) {
        window.opener.postMessage({
          type: 'topology-commit-error',
          vmId: AppState.sourceVmId,
          labName: AppState.sourceLabName,
          error: errMsg,
          timestamp: new Date().toISOString(),
        }, '*');
      }
    }
    // If 'Annulé', onCommitCancel already hid the panel
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderCommitSteps(container, steps) {
  if (!container) return;
  const entries = Array.isArray(steps) ? steps : [];
  if (!entries.length) {
    container.innerHTML = '<p class="text-xs text-slate-500">Aucune étape disponible.</p>';
    return;
  }
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  container.innerHTML = entries.map(step => {
    const ok  = Boolean(step?.ok);
    const lbl = esc(step?.name    || 'step');
    const msg = esc(step?.message || '');
    return `<div class="rounded border ${ok ? 'border-emerald-700/50 bg-emerald-950/15' : 'border-rose-700/50 bg-rose-950/15'} px-2 py-1">
      <div class="flex items-center gap-2">
        <span class="font-mono text-xs ${ok ? 'text-emerald-300' : 'text-rose-300'}">${ok ? '✓' : '✗'}</span>
        <span class="text-xs text-slate-100">${lbl}</span>
      </div>
      ${msg ? `<p class="mt-0.5 text-[11px] text-slate-300">${msg}</p>` : ''}
    </div>`;
  }).join('');
}

// ─── Create Sandbox ───────────────────────────────────────────────────────────

async function createSandbox() {
  if (!AppState.selectedVm) {
    showToast('Sélectionnez une VM d\'abord.', 'warn');
    return;
  }
  if (!AppState.lastYaml) {
    showToast('Générez un YAML valide d\'abord.', 'warn');
    return;
  }

  const btn = document.getElementById('btn-create-sandbox');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Création...';

  try {
    const payload = {
      topology: buildTopologyPayload(),
      vm_id: AppState.selectedVm.id,
      create_sandbox: true,
    };

    const data = await apiFetch('/api/topology-builder/create-sandbox', {
      method: 'POST',
      body: JSON.stringify(payload),
    });

    showToast(`✓ Lab '${data.lab_name}' créé sur ${AppState.selectedVm.name} !`, 'success');
    document.getElementById('sandbox-result').textContent =
      `Lab '${data.lab_name}' soumis avec succès sur ${AppState.selectedVm.name}.`;
    document.getElementById('sandbox-result').classList.remove('hidden');
  } catch (err) {
    showToast('Erreur création sandbox : ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
    updateCreateSandboxButtonState();
  }
}

// ─── Canvas layout ────────────────────────────────────────────────────────────

function layoutAuto() {
  if (!AppState.cy || AppState.nodes.length === 0) return;
  AppState.cy.layout({
    name: 'cose',
    nodeRepulsion: 8000,
    idealEdgeLength: 120,
    animate: true,
    animationDuration: 400,
  }).run();
}

function clearCanvas() {
  if (AppState.nodes.length === 0) return;
  if (!confirm('Effacer toute la topologie ?')) return;
  AppState.cy.elements().remove();
  AppState.nodes = [];
  AppState.links = [];
  AppState.passthroughLinks = [];
  AppState.selectedElement = null;
  AppState.lastYaml = '';
  AppState.importedNodeIds = new Set();
  AppState.importedLinkIds = new Set();
  document.getElementById('yaml-preview').textContent = '# Définissez un lab et ajoutez des nœuds...';
  document.getElementById('yaml-errors').classList.add('hidden');
  document.getElementById('sandbox-result').classList.add('hidden');
  showPanel('panel-info');
}

// ─── Import YAML ─────────────────────────────────────────────────────────────

function importYaml() {
  document.getElementById('import-yaml-text').value = '';
  document.getElementById('import-yaml-err').classList.add('hidden');
  document.getElementById('modal-import-yaml').classList.remove('hidden');
  setTimeout(() => document.getElementById('import-yaml-text').focus(), 50);
}

function closeImportModal() {
  document.getElementById('modal-import-yaml').classList.add('hidden');
}

async function fetchSourceLabNodePositions(vmId, labName) {
  if (!vmId || !labName) return {};
  try {
    const state = await apiFetch(`/api/vms/${encodeURIComponent(vmId)}/state`);
    const labs = Array.isArray(state?.labs) ? state.labs : [];
    const targetLab = labs.find(l => String(l?.name || '') === String(labName));
    const graphNodes = Array.isArray(targetLab?.graph?.nodes) ? targetLab.graph.nodes : [];

    const byName = {};
    graphNodes.forEach(node => {
      const name = String(node?.name || '').trim();
      if (!name) return;
      // Guard against null/undefined — Number(null) === 0 passes isFinite() but is not a real position
      const rawX = node?.x;
      const rawY = node?.y;
      if (rawX != null && rawY != null) {
        const x = Number(rawX);
        const y = Number(rawY);
        if (Number.isFinite(x) && Number.isFinite(y)) {
          byName[name] = { x, y };
        }
      }
    });
    return byName;
  } catch {
    return {};
  }
}

/** Map a ContainerLab node kind string to one of our known family ids. */
function kindToFamily(kind) {
  if (!kind) return 'linux';
  const k = String(kind).toLowerCase();
  // Nokia families
  if (k === 'nokia_sros' || k.includes('sros') || k.includes('vr-sros')) return 'sros';
  if (k === 'nokia_srlinux' || k.includes('srlinux') || k.includes('sr-linux')) return 'srl';
  // Arista family
  if (k === 'arista_ceos' || k.includes('ceos') || k.includes('arista')) return 'ceos';
  // IOS-XR family: cisco_xrd, cisco_xrv9k, cisco_xrv
  if (k === 'cisco_xrd' || k === 'cisco_xrv9k' || k === 'cisco_xrv' ||
      k.includes('xrd') || k.includes('xrv9k') || k.includes('iosxr')) return 'iosxr';
  // cRPD family: juniper_crpd (before generic juniper check)
  if (k === 'juniper_crpd' || k.includes('crpd')) return 'crpd';
  // vJunos family (before generic Juniper check)
  if (k === 'juniper_vjunos-switch' || k === 'juniper_vjunos-router' || k.includes('vjunos')) return 'vjunos';
    // Junos family: juniper_vmx, juniper_vsrx, juniper_vqfx, juniper_vevo
  if (k === 'juniper_vmx' || k === 'juniper_vsrx' || k === 'juniper_vqfx' ||
      k.includes('vmx') || k.includes('vsrx') || k.includes('vqfx') ||
      (k.includes('juniper') && !k.includes('crpd'))) return 'junos';
    // IOS family: cisco_iol, cisco_iosv, cisco_csr1000v
  if (k === 'cisco_iol' || k === 'cisco_iosv' || k === 'cisco_csr1000v' ||
      k.includes('iol') || k.includes('iosv') || k.includes('csr') ||
      k.includes('cisco_ios') || k.includes('vios')) return 'ios';
  return 'linux';
}

/** Parse pasted ContainerLab YAML and load into the canvas. */
function loadFromYamlText(options = {}) {
  const text = document.getElementById('import-yaml-text').value.trim();
  const errEl = document.getElementById('import-yaml-err');
  errEl.classList.add('hidden');

  if (!text) {
    errEl.textContent = 'Collez d\'abord un YAML valide.';
    errEl.classList.remove('hidden');
    return;
  }

  let parsed;
  try {
    parsed = jsyaml.load(text);
  } catch (e) {
    errEl.textContent = 'Erreur de parsing YAML : ' + e.message;
    errEl.classList.remove('hidden');
    return;
  }

  if (!parsed || typeof parsed !== 'object') {
    errEl.textContent = 'Le YAML ne semble pas être un objet valide.';
    errEl.classList.remove('hidden');
    return;
  }

  const topologyNodes = (parsed.topology || {}).nodes || {};
  const topologyLinks = (parsed.topology || {}).links || [];

  if (Object.keys(topologyNodes).length === 0) {
    errEl.textContent = 'Aucun nœud trouvé dans topology.nodes.';
    errEl.classList.remove('hidden');
    return;
  }

  if (AppState.nodes.length > 0) {
    if (!confirm('La topologie actuelle sera remplacée. Continuer ?')) return;
  }

  // ── Reset state ──
  AppState.cy.elements().remove();
  AppState.nodes       = [];
  AppState.links       = [];
  AppState.selectedElement = null;
  AppState.nodeCounter = 0;
  AppState.linkCounter = 0;
  AppState.lastYaml    = '';
  AppState.importedNodeIds = new Set();
  AppState.importedLinkIds = new Set();
  AppState.passthroughLinks = [];
  document.getElementById('yaml-errors').classList.add('hidden');
  document.getElementById('sandbox-result').classList.add('hidden');

  // ── Fill header fields ──
  const labName    = parsed.name   || '';
  const mgmt       = parsed.mgmt   || {};
  const mgmtNet    = mgmt.network  || 'clab-mgmt';
  const mgmtSubnet = mgmt['ipv4-subnet'] || mgmt.ipv4_subnet || mgmt.ipv4subnet || '';
  if (labName)    document.getElementById('field-lab-name').value      = labName;
  if (mgmtNet)    document.getElementById('field-mgmt-network').value  = mgmtNet;
  if (mgmtSubnet) document.getElementById('field-mgmt-subnet').value   = mgmtSubnet;

  // ── Create nodes ──
  const nodeNameToId = {};
  const nodeNames    = Object.keys(topologyNodes);
  const cols = Math.max(1, Math.ceil(Math.sqrt(nodeNames.length)));
  const sourcePositions = (options && typeof options === 'object' && options.nodePositionsByName && typeof options.nodePositionsByName === 'object')
    ? options.nodePositionsByName
    : {};
  const positionedNodeIds = new Set();

  nodeNames.forEach((nodeName, idx) => {
    const nodeDef  = topologyNodes[nodeName] || {};
    const id       = `n${++AppState.nodeCounter}`;
    nodeNameToId[nodeName] = id;

    const family   = kindToFamily(nodeDef.kind || '');
    const image    = nodeDef.image   || '';
    const mgmt_ip  = nodeDef.mgmt_ipv4 || nodeDef.mgmt_ipv6 || nodeDef['mgmt-ipv4'] || nodeDef['mgmt-ipv6'] || '';
    const original_kind = nodeDef.kind || null;
    // Preserve all fields not explicitly mapped for round-trip fidelity (env, runtime, cpu, memory…)
    const _knownImportKeys = new Set(['kind', 'image', 'mgmt_ipv4', 'mgmt_ipv6', 'mgmt-ipv4', 'mgmt-ipv6', 'startup-config', 'startup_config']);
    const extra_raw = Object.fromEntries(Object.entries(nodeDef).filter(([k]) => !_knownImportKeys.has(k)));
    const extra_params = Object.keys(extra_raw).length ? extra_raw : null;

    AppState.nodes.push({ id, name: nodeName, family, image, mgmt_ip, original_kind, extra_params });
    AppState.importedNodeIds.add(id);

    const col = idx % cols;
    const row = Math.floor(idx / cols);
    const hintedPos = sourcePositions[nodeName];
    const hasHintedPos = Number.isFinite(Number(hintedPos?.x)) && Number.isFinite(Number(hintedPos?.y));
    const nodePosition = hasHintedPos
      ? { x: Number(hintedPos.x), y: Number(hintedPos.y) }
      : { x: 140 + col * 180, y: 140 + row * 140 };
    AppState.cy.add({
      group: 'nodes',
      data: { id, label: nodeName, family, icon: iconForFamily(family) },
      position: nodePosition,
    });
    if (hasHintedPos) positionedNodeIds.add(id);
  });

  // ── Create links ──
  topologyLinks.forEach(linkDef => {
    if (!linkDef || !Array.isArray(linkDef.endpoints) || linkDef.endpoints.length < 2) return;
    const [ep1, ep2] = linkDef.endpoints;
    if (!ep1 || !ep2) return;

    const colonIdx1 = String(ep1).indexOf(':');
    const colonIdx2 = String(ep2).indexOf(':');
    const srcName  = colonIdx1 >= 0 ? ep1.slice(0, colonIdx1)  : String(ep1);
    const srcIface = colonIdx1 >= 0 ? ep1.slice(colonIdx1 + 1) : '';
    const dstName  = colonIdx2 >= 0 ? ep2.slice(0, colonIdx2)  : String(ep2);
    const dstIface = colonIdx2 >= 0 ? ep2.slice(colonIdx2 + 1) : '';

    const srcId = nodeNameToId[srcName];
    const dstId = nodeNameToId[dstName];
    if (!srcId || !dstId) {
      // One or both endpoints reference external nodes (e.g. "host:eth0") — preserve for round-trip
      AppState.passthroughLinks.push(linkDef);
      return;
    }

    const linkId = `e${++AppState.linkCounter}`;
    AppState.links.push({ id: linkId, src: srcId, srcIface, dst: dstId, dstIface });
    AppState.importedLinkIds.add(linkId);
    AppState.cy.add({
      group: 'edges',
      data: {
        id: linkId,
        source: srcId,
        target: dstId,
        label: `${srcIface || '?'} ↔ ${dstIface || '?'}`,
      },
    });
  });

  // ── Layout ──
  if (AppState.nodes.length > 1) {
    const hasHints = positionedNodeIds.size > 0;
    const allHinted = positionedNodeIds.size === AppState.nodes.length;
    if (!allHinted) {
      if (hasHints) {
        positionedNodeIds.forEach(id => AppState.cy.getElementById(id).lock());
      }
      AppState.cy.layout({
        name: 'cose',
        nodeRepulsion: 9000,
        idealEdgeLength: 130,
        animate: false,
      }).run();
      if (hasHints) {
        positionedNodeIds.forEach(id => AppState.cy.getElementById(id).unlock());
      }
    }
  }
  AppState.cy.fit(undefined, 60);

  closeImportModal();
  showPanel('panel-info');
  updateYamlPreview();
  showToast(
    `Topologie '${labName || 'importée'}' chargée — ${AppState.nodes.length} nœud(s), ${AppState.links.length} lien(s).`,
    'success',
  );
}

// ─── DOMContentLoaded bootstrap ───────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  initCytoscape();

  // Read URL params early — used below after metadata loads
  const _qp = new URLSearchParams(window.location.search);
  AppState.sourceVmId    = _qp.get('vm_id')    || null;
  AppState.sourceLabName = _qp.get('lab_name') || null;
  AppState.sourceMode    = _qp.get('source')   || null;  // 'sandbox'

  // Build family buttons and populate VM list
  await loadMetadata();
  buildFamilyButtons();

  // VM select listener
  document.getElementById('vm-select').addEventListener('change', onVmSelect);

  // ── Source lab context (opened from sandbox YAML modal) ──────────────────
  if (AppState.sourceMode === 'sandbox' && AppState.sourceVmId && AppState.sourceLabName) {
    // Auto-select the source VM in the dropdown → fills subnet + loads docker images
    const sel = document.getElementById('vm-select');
    if (sel) {
      sel.value = AppState.sourceVmId;
      onVmSelect();   // triggers subnet fill, docker image fetch, button state update
    }

    // Show commit button and source banner
    document.getElementById('btn-commit-to-lab')?.classList.remove('hidden');
    const banner = document.getElementById('source-lab-banner');
    const txt    = document.getElementById('source-lab-banner-text');
    if (banner) banner.classList.remove('hidden');
    const vmLabel = AppState.selectedVm?.name || AppState.sourceVmId;
    if (txt) txt.textContent = `Mode Sandbox · lab « ${AppState.sourceLabName} » sur ${vmLabel} — "Commit au Lab" met à jour la topologie directement.`;

    // Wire commit button
    document.getElementById('btn-commit-to-lab')?.addEventListener('click', commitToLab);

    // Auto-load YAML from the sandbox endpoint
    try {
      const [data, sourceNodePositions] = await Promise.all([
        apiFetch(
          `/api/vms/${encodeURIComponent(AppState.sourceVmId)}/labs/${encodeURIComponent(AppState.sourceLabName)}/sandbox-yaml`,
        ),
        fetchSourceLabNodePositions(AppState.sourceVmId, AppState.sourceLabName),
      ]);
      const yamlText = data.yaml_text || '';
      if (yamlText) {
        // Feed into the import parser (no modal shown — direct load)
        const ta = document.getElementById('import-yaml-text');
        if (ta) ta.value = yamlText;
        loadFromYamlText({ nodePositionsByName: sourceNodePositions });
      } else {
        showToast('YAML du lab vide ou introuvable.', 'error');
      }
    } catch (err) {
      showToast(`Chargement du lab impossible : ${err.message}`, 'error');
    }
  }

  // Lab fields → trigger yaml update on change
  ['field-lab-name', 'field-mgmt-network', 'field-mgmt-subnet'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => updateYamlPreview());
  });

  // Image select → toggle manual field
  document.getElementById('edit-node-image')?.addEventListener('change', () => onImageSelectChange());

  // Family select → repopulate images
  document.getElementById('edit-node-family')?.addEventListener('change', onEditFamilyChange);

  // Buttons
  document.getElementById('btn-save-node')?.addEventListener('click', saveNodeEdits);
  document.getElementById('btn-remove-node')?.addEventListener('click', removeSelectedNode);
  document.getElementById('btn-save-link')?.addEventListener('click', saveLinkEdits);
  document.getElementById('btn-remove-link')?.addEventListener('click', removeSelectedLink);
  document.getElementById('btn-add-link')?.addEventListener('click', () => {
    showToast('Clic droit sur un nœud → "Créer un lien".', 'info');
  });
  document.getElementById('btn-layout')?.addEventListener('click', layoutAuto);
  document.getElementById('btn-clear')?.addEventListener('click', clearCanvas);
  document.getElementById('btn-download-yaml')?.addEventListener('click', downloadYaml);
  document.getElementById('btn-create-sandbox')?.addEventListener('click', createSandbox);

  document.getElementById('btn-refresh-images')?.addEventListener('click', () => {
    if (AppState.selectedVm) fetchVmDockerImages(AppState.selectedVm.id, true);
  });

  // Dismiss context menu on click outside canvas
  document.addEventListener('click', () => hideNodeContextMenu());

  // Dismiss import modal on Escape
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeImportModal();
  });

  // Prevent browser default context menu on canvas
  document.getElementById('cy-canvas')?.addEventListener('contextmenu', e => e.preventDefault());
});

function buildFamilyButtons() {
  const container = document.getElementById('family-buttons');
  if (!container) return;
  const colors = {
    iosxr: 'border-blue-500 text-blue-300 hover:bg-blue-900/40',
    junos:  'border-green-500 text-green-300 hover:bg-green-900/40',
    ios:    'border-yellow-500 text-yellow-300 hover:bg-yellow-900/40',
    crpd:   'border-emerald-500 text-emerald-300 hover:bg-emerald-900/40',
    sros:   'border-red-500 text-red-300 hover:bg-red-900/40',
    srl:    'border-teal-500 text-teal-300 hover:bg-teal-900/40',
    ceos:   'border-orange-500 text-orange-300 hover:bg-orange-900/40',
    vjunos: 'border-lime-500 text-lime-300 hover:bg-lime-900/40',
    linux:  'border-purple-500 text-purple-300 hover:bg-purple-900/40',
  };
  AppState.families.forEach(fam => {
    const btn = document.createElement('button');
    const colorClass = colors[fam.id] || 'border-slate-500 text-slate-300 hover:bg-slate-700/40';
    btn.className = `w-full text-left px-3 py-2 rounded border text-xs font-medium transition-colors ${colorClass}`;
    btn.innerHTML = `<span class="font-bold">+ </span>${fam.label}`;
    btn.title = fam.images.map(i => i.label).join(', ');
    btn.addEventListener('click', () => addNode(fam.id));
    container.appendChild(btn);
  });
}
