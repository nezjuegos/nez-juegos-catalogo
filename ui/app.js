const API_URL = '/api';

// DOM Elements
const searchBtn = document.getElementById('search-btn');
const clearBtn = document.getElementById('clear-btn');
const refreshBtn = document.getElementById('refresh-btn');
const gameInput = document.getElementById('game-input');
const excludeInput = document.getElementById('exclude-input');
const priceMin = document.getElementById('price-min');
const priceMax = document.getElementById('price-max');
const resultsGrid = document.getElementById('results-grid');
const resultsCount = document.getElementById('results-count');
const statusDot = document.querySelector('.status-dot');
const statusText = document.getElementById('status-text');
const toast = document.getElementById('toast');

// Store all packs for filtering
let allPacks = [];

// --- Initialization ---
function init() {
    checkStatus();
    setInterval(checkStatus, 5000);
}

// --- Status Check ---
async function checkStatus() {
    try {
        const res = await fetch(`${API_URL}/status`);
        const data = await res.json();

        if (data.telegram_connected) {
            statusDot.classList.add('active');
            const cacheInfo = data.cached_packs > 0 ? ` (${data.cached_packs} packs)` : '';
            statusText.textContent = `Conectado${cacheInfo}`;

            // Auto-load packs if cache exists but we haven't loaded yet
            if (data.cached_packs > 0 && allPacks.length === 0) {
                loadAllPacks();
            }
        } else {
            statusDot.classList.remove('active');
            statusText.textContent = "Esperando Login...";
        }
    } catch (e) {
        statusDot.classList.remove('active');
        statusText.textContent = "Servidor Desconectado";
    }
}

// --- Load All Packs (no filter) ---
async function loadAllPacks() {
    try {
        // Use a generic search that returns all packs (empty space searches everything)
        const res = await fetch(`${API_URL}/search?q=&limit=500`);
        const data = await res.json();
        allPacks = data.results || [];
        renderResults(allPacks);
    } catch (e) {
        console.error('Error loading packs:', e);
    }
}

// --- Refresh List (Manual) ---
async function refreshList(messageCount = 1000) {
    const fullBtn = document.getElementById('refresh-full-btn');
    const quickBtn = document.getElementById('refresh-quick-btn');
    const isQuick = messageCount <= 100;
    const activeBtn = isQuick ? quickBtn : fullBtn;

    // Disable both buttons
    fullBtn.disabled = true;
    quickBtn.disabled = true;
    activeBtn.textContent = '⌛ Renovando...';

    const timeEstimate = isQuick ? '~10s' : '~60s';
    resultsGrid.innerHTML = `<div class="empty-state">Escaneando los últimos ${messageCount} mensajes (${timeEstimate})...</div>`;
    resultsCount.textContent = 'Renovando lista...';

    try {
        const res = await fetch(`${API_URL}/refresh?count=${messageCount}`, { method: 'POST' });

        if (res.status === 401) {
            window.location.href = '/admin/login';
            return;
        }

        // Try reading as text first in case it's an HTML error page from Cloudflare/Railway
        const text = await res.text();

        if (!res.ok) {
            console.error("Server returned full error HTML:", text);
            // Try extracting a clean error message, or just show standard error
            throw new Error(`Server status ${res.status}. Check console for details.`);
        }

        const data = JSON.parse(text);

        if (data.packs_found !== undefined) {
            resultsCount.textContent = `✅ Lista renovada: ${data.packs_found} packs encontrados`;
            await loadAllPacks();
        } else {
            resultsGrid.innerHTML = `<div class="empty-state" style="color: #ef4444">Error: ${data.error}</div>`;
        }
    } catch (e) {
        resultsGrid.innerHTML = `<div class="empty-state" style="color: #ef4444">Error: ${e.message}</div>`;
        console.error(e);
    } finally {
        fullBtn.disabled = false;
        quickBtn.disabled = false;
        fullBtn.textContent = '🔄 Renovar 1000';
        quickBtn.textContent = '⚡ Renovar 100';
    }
}

// --- Clear Search ---
function clearSearch() {
    gameInput.value = '';
    excludeInput.value = '';
    priceMin.value = '';
    priceMax.value = '';
    renderResults(allPacks);
}

// --- Search Logic ---
async function performSearch() {
    const query = gameInput.value.trim();
    const exclude = excludeInput.value.trim();
    const minPrice = parseInt(priceMin.value) || null;
    const maxPrice = parseInt(priceMax.value) || null;

    searchBtn.disabled = true;
    searchBtn.textContent = '⌛ Buscando...';

    try {
        // Build URL
        let url = `${API_URL}/search?q=${encodeURIComponent(query)}&exclude=${encodeURIComponent(exclude)}&limit=1000`;
        if (minPrice) url += `&price_min=${minPrice}`;
        if (maxPrice) url += `&price_max=${maxPrice}`;

        const res = await fetch(url);
        const data = await res.json();

        renderResults(data.results || [], query, exclude);
    } catch (e) {
        resultsGrid.innerHTML = `<div class="empty-state" style="color: #ef4444">Error: ${e.message}</div>`;
        resultsCount.textContent = 'Error en la búsqueda';
    } finally {
        searchBtn.disabled = false;
        searchBtn.textContent = '🔍 Buscar';
    }
}

