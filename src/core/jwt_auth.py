"""
Autenticación JWT para Coinbase Advanced Trade API v3.

CORREGIDO P0: uri va en payload (no headers), formato "<METHOD> <HOST><PATH>"
"""

import time
import uuid
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives import serialization


@dataclass
class CoinbaseCredentials:
    """Credenciales para Coinbase Advanced Trade API."""

    key_name: str  # organizations/{org_id}/apiKeys/{key_id}
    key_secret: str  # PEM private key


class JWTAuth:
    """
    Generador de JWT para Coinbase Advanced Trade API.

    REST: uri en payload con formato "<METHOD> <HOST><PATH>[?<QUERY>]"
          Ej: "GET api.coinbase.com/api/v3/brokerage/accounts"

    WS: sin uri

    kid y sub: key_name completo
    """

    def __init__(
        self,
        credentials: CoinbaseCredentials,
        issuer: str = "cdp",
        expiry_seconds: int = 120,
    ):
        self.credentials = credentials
        self.issuer = issuer
        self.expiry_seconds = expiry_seconds

        # Cargar clave privada desde PEM
        self.private_key = serialization.load_pem_private_key(
            credentials.key_secret.encode(),
            password=None,
        )

    def generate_rest_jwt(
        self,
        method: str,
        path: str,
    ) -> str:
        """
        Generar JWT para llamada REST.

        CORREGIDO P0: uri va en payload, no en headers.
        Formato: "<METHOD> <HOST><PATH>[?<QUERY>]"

        Args:
            method: HTTP method (GET, POST, etc.)
            path: Path del endpoint (ej: /api/v3/brokerage/accounts)

        Returns:
            JWT token string
        """
        now = int(time.time())

        # CORREGIDO: uri en payload, no en headers
        uri = f"{method} api.coinbase.com{path}"

        headers = {
            "alg": "ES256",
            "kid": self.credentials.key_name,
            "nonce": str(uuid.uuid4()),
        }

        payload = {
            "iss": self.issuer,
            "sub": self.credentials.key_name,
            "iat": now,
            "exp": now + self.expiry_seconds,
            "nbf": now,
            "uri": uri,  # CORREGIDO P0: uri en payload
        }

        return jwt.encode(
            payload,
            self.private_key,
            algorithm="ES256",
            headers=headers,
        )

    def generate_ws_jwt(self) -> str:
        """
        Generar JWT para WebSocket.

        NOTA: No incluye uri (a diferencia de REST).
        """
        now = int(time.time())

        headers = {
            "alg": "ES256",
            "kid": self.credentials.key_name,
            "nonce": str(uuid.uuid4()),
        }

        payload = {
            "iss": self.issuer,
            "sub": self.credentials.key_name,
            "iat": now,
            "exp": now + self.expiry_seconds,
            "nbf": now,
        }

        return jwt.encode(
            payload,
            self.private_key,
            algorithm="ES256",
            headers=headers,
        )


def load_credentials_from_env() -> CoinbaseCredentials:
    """Cargar credenciales desde variables de entorno."""
    import os

    key_name = os.getenv("COINBASE_KEY_NAME", "")
    key_secret = os.getenv("COINBASE_KEY_SECRET", "")

    if not key_name or not key_secret:
        raise ValueError("COINBASE_KEY_NAME y COINBASE_KEY_SECRET deben estar configurados")

    return CoinbaseCredentials(
        key_name=key_name,
        key_secret=key_secret,
    )
