# Google Tag Manager - Configuración de Eventos en Nez Juegos

## 📋 Resumen de Eventos a Rastrear

| Evento | Dónde Ocurre | ID del Elemento | Acción en GTM |
|--------|-------------|-----------------|----------------|
| **initiate_checkout** | Botón "🛒 Comprar" en juegos | `.btn-checkout` | Track cuando hace clic |
| **lead** | Botón "📞 Pedir por WhatsApp" | `.btn-wa-small` | Track cuando hace clic |
| **view_item** | Abre página de juego individual | `/juegos/<slug>` | Track automático |
| **purchase** | Completa el checkout | Página `/mp/success` o `/uala/success` | Track al cargar |

---

## 🎯 Paso 1: Crear Variables en GTM

### Paso 1.1: Ir a Google Tag Manager

1. Ve a https://tagmanager.google.com
2. Selecciona tu contenedor **GTM-KRBGTMQ4**
3. En el menú izquierdo, haz clic en **"Variables"**

### Paso 1.2: Crear Variable para Nombre del Juego

1. Haz clic en **"New"** (botón azul)
2. Selecciona tipo: **"Data Layer Variable"**
3. Configura:
   - **Variable Configuration → Data Layer Variable Name**: `game_name`
   - **Variable name**: "Game Name"
4. Haz clic en **"Save"**

### Paso 1.3: Crear Variable para Precio

1. Haz clic en **"New"** nuevamente
2. Tipo: **"Data Layer Variable"**
3. Configura:
   - **Data Layer Variable Name**: `game_price`
   - **Variable name**: "Game Price"
4. Haz clic en **"Save"**

### Paso 1.4: Crear Variable para URL

1. **"New"** → Tipo: **"Page URL"**
   - **Variable name**: "Current Page URL"
   - Guarda

---

## 🏷️ Paso 2: Crear Disparadores (Triggers)

### Paso 2.1: Trigger para Clic en "Comprar"

1. Ve a **"Triggers"** en el menú izquierdo
2. Haz clic en **"New"**
3. Configura:
   - **Trigger Configuration → Trigger Type**: Selecciona **"Click - All Elements"**
   - **This trigger fires on**: Selecciona **"Some Clicks"**
   - En el dropdown, selecciona:
     - **Click Classes** | **contains** | **btn-checkout**
   - **Trigger name**: "Click - Buy Button"
4. Haz clic en **"Save"**

### Paso 2.2: Trigger para Clic en "Pedir por WhatsApp"

1. **"New"** → **"Click - All Elements"**
2. Configura:
   - **This trigger fires on**: **"Some Clicks"**
   - **Click Classes** | **contains** | **btn-wa-small**
   - **Trigger name**: "Click - WhatsApp Button"
3. **"Save"**

### Paso 2.3: Trigger para Ver Página de Juego Individual

1. **"New"** → Type: **"Page View"**
2. Configura:
   - **This trigger fires on**: **"Some Page Views"**
   - **Page URL** | **matches RegEx** | `/juegos/.*`
   - **Trigger name**: "View - Individual Game Page"
3. **"Save"**

### Paso 2.4: Trigger para Compra Completada

1. **"New"** → Type: **"Page View"**
2. Configura:
   - **This trigger fires on**: **"Some Page Views"**
   - **Page URL** | **matches RegEx** | `(mp/success|uala/success)`
   - **Trigger name**: "View - Purchase Success Page"
3. **"Save"**

---

## 🏷️ Paso 3: Crear Tags en GTM

### Paso 3.1: Tag para "Initiate Checkout" (Clic en Comprar)

1. Ve a **"Tags"** en el menú izquierdo
2. Haz clic en **"New"**
3. Configura:
   - **Tag Configuration → Choose tag type**: **"Google Analytics: GA4 Event"**
   - **Measurement ID**: Tu ID de GA4 (búscalo en Google Analytics 4)
   - **Event name**: `initiate_checkout`
   - **User Properties** (opcional): agrega si quieres
     - Key: `game_name`
     - Value: `{{Game Name}}`
   - **Triggering → Choose a trigger to fire this tag**: **"Click - Buy Button"**
   - **Tag name**: "GA4 - Initiate Checkout"
