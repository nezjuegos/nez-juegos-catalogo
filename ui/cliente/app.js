const API_URL = '/api';
const WHATSAPP_NUMBER = '5491160120337';

// DOM Elements
const gameInput = document.getElementById('game-input');
const excludeInput = document.getElementById('exclude-input');
const priceMin = document.getElementById('price-min');
const priceMax = document.getElementById('price-max');
const searchBtn = document.getElementById('search-btn');
const resultsGrid = document.getElementById('results-grid');
const resultsCount = document.getElementById('results-count');
const toast = document.getElementById('toast');

// Store all packs
let allPacks = [];

// --- Initialization ---
function init() {
    loadAllPacks();
}

// --- Load All Packs ---
async function loadAllPacks() {
    try {
        const res = await fetch(`${API_URL}/search?q=&limit=1000`, { cache: "no-store" });
        const data = await res.json();
        allPacks = data.results || [];
        renderResults(allPacks);
    } catch (e) {
        showError('No se pudo cargar el catálogo. Intenta recargar la página.');
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
    searchBtn.textContent = 'Buscando...';

    // Show skeleton
    resultsGrid.innerHTML = `
        <div class="loading-skeleton">
            <div class="skeleton-card"></div>
            <div class="skeleton-card"></div>
        </div>
    `;

    try {
        let url = `${API_URL}/search?q=${encodeURIComponent(query)}&exclude=${encodeURIComponent(exclude)}&limit=1000`;
        if (minPrice) url += `&price_min=${minPrice}`;
        if (maxPrice) url += `&price_max=${maxPrice}`;

        const res = await fetch(url, { cache: "no-store" });
        const data = await res.json();

        renderResults(data.results || [], query, exclude);
    } catch (e) {
        showError('Error al buscar. Intenta de nuevo.');
    } finally {
        searchBtn.disabled = false;
        searchBtn.textContent = 'Buscar Packs';
    }
}

// Pagination State
let currentPage = 1;
const ITEMS_PER_PAGE = 20;
let currentFilteredPacks = [];

// --- Render Results (with Pagination) ---
function renderResults(packs, query = '', exclude = '') {
    // Reset pagination on new search
    if (query !== window.lastQuery || exclude !== window.lastExclude) {
        currentPage = 1;
        window.lastQuery = query;
        window.lastExclude = exclude;
        resultsGrid.innerHTML = '';
        currentFilteredPacks = packs;
    }

    // Update count title
    let countText = 'Catálogo Completo';
    if (query || exclude) {
        countText = `${packs.length} resultados encontrados`;
    }
    resultsCount.textContent = countText;

    if (packs.length === 0) {
        resultsGrid.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🔍</div>
                <h3>No encontramos nada</h3>
                <p>Intenta cambiar los términos de búsqueda</p>
            </div>
        `;
        return;
    }

    // Slice packs for pagination
    const start = (currentPage - 1) * ITEMS_PER_PAGE;
    const end = start + ITEMS_PER_PAGE;
    const pagePacks = packs.slice(start, end);

    // Append packs (don't clear grid if loading more)
    if (currentPage === 1) resultsGrid.innerHTML = '';

    pagePacks.forEach(pack => {
        const card = document.createElement('div');
        card.className = 'pack-card';

        const gamesDisplay = pack.games.join('\n');
        // If cover_url is missing or empty, use a transparent pixel and handle with CSS or JS fallback
        const coverUrl = pack.cover_url;

        const whatsappUrl = buildWhatsAppUrl(pack.id);
        const formattedText = pack.formatted_text.replace(/`/g, '\\`').replace(/\$/g, '$$');

        // Logic for handling missing images:
        // We use a specific class 'no-cover' if URL is missing to show a nice pattern instead
        // Add cache-busting param to force reload if image changed
        const imgUrlWithCache = coverUrl && !coverUrl.startsWith('data:')
            ? `${coverUrl}?t=${new Date().getTime()}` // Force refresh
            : coverUrl;

        const imgHtml = coverUrl
            ? `<img class="pack-cover" src="${imgUrlWithCache}" alt="Pack Cover" loading="lazy" onerror="this.onerror=null; this.parentElement.classList.add('no-cover');">`
            : '';

        const noCoverClass = !coverUrl ? 'no-cover' : '';

        card.innerHTML = `
            <div class="card-image-wrapper ${noCoverClass}">
                ${imgHtml}
                <div class="fallback-pattern">
                    <span class="fallback-icon">🎮</span>
                </div>
                <div class="price-badge">$${pack.price_local.toLocaleString('es-AR')}</div>
            </div>
            
            <div class="pack-body">
                <div class="pack-meta">
                    <span class="pack-id">ID: ${pack.id}</span>
                </div>
                
                <div class="pack-games">${gamesDisplay}</div>
                
                <div class="pack-actions">
                    <button class="btn-copy" onclick="copyToClipboard(this, \`${formattedText}\`)">
                        <span class="btn-icon">📋</span> Copiar
                    </button>
                    <a class="btn-whatsapp" href="${whatsappUrl}" target="_blank">
                       <svg class="whatsapp-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512"><!--!Font Awesome Free 6.5.1 by @fontawesome - https://fontawesome.com License - https://fontawesome.com/license/free Copyright 2024 Fonticons, Inc.--><path d="M380.9 97.1C339 55.1 283.2 32 223.9 32c-122.4 0-222 99.6-222 222 0 39.1 10.2 77.3 29.6 111L0 480l117.7-30.9c32.4 17.7 68.9 27 106.1 27h.1c122.3 0 224.1-99.6 224.1-222 0-59.3-25.2-115-67.1-157zm-157 341.6c-33.2 0-65.7-8.9-94-25.7l-6.7-4-69.8 18.3L72 359.2l-4.4-7c-18.5-29.4-28.2-63.3-28.2-98.2 0-101.7 82.8-184.5 184.6-184.5 49.3 0 95.6 19.2 130.4 54.1 34.8 34.9 56.2 81.2 56.1 130.5 0 101.8-84.9 184.6-186.6 184.6zm101.2-138.2c-5.5-2.8-32.8-16.2-37.9-18-5.1-1.9-8.8-2.8-12.5 2.8-3.7 5.6-14.3 18-17.6 21.8-3.2 3.7-6.5 4.2-12 1.4-32.6-16.3-54-29.1-75.5-66-5.7-9.8 5.7-9.1 16.3-30.3 1.8-3.7 .9-6.9-.5-9.7-1.4-2.8-12.5-30.1-17.1-41.2-4.5-10.8-9.1-9.3-12.5-9.5-3.2-.2-6.9-.2-10.6-.2-3.7 0-9.7 1.4-14.8 6.9-5.1 5.6-19.4 19-19.4 46.3 0 27.3 19.9 53.7 22.6 57.4 2.8 3.7 39.1 59.7 94.8 83.8 35.2 15.2 49 16.5 66.6 13.9 10.7-1.6 32.8-13.4 37.4-26.4 4.6-13 4.6-24.1 3.2-26.4-1.3-2.5-5-3.9-10.5-6.6z"/></svg>
                       Pedir
                    </a>
                </div>
            </div>
        `;
        resultsGrid.appendChild(card);
    });

    // Handle "Load More" button
    const existingBtn = document.getElementById('load-more-btn');
    if (existingBtn) existingBtn.remove();

    if (end < packs.length) {
        const loadMoreBtn = document.createElement('button');
        loadMoreBtn.id = 'load-more-btn';
        loadMoreBtn.className = 'btn-secondary btn-block';
        loadMoreBtn.textContent = 'Ver más packs...';
        loadMoreBtn.onclick = () => {
            currentPage++;
            renderResults(currentFilteredPacks, query, exclude);
        };
        resultsGrid.appendChild(loadMoreBtn);
    }
}

function showError(msg) {
    resultsGrid.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">⚠️</div>
            <h3>Algo salió mal</h3>
            <p>${msg}</p>
        </div>
    `;
}

// --- Build WhatsApp URL ---
function buildWhatsAppUrl(packId) {
    const message = `Hola! Quiero consultarte si el pack de ID ${packId} está disponible, gracias!`;
    return `https://wa.me/${WHATSAPP_NUMBER}?text=${encodeURIComponent(message)}`;
}

// --- Copy to Clipboard ---
function copyToClipboard(btn, text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast();
        btn.classList.add('copied');
        setTimeout(() => btn.classList.remove('copied'), 2000);
    });
}

// --- Show Toast ---
function showToast() {
    toast.classList.remove('hidden');
    setTimeout(() => toast.classList.add('hidden'), 3000);
}

// --- Event Listeners ---
gameInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') performSearch();
});

excludeInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') performSearch();
});

// Start
init();
