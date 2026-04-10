"""
Email sender using Resend API.
Sends PlayStation credentials to buyers after payment approval.
"""
import requests
import os
import logging

logger = logging.getLogger(__name__)

RESEND_API_URL = 'https://api.resend.com/emails'


def send_ps_credentials(to_email, game_name, account_email, account_password, activation_key, sale_type='primaria'):
    """Send PlayStation account credentials to buyer via email.
    
    sale_type: 'primaria', 'primaria_ps4', or 'secundaria' — controls which setup instructions to show.
    Returns True if email sent successfully, False otherwise.
    """
    api_key = os.getenv('RESEND_API_KEY', '')
    if not api_key:
        logger.error("RESEND_API_KEY not set")
        return False

    # Build sale-type-specific instructions
    is_secundaria = sale_type == 'secundaria'

    if is_secundaria:
        offline_instruction = """
            <div style="background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); border-radius: 8px; padding: 1rem; margin: 0.75rem 0;">
                <strong style="color: #ef4444;">🚫 NO activar Uso Offline</strong><br>
                <span style="color: #94a3b8; font-size: 0.9rem;">
                    Como tu compra es una cuenta secundaria, <strong style="color: #f8fafc;">NO debes activar el Uso Offline</strong> en tu PS4/PS5. 
                    Activarlo causaría conflictos y perderías el acceso al juego.
                </span>
            </div>
        """
        account_type_label = "Cuenta Secundaria"
        account_type_color = "#eab308"
        user_instruction = "Iniciá sesión con el usuario que te enviamos (abajo) en tu PS4 o PS5."
    else:
        offline_instruction = """
            <div style="background: rgba(34,197,94,0.1); border: 1px solid rgba(34,197,94,0.3); border-radius: 8px; padding: 1rem; margin: 0.75rem 0;">
                <strong style="color: #22c55e;">✅ Activar Uso Offline</strong><br>
                <span style="color: #94a3b8; font-size: 0.9rem;">
                    Como tu compra es una cuenta primaria, <strong style="color: #f8fafc;">debés activar el Uso Offline</strong> en tu PS4/PS5 
                    (Configuración → Usuarios y cuentas → Otra). Esto te permite jugar sin internet desde tu propio usuario.
                </span>
            </div>
        """
        account_type_label = "Cuenta Primaria PS5" if sale_type != 'primaria_ps4' else "Cuenta Primaria PS4"
        account_type_color = "#22c55e"
        user_instruction = "Agregá la cuenta con las credenciales de abajo en tu PS4 o PS5, y luego activá el Uso Offline."

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0a0a0c; color: #f8fafc; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 2rem; }}
            .header {{ text-align: center; padding: 2rem 0 1rem; }}
            .header h1 {{ color: #8b5cf6; margin: 0; font-size: 1.8rem; }}
            .card {{ background: #131318; border: 1px solid #27272a; border-radius: 16px; padding: 2rem; margin: 1.5rem 0; }}
            .type-badge {{ display: inline-block; background: rgba(139,92,246,0.15); border: 1px solid rgba(139,92,246,0.3); border-radius: 20px; padding: 0.25rem 0.75rem; font-size: 0.8rem; font-weight: 700; color: {account_type_color}; margin-bottom: 0.75rem; }}
            .game-name {{ color: #d946ef; font-size: 1.3rem; font-weight: 700; margin-bottom: 1rem; }}
            .credential {{ background: #1a1a24; border: 1px solid #27272a; border-radius: 8px; padding: 1rem; margin: 0.75rem 0; }}
            .credential-label {{ color: #94a3b8; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem; }}
            .credential-value {{ color: #f8fafc; font-size: 1.1rem; font-weight: 600; font-family: 'Courier New', monospace; word-break: break-all; }}
            .steps {{ margin-top: 1.5rem; }}
            .step {{ display: flex; gap: 0.75rem; margin: 0.6rem 0; align-items: flex-start; font-size: 0.9rem; color: #94a3b8; }}
            .step-num {{ background: #8b5cf6; color: white; border-radius: 50%; width: 22px; height: 22px; min-width: 22px; display: flex; align-items: center; justify-content: center; font-size: 0.75rem; font-weight: 700; margin-top: 1px; }}
            .warning {{ background: rgba(234,179,8,0.1); border: 1px solid rgba(234,179,8,0.3); border-radius: 8px; padding: 1rem; margin-top: 1.5rem; color: #eab308; font-size: 0.9rem; }}
            .footer {{ text-align: center; color: #64748b; font-size: 0.8rem; margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #27272a; }}
            .footer a {{ color: #8b5cf6; text-decoration: none; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎮 Nez Juegos</h1>
                <p style="color: #94a3b8; margin-top: 0.5rem;">¡Tu juego está listo!</p>
            </div>
            
            <div class="card">
                <div class="type-badge">{account_type_label}</div>
                <div class="game-name">🎮 {game_name}</div>
                
                <div class="credential">
                    <div class="credential-label">📧 Email de la cuenta</div>
                    <div class="credential-value">{account_email}</div>
                </div>
                
                <div class="credential">
                    <div class="credential-label">🔑 Contraseña</div>
                    <div class="credential-value">{account_password}</div>
                </div>
                
                <div class="credential">
                    <div class="credential-label">🛡️ Clave de activación (un solo uso)</div>
                    <div class="credential-value">{activation_key}</div>
                </div>
                
                <div class="steps">
                    <p style="color: #f8fafc; font-weight: 600; margin-bottom: 0.5rem;">📋 Cómo configurar tu juego:</p>
                    <div class="step">
                        <div class="step-num">1</div>
                        <span>En tu PS4 o PS5, andá a <strong style="color: #f8fafc;">Configuración → Usuarios y cuentas → Agregar usuario</strong>.</span>
                    </div>
                    <div class="step">
                        <div class="step-num">2</div>
                        <span>Creá un nuevo usuario. <strong style="color: #ef4444;">Asegurate de NO agregarlo como invitado</strong> — debe ser un usuario normal con cuenta PSN.</span>
                    </div>
                    <div class="step">
                        <div class="step-num">3</div>
                        <span>Iniciá sesión con el email y contraseña que te enviamos arriba. Usá la clave de activación si te la solicita.</span>
                    </div>
                    <div class="step">
                        <div class="step-num">4</div>
                        <span>{user_instruction}</span>
                    </div>
                </div>

                {offline_instruction}
                
                <div class="warning">
                    ⚠️ <strong>Importante:</strong> La clave de activación es de un solo uso. 
                    No la compartas con nadie. Si tenés algún problema, escribinos por WhatsApp 
                    dentro de las 48 horas.
                </div>
            </div>
            
            <div class="footer">
                <p>Gracias por elegirnos 💜</p>
                <p><a href="https://nezjuegos.com">nezjuegos.com</a> · 
                   <a href="https://wa.me/5491160120337">WhatsApp</a></p>
                <p style="margin-top: 0.5rem;">+2000 ventas · De gamers para gamers</p>
            </div>
        </div>
    </body>
    </html>
    """

    try:
        resp = requests.post(RESEND_API_URL, json={
            "from": "Nez Juegos <pedidos@nezjuegos.com>",
            "to": [to_email],
            "subject": f"🎮 Tu juego está listo: {game_name} — Nez Juegos",
            "html": html
        }, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }, timeout=15)
        
        if resp.status_code in (200, 201):
            logger.info(f"Email sent to {to_email} for {game_name} ({sale_type})")
            return True
        else:
            logger.error(f"Resend error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Email send error: {e}")
        return False
