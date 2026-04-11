# Google Tag Manager Setup Guide — Nez Juegos

## ✅ Implementación Completada

GTM ya está integrado en tu sitio. Aquí está lo que hicimos:

### Lo que se creó:

1. **`ui/gtm.js`** — Script de inicialización de GTM
   - Se inyecta automáticamente en todas las páginas HTML
   - Carga la etiqueta GTM de forma asíncrona (no ralentiza el sitio)
   - Proporciona funciones helper para rastrear eventos

2. **Inyección automática en `server.py`**
   - La función `inject_gtm_script()` agrega GTM a cada página HTML servida
   - Se inserta antes de `</head>` para cargar rápido
   - Funciona en todas las páginas públicas y admin

3. **Ruta pública `/gtm.js`**
   - Permite que el navegador descargue el script de inicialización

---

## 🔧 Configuración Necesaria

### Paso 1: Crear Cuenta en Google Tag Manager

1. Ve a https://tagmanager.google.com
2. **Sign in** con tu cuenta Google
3. Haz clic en **"Create Account"**
   - **Account name**: "Nez Juegos"
   - **Country**: Argentina
   - Acepta los términos de servicio

### Paso 2: Crear Contenedor

1. **Container name**: "nezjuegos.com"
2. **Target platform**: Web
3. Haz clic en **"Create"**

### Paso 3: Obtener tu GTM Container ID

Google te mostrará algo como `GTM-XXXXXX` (ejemplo: `GTM-ABC1234`)

**Copia este ID.**

### Paso 4: Configurar en tu sitio

Abre `ui/gtm.js` y reemplaza:

```javascript
const GTM_ID = 'GTM-XXXXXX'; // ← UPDATE THIS WITH YOUR GTM CONTAINER ID
```

Con tu ID real. Ejemplo:

```javascript
const GTM_ID = 'GTM-ABC1234';
```

### Paso 5: Deploy

```bash
git add ui/gtm.js backend/server.py
git commit -m "Implement Google Tag Manager"
git push origin main
```

Railway hará el deploy automáticamente en unos minutos.

---

## 📊 Funciones Disponibles para Rastrear Eventos

El script proporciona helpers listos para usar en tu JavaScript:

### 1. Rastrear Vistas de Página
```javascript
// Se hace automáticamente, pero puedes forzar manualmente:
window.trackPageView("Nombre de la Página", { custom_param: "value" });
```

### 2. Rastrear Visualización de Producto
```javascript
window.trackProductView(
  productId,       // ID único del juego
  productName,     // "Mario Kart 8 Deluxe"
  productCategory, // "Nintendo Switch"
  price            // 3500 (en ARS)
);
```

### 3. Rastrear Agregar al Carrito
```javascript
window.trackAddToCart(
  productId,
  productName,
  quantity,  // cantidad
  price      // precio unitario
);
```

### 4. Rastrear Compra
```javascript
window.trackPurchase(
  orderId,        // "nez-12345"
  totalAmount,    // 3500
  items: [        // array de items comprados
    {
      item_id: "game-1",
      item_name: "Mario Kart 8",
      quantity: 1,
      price: 3500
    }
  ]
);
```

### 5. Rastrear Evento Personalizado
```javascript
window.trackEvent('custom_event_name', {
  param1: 'value1',
  param2: 'value2'
});
```

### 6. Rastrear Envío de Formulario
```javascript
window.trackFormSubmit('form_name', {
  additional_param: 'value'
});
```

---

## 🎯 Dónde Usar Estos Helpers

### En `ui/index.html` o `ui/juegos.html` (cuando se ve un juego):
```javascript
// Al cargar la página con un juego visible
window.trackProductView(gameId, gameName, 'Nintendo Switch', price);
```

### En `ui/checkout.html` (cuando se agrega al carrito):
```javascript
// Cuando el usuario hace clic en "Comprar"
window.trackAddToCart(gameId, gameName, 1, price);
```

### En `ui/success.html` (después de la compra):
```javascript
// Después de que se confirma el pago
window.trackPurchase(orderId, totalAmount, itemsArray);
```

---

## 📋 Próximos Pasos en GTM

Una vez que GTM esté funcionando en tu sitio:

1. **Configura Google Analytics 4 (GA4)**
   - En GTM: Click en **"Tags"** → **"New"**
   - Elige template: **"Google Analytics: GA4 Configuration"**
   - Ingresa tu **Measurement ID** de GA4
   - Trigger: **"All Pages"**

2. **Configura Conversiones**
   - En GTM: Crear tags para eventos de compra
   - En GA4: Definir qué eventos son conversiones

3. **Configura Anuncios (Ads)**
   - Meta Pixel (ya lo tienes)
   - Google Ads Conversion Tracking
   - LinkedIn Pixel (si lo necesitas)

4. **Prueba en Preview Mode**
   - En GTM: Haz clic en **"Preview"**
   - Navega tu sitio y verifica que los tags se activen
   - Ve a **"Debug"** tab en GTM para ver los eventos enviados

---

## ✅ Verificación

Para verificar que GTM está funcionando:

1. Abre https://nezjuegos.com en el navegador
2. Abre DevTools (F12)
3. Ve a **Console**
4. Deberías ver: `✅ Google Tag Manager initialized with ID: GTM-XXXXXX`

Si lo ves, ¡GTM está corriendo!

---

## 🆘 Solución de Problemas

**Problema**: "❌ GTM no aparece en la consola"
- Asegúrate de que reemplazaste `GTM-XXXXXX` con tu ID real
- Verifica que hiciste `git push` y que Railway completó el deploy

**Problema**: "Veo script errors en la consola"
- Revisa que `gtm.js` está siendo servido (F12 → Network → busca `gtm.js`)
- Si no aparece, verifica que la ruta `/gtm.js` está configurada en `server.py`

**Problema**: "Los eventos no se registran en GA4"
- Verifica que creaste el tag de GA4 en GTM
- En GA4, ve a **Admin → Tags → Tag Assistants** para debuggear
- Espera 24 horas para ver datos en GA4 (tienen lag)

---

**¿Preguntas?** Revisa la [documentación oficial de GTM](https://support.google.com/tagmanager)
