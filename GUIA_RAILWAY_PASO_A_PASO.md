# Guía Definitiva: Cómo subir la App a Railway paso a paso

Dado que ya tienes tu cuenta de Railway y tu dominio en Cloudflare, el proceso es mucho más rápido. Aquí vamos:

## PASO 1: Subir el código a GitHub

Railway necesita leer el código desde GitHub. Vamos a subirlo:

1. Abre GitHub y haz clic en el botón verde **"New"** (Nuevo repositorio).
2. Ponle un nombre, por ejemplo: `tienda-nintendo-bot`.
3. Selecciona **Private** (Privado) para que nadie más vea tu código.
4. Haz clic en **"Create repository"**.
5. Ahora, en tu computadora, abre una terminal en la carpeta principal del proyecto (donde están todos los archivos) y pega estos comandos uno a uno:
   
   ```bash
   git init
   git add .
   git commit -m "Primera version"
   git branch -M main
   # Este es el comando exacto para tu repositorio:
   git remote add origin https://github.com/nezjuegos/nez-juegos-catalogo.git
   git push -u origin main
   ```
   *(Nota: Yo ya configuré tu proyecto para que no suba carpetas pesadas ni cosas que no hacen falta).*

---

## PASO 2: Conectar GitHub a Railway

1. Entra a tu panel de **Railway** (railway.app/dashboard).
2. Haz clic en **"New Project"**.
3. Selecciona **"Deploy from GitHub repo"**.
4. Busca y selecciona el repositorio `tienda-nintendo-bot` que acabas de subir.
5. **¡Importante!** Railway intentará instalarlo ahora mismo. Va a fallar la primera vez porque nos falta configurar el disco en la nube (Volume). Déjalo fallar, es normal.

---

## PASO 3: Crear el Disco Persistente (Volume)

Para que Telegram no te pida el código QR cada vez que se reinicie la app, necesitamos un disco duro virtual que guarde tus datos.

1. En el panel de Railway, dentro de tu nuevo proyecto, haz clic en el botón superior derecho **"New"** (o en el botón "+").
2. Selecciona **"Volume"**.
3. Ponle un nombre, por ejemplo: `app-data`.
4. Railway lo creará. Ahora haz clic sobre el bloque cuadrado de tu aplicación (el que tiene tu código de GitHub).
5. Ve a la pestaña superior llamada **Variables**. Haz clic en "New Variable":
   - **VARIABLE NAME:** `RAILWAY_VOLUME_MOUNT_PATH`
   - **VALUE:** `/data`
6. Ahora ve a la pestaña superior derecha llamada **Settings**. 
7. Desplaza hacia abajo hasta la sección **Volumes**.
   - Haz clic en "Mount Volume".
   - Selecciona el volumen `app-data` que acabas de crear.
   - En la cajita de **"Mount Path"**, escribe `/data`.
   
*Listo. Ahora tu código sabe que la carpeta /data nunca se borra.*

---

## PASO 4: Transferir la Sesión de Telegram (Lo más crítico)

Aquí es donde subimos tu cuenta logueada de Telegram para que el robot funcione solo.

1. En tu compu, entra a la carpeta del proyecto y busca la carpeta `browser_data_clean`. ¡Ahí está tu sesión de Telegram!
2. Da clic derecho sobre `browser_data_clean` y comprímelo en un archivo `.zip`. Llámalo `sesion.zip` (es muy importante que el interior del zip contenga los archivos de frente, o la carpeta misma).
3. **Sube el archivo `sesion.zip` a tu repositorio de GitHub** (puedes simplemente arrastrarlo a la web de tu repositorio en GitHub y guardar los cambios / Commit).
4. Vuelve a Railway. Al detectar un cambio en GitHub, empezará a actualizarse solo.
5. **El truco mágico:** Hemos configurado el código para que, si encuentra `sesion.zip`, lo extraiga automáticamente en tu disco duro virtual (`/data`) y el robot inicie sesión por ti.

---

## PASO 5: Conectar tu Dominio (Cloudflare)

Finalmente, conectemos `nezjuegos.com`.

1. En Railway, haz clic nuevamente en el bloque cuadrado de tu app.
2. Ve a la pestaña **Settings**.
3. Baja a la sección **Networking** y haz clic en **"Generate Domain"** (Este es un dominio temporal que exige Railway).
4. Inmediatamente debajo, haz clic en **"Custom Domain"**. 
5. Escribe `nezjuegos.com` y dale a Enter.
6. Railway te mostrará un mensaje indicando cómo configurar el DNS (algo como Tipo CNAME, o una dirección tuya como `xxxxxx.up.railway.app`). Copia esa dirección.
7. Entra a tu cuenta de **Cloudflare** y selecciona tu dominio.
8. Ve a la columna izquierda y elige **DNS -> Registros**.
9. Agrega un registro nuevo:
   - **Tipo:** `CNAME`
   - **Nombre:** `@`
   - **Objetivo:** Pega la dirección larga que copiaste de Railway.
   - **Estado del proxy:** Déjalo encendido (nube naranja).
10. ¡Guarda! En unos pocos minutos, tu app estará visible para el mundo entero en `nezjuegos.com`.

¡Esos son, puntillosos, los pasos totales! Cualquier paso en el que te trabes, avísame.
