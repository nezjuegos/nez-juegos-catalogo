"""
Ualá Bis API v2 Integration
Handles OAuth2 authentication and checkout creation.
Docs: https://developers.ualabis.com.ar/v2
"""
import requests
import os
import logging
import time

logger = logging.getLogger(__name__)

# Surcharge: 4.9% commission + 21% IVA on commission = 5.929% total
UALA_SURCHARGE_RATE = 0.05929


def calcular_precio_con_uala(precio_base):
    """Calculate price with Ualá Bis surcharge.
    
    Example: for $35,000 base:
    - Commission (4.9%): $1,715
    - IVA on commission (21% of $1,715): $360
    - Total surcharge: $2,075
    - Client pays: $37,075
    """
    surcharge = round(precio_base * UALA_SURCHARGE_RATE)
    return precio_base + surcharge, surcharge


class UalaBis:
    def __init__(self):
        env = os.getenv('UALA_ENV', 'stage')
        if env == 'prod':
            self.auth_url = 'https://auth.developers.ar.ua.la/v2/api/auth/token'
            self.checkout_url = 'https://checkout.developers.ar.ua.la/v2/api/checkout'
        else:
            self.auth_url = 'https://auth.stage.developers.ar.ua.la/v2/api/auth/token'
            self.checkout_url = 'https://checkout.stage.developers.ar.ua.la/v2/api/checkout'
        
        self.username = os.getenv('UALA_USERNAME', '')
        self.client_id = os.getenv('UALA_CLIENT_ID', '')
        self.client_secret = os.getenv('UALA_CLIENT_SECRET', '')
        self._token = None
        self._token_expiry = 0

    def get_token(self):
        """Get OAuth2 access token. Reuses cached token if still valid."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        try:
            resp = requests.post(self.auth_url, json={
                "username": self.username,
                "client_id": self.client_id,
                "client_secret_id": self.client_secret,
                "grant_type": "client_credentials"
            }, headers={'Content-Type': 'application/json'}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self._token = data['access_token']
            expires_in = data.get('expires_in', 3600)
            self._token_expiry = time.time() + expires_in
            return self._token
        except Exception as e:
            logger.error(f"Ualá Bis auth error: {e}")
            raise

    def create_checkout(self, amount, description, external_ref, base_url):
        """Create a checkout order in Ualá Bis.
        
        Args:
            amount: Amount in ARS (integer or string, e.g. "37075")
            description: Order description
            external_ref: Our internal reference (e.g. "nez-123")
            base_url: Base URL of our site (e.g. "https://nezjuegos.com")
        
        Returns:
            dict with keys: uuid, amount, status, links.checkout_link
        """
        token = self.get_token()
        
        try:
            resp = requests.post(self.checkout_url, json={
                "amount": str(amount),
                "description": description,
                "callback_success": f"{base_url}/checkout?status=success&ref={external_ref}",
                "callback_fail": f"{base_url}/checkout?status=fail&ref={external_ref}",
                "notification_url": f"{base_url}/api/uala/webhook",
                "external_reference": external_ref
            }, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Ualá Bis checkout error: {e}")
            raise
