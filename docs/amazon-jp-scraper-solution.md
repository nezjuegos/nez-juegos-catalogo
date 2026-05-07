# Amazon JP Price Scraper — Solución Definitiva

Fecha: Mayo 2026

## Resumen

El tracker de precios de juegos de Nintendo en Amazon Japón utiliza Playwright (Chromium headless) para extraer precios en JPY desde páginas de producto. Este documento registra la solución que funcionó correctamente en Railway (entorno de producción).

---

## Stack

- Python 3.12 + Flask + Gunicorn
- Playwright `1.44.0` (sync API)
- Railway (Nixpacks, Ubuntu Noble)
- SQLite con volumen persistente

---

## Problemas encontrados y soluciones

### 1. Build failures en Railway

**Problema**: `libasound2` es un paquete virtual en Ubuntu 24.04 (Noble).
**Solución**: Usar `libasound2t64` en `nixpacks.toml`.

**Problema**: Eliminar el placeholder `"..."` de `cmds` en `nixpacks.toml` rompe el setup del virtualenv Python de Nixpacks.
**Solución**: Siempre mantener `"..."` como primer elemento de `cmds` — Nixpacks lo usa para ejecutar su setup por defecto (crear venv, instalar requirements.txt). Luego agregar comandos adicionales.

```toml
[phases.install]
cmds = [
  "...",
  "python -m playwright install chromium"
]
```

**Problema**: `playwright-stealth==1.0.6` hace `import pkg_resources` que no existe en Python 3.12.
**Solución**: No usar `playwright-stealth`. Implementar stealth manualmente con `add_init_script()`.

---

### 2. Detección de bot (CAPTCHA) en Railway

Railway usa IPs de datacenter que Amazon detecta como bots.

**Solución**: Stealth manual en el contexto de Playwright:

```python
self._context_sync.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
    Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
    window.chrome = {runtime: {}};
    Object.defineProperty(navigator, 'permissions', {
        get: () => ({query: () => Promise.resolve({state: 'granted'})})
    });
""")
```

**Solución adicional**: Cookies para forzar JPY y locale japonés:

```python
self._context_sync.add_cookies([
    {"name": "i18n-prefs", "value": "JPY",   "domain": ".amazon.co.jp", "path": "/"},
    {"name": "lc-main",    "value": "ja_JP", "domain": ".amazon.co.jp", "path": "/"},
])
```

---

### 3. Extracción de precios incorrectos

**Problema**: Escanear toda la página con regex `¥XXXX` captura precios de secciones incorrectas (otros vendedores, productos relacionados, bundles).

**Solución**: Buscar solo dentro del buybox con selectores CSS precisos.

#### JS que funcionó (dentro de `page.evaluate`):

```javascript
// Buybox root
const root = document.querySelector(
    '#corePrice_feature_div, #corePriceDisplay_desktop_feature_div, #apex_desktop'
) || document.body;

// Precio actual (NO tachado)
const offerSelectors = [
    '.reinventPricePriceToPayMargin .a-offscreen',
    '.priceToPay .a-offscreen',
    '#priceblock_ourprice',
    '#priceblock_dealprice',
    '.a-price:not(.a-text-price) > .a-offscreen',
];

// Precio de lista (tachado)
const listSelectors = [
    '.basisPrice .a-offscreen',
    '.a-price.a-text-price .a-offscreen',
    '#priceblock_listprice',
];
```

**Regla clave**: `.a-offscreen` dentro de `.a-text-price` = precio tachado (lista). `.a-offscreen` fuera de `.a-text-price` = precio actual.

---

### 4. Escape sequences en strings JS dentro de Python

**Problema**: Python 3.12 trata `\s`, `\d`, `\w` como escape sequences inválidas en strings normales.

**Solución**: Usar raw strings para todo código JS pasado a `page.evaluate`:

```python
# MAL - genera SyntaxWarning en Python 3.12
result = page.evaluate("""() => {
    const re = /\s*(\d+)/;
}""")

# BIEN
result = page.evaluate(r"""() => {
    const re = /\s*(\d+)/;
}""")
```

---

### 5. URLs cortas de la app móvil (amzn.asia)

**Solución**: Resolver el redirect antes de extraer el ASIN:

```python
def _resolve_short_amazon_url(self, url, timeout=8):
    if any(h in url.lower() for h in ("amzn.asia", "amzn.to")):
        r = self.session.get(url, timeout=timeout, allow_redirects=True)
        return r.url
    return url
```

---

## Lecciones aprendidas

1. **No hacer scans globales de HTML** para buscar precios — hay demasiado ruido (otros vendedores, recomendados, etc.)
2. **Los selectores del buybox son estables**: `#corePrice_feature_div`, `.reinventPricePriceToPayMargin`, `.a-offscreen`
3. **Railway cachea builds** — siempre verificar que el deploy que está corriendo es el último commit antes de diagnosticar
4. **Validar `scraper.py` localmente** antes de cada push: `python -W error -c "import py_compile; py_compile.compile('backend/scraper.py', doraise=True)"`
5. **Un cambio por commit** — facilita identificar qué rompió qué
6. **`playwright-stealth` no funciona en Python 3.12 con Nixpacks** — implementar stealth manualmente

---

## Configuración final que funciona

### `requirements.txt`
```
Flask==3.0.0
playwright==1.44.0
gunicorn==21.2.0
requests==2.31.0
```

### `nixpacks.toml`
```toml
providers = ["python"]

[phases.setup]
aptPkgs = [
  "libnss3", "libnspr4", "libatk1.0-0", "libatk-bridge2.0-0",
  "libcups2", "libdrm2", "libxkbcommon0", "libatspi2.0-0",
  "libxcomposite1", "libxdamage1", "libxfixes3", "libxrandr2",
  "libgbm1", "libpango-1.0-0", "libcairo2", "libasound2t64",
  "libwayland-client0", "libx11-6", "libx11-xcb1", "libxcb1",
  "libxext6", "libxrender1", "libxtst6", "libglib2.0-0",
  "ca-certificates", "fonts-liberation"
]

[phases.install]
cmds = ["...", "python -m playwright install chromium"]
```
