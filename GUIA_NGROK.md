# Guía: Configurar Dominio Estático en Ngrok (Gratis / Lite)

Esta guía te ayudará a eliminar los enlaces largos y aleatorios de Ngrok y usar un nombre de dominio fijo y profesional.

## Pasos

1.  **Reclama tu dominio**
    *   Ve al [Dashboard de Ngrok](https://dashboard.ngrok.com/cloud-edge/domains) e inicia sesión.
    *   Haz clic en **Cloud Edge** -> **Domains**.
    *   Haz clic en **"+ Create Domain"**.
    *   Ngrok te asignará un dominio gratuito (ej: `tienda-juegos.ngrok-free.app`). O puedes probar escribir uno.
    *   **Copia ese nombre de dominio**.

2.  **Configura tu inicio**
    *   Abre la carpeta de tu proyecto.
    *   Crea un archivo llamado `iniciar_con_dominio.bat` (clic derecho -> nuevo archivo de texto -> cambiar nombre).
    *   Edítalo con el Bloc de Notas y pega lo siguiente (reemplaza `TU-DOMINIO` por el tuyo):

```batch
@echo off
echo Iniciando Servidor...
start cmd /k ".\venv\Scripts\python server.py"
timeout /t 5
echo Iniciando Ngrok con dominio fijo...
ngrok http --url=TU-DOMINIO-AQUI.ngrok-free.app 5000
pause
```

3.  **¡Listo!**
    *   Ahora, cada vez que quieras abrir la tienda, solo haz doble clic en `iniciar_con_dominio.bat`.
    *   El link será siempre el mismo.