// --- Rendering ---
function renderResults(packs, query = '', exclude = '') {
    resultsGrid.innerHTML = '';

    // Update count
    let countText = `${packs.length} packs disponibles`;
    if (query && exclude) {
        countText = `${packs.length} resultados para "${query}" (excluyendo "${exclude}")`;
    } else if (query) {
        countText = `${packs.length} resultados para "${query}"`;
    } else if (exclude) {
        countText = `${packs.length} resultados excluyendo "${exclude}"`;
    }
    resultsCount.textContent = countText;

    if (packs.length === 0) {
        const message = query
            ? `No se encontraron packs con: "${query}"`
            : 'No hay packs cargados. Haz clic en "Renovar Lista"';
        resultsGrid.innerHTML = `<div class="empty-state">${message}</div>`;
        return;
    }

    packs.forEach(pack => {
        const card = document.createElement('div');
        card.className = 'pack-card';

        // Show ALL games (no truncation)
        const displayGames = pack.games.join('\n');

        card.innerHTML = `
            <div class="pack-header">
                <span class="pack-id">ID: ${pack.id}</span>
            </div>
            <div class="pack-content">${displayGames}</div>
            <div class="pack-footer">
                <div class="footer-left">
                    <span class="pack-price">$${pack.price_local.toLocaleString('es-AR')}</span>
                </div>
                <div class="footer-right">
                    <button class="action-btn" onclick="openEditModal('${pack.id}', '${pack.cover_url || ''}')">
                        ✏️
                    </button>
                    <button class="action-btn btn-danger-icon" onclick="deletePack('${pack.id}')" title="Eliminar Pack">
                        🗑️
                    </button>
                    <button class="copy-btn" onclick="copyToClipboard(this, \`${pack.formatted_text.replace(/`/g, '\\`').replace(/\$/g, '$$')}\`)">
                        📋 Copiar
                    </button>
                </div>
            </div>
        `;
        resultsGrid.appendChild(card);
    });
}

// --- Utilities ---
function copyToClipboard(btn, text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast();
        const originalText = btn.textContent;
        btn.textContent = "✅ Copiado!";
        setTimeout(() => btn.textContent = originalText, 2000);
    });
}

function showToast() {
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 3000);
}

// --- Manual Cover Management ---
let currentEditId = null;

function openEditModal(id, currentUrl) {
    currentEditId = id;
    document.getElementById('edit-modal-pack-info').textContent = `Pack ID: ${id}`;

    // Check if it's the default placeholder, show empty if so
    const isPlaceholder = currentUrl && currentUrl.includes('default');
    document.getElementById('manual-cover-url').value = isPlaceholder ? '' : currentUrl;

    document.getElementById('edit-cover-modal').classList.remove('hidden');
}

function closeEditModal() {
    document.getElementById('edit-cover-modal').classList.add('hidden');
    currentEditId = null;
}

async function saveManualCover() {
    if (!currentEditId) return;
    const url = document.getElementById('manual-cover-url').value.trim();

    try {
        await fetch(`${API_URL}/admin/set-cover`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: currentEditId, url: url })
        });
        closeEditModal();
        performSearch(); // Refresh list to show changes
    } catch (e) {
        alert("Error guardando portada: " + e.message);
    }
}

async function deleteManualCover() {
    if (!currentEditId) return;
    if (!confirm("¿Borrar portada manual y volver a la automática?")) return;

    try {
        await fetch(`${API_URL}/admin/set-cover`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: currentEditId, url: null })
        });
        closeEditModal();
        performSearch();
    } catch (e) {
        alert("Error borrando portada: " + e.message);
    }
}



async function deletePack(id) {
    if (!confirm(`¿Estás seguro de ELIMINAR el pack ${id}? Esta acción no se puede deshacer.`)) return;

    try {
        const res = await fetch(`${API_URL}/admin/delete-pack`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id })
        });

        if (res.ok) {
            // Remove from DOM immediately
            performSearch();
            // Or remove card directly but search refreshes list cleanly
        } else {
            const data = await res.json();
            alert("Error al eliminar: " + data.error);
        }
    } catch (e) {
        alert("Error de red: " + e.message);
    }
}

// --- Bulk Edit ---
function openBulkModal() {
    document.getElementById('bulk-edit-modal').classList.remove('hidden');
}

function closeBulkModal() {
    document.getElementById('bulk-edit-modal').classList.add('hidden');
}

async function saveBulkCovers() {
    const text = document.getElementById('bulk-cover-text').value;
    const lines = text.split('\n');
    const covers = [];

    lines.forEach(line => {
        const parts = line.trim().split(/\s+/);
        if (parts.length >= 2) {
            const id = parts[0];
            const url = parts.slice(1).join(''); // In case URL has spaces? Unlikely but safe
            if (id && url) covers.push({ id, url });
        }
    });

    if (covers.length === 0) {
        alert("No se detectaron líneas válidas (Formato: ID URL)");
        return;
    }

    try {
        const res = await fetch(`${API_URL}/admin/bulk-set-covers`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ covers })
        });
        const data = await res.json();
        alert(`Actualizados ${data.updated} packs correctamente.`);
        closeBulkModal();
        performSearch();
    } catch (e) {
        alert("Error guardando masivo: " + e.message);
    }
}


// Event Listeners
gameInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') performSearch();
});

// Start
init();