4. Haz clic en **"Save"**

### Paso 3.2: Tag para "Lead" (Clic en WhatsApp)

1. **"New"**
2. Configura:
   - **Tag Type**: **"Google Analytics: GA4 Event"**
   - **Measurement ID**: Tu ID de GA4
   - **Event name**: `lead`
   - **Trigger**: **"Click - WhatsApp Button"**
   - **Tag name**: "GA4 - Lead (WhatsApp)"
3. **"Save"**

### Paso 3.3: Tag para "View Item" (Página de Juego Individual)

1. **"New"**
2. Configura:
   - **Tag Type**: **"Google Analytics: GA4 Event"**
   - **Measurement ID**: Tu ID de GA4
   - **Event name**: `view_item`
   - **Event Parameters**:
     - Key: `page_url`
     - Value: `{{Current Page URL}}`
   - **Trigger**: **"View - Individual Game Page"**
   - **Tag name**: "GA4 - View Item (Game Page)"
3. **"Save"**

### Paso 3.4: Tag para "Purchase" (Compra Completada)

1. **"New"**
2. Configura:
   - **Tag Type**: **"Google Analytics: GA4 Event"**
   - **Measurement ID**: Tu ID de GA4
   - **Event name**: `purchase`
   - **Event Parameters**:
     - Key: `source`
     - Value: `payment_gateway`
   - **Trigger**: **"View - Purchase Success Page"**
   - **Tag name**: "GA4 - Purchase"
3. **"Save"**

---

## 🔍 Paso 4: Probar en Preview Mode

1. En GTM, haz clic en el botón **"Preview"** (arriba a la derecha)
2. Abre https://nezjuegos.com en otra pestaña
3. Navega en tu sitio:
   - Abre un juego individual → Deberías ver "View Item" activarse
   - Haz clic en "🛒 Comprar" → Deberías ver "Initiate Checkout"
   - Haz clic en "📞 Pedir" → Deberías ver "Lead"
4. En GTM Preview, deberías ver todos los eventos listados

---

## ✅ Paso 5: Publicar en Producción

Una vez que todo esté probado:

1. En GTM, haz clic en **"Submit"** (arriba a la derecha)
2. Ingresa un nombre de versión: "v1 - Initial Events Setup"
3. Ingresa descripción: "Added initiate_checkout, lead, view_item, purchase events"
4. Haz clic en **"Publish"**

¡Listo! GTM ahora está rastreando eventos en vivo.

---

## 📊 Ver Datos en Google Analytics 4

Después de publicar:

1. Ve a https://analytics.google.com
2. Selecciona tu propiedad Nez Juegos
3. Ve a **"Events"** en el menú izquierdo
4. Deberías ver tus eventos listados (puede tardar 24-48 horas para datos completos)
5. Para verificar en tiempo real:
   - Ve a **"Realtime"** → **"Event Count by Event Name"**
   - Navega tu sitio y verás eventos registrándose en vivo

---

## 🎯 Próximos Pasos Avanzados (Opcional)

Una vez que los eventos funcionen:

1. **Crear Conversiones en GA4**:
   - Mark `initiate_checkout` como conversión
   - Mark `purchase` como conversión (la más importante)

2. **Conectar Meta Pixel a GTM**
   - En GTM, crear tag de Meta Pixel
   - Rastrear `purchase` evento hacia Meta Pixel
   - Esto ayuda con retargeting en Facebook

3. **Crear Análisis Personalizados**
   - En GA4, crear Exploración (Exploration)
   - Analizar qué juegos generan más leads/compras
   - Ver embudo completo: Vista → Lead → Compra

---

## 🆘 Si Algo No Funciona

**Los eventos no aparecen en GTM Preview:**
- Verifica que los triggers tengan las clases CSS correctas (`.btn-checkout`, `.btn-wa-small`)
- Abre DevTools (F12) y busca los elementos para confirmar las clases
- Revisa la consola para mensajes de error

**Los eventos no aparecen en GA4 después de 24h:**
- Verifica que tu Measurement ID es correcto
- En GA4 → Admin → Data Collection, verifica que el evento está activado
- Va a Analytics Debugger (extensión de Chrome) para debug en tiempo real

---

¡Éxito con GTM! 🚀
