"""
Cliente REST para Coinbase Advanced Trade API v3.

CORREGIDO P0:
  - Parsing de respuestas de órdenes (wrappers correctos)
  - Create Order: success_response.order_id
  - Get Order: response["order"]
"""
import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlencode

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from src.core.jwt_auth import JWTAuth, CoinbaseCredentials

logger = logging.getLogger("CoinbaseREST")


def _is_retryable(exc: Exception) -> bool:
    """Determinar si un error es retryable."""
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    if isinstance(exc, CoinbaseAPIError):
        # Solo reintentar: rate limit (429), server errors (5xx), o sin status
        return exc.status_code in (429, 500, 502, 503, 504, None)
    return False


class CoinbaseAPIError(Exception):
    """Error de API de Coinbase."""
    
    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class CoinbaseRESTClient:
    """
    Cliente REST para Coinbase Advanced Trade API v3.
    
    CORREGIDO P0: Parsing de respuestas de órdenes con wrappers correctos.
    """
    
    BASE_URL = "https://api.coinbase.com"
    BROKERAGE_PATH = "/api/v3/brokerage"
    
    def __init__(
        self,
        jwt_auth: JWTAuth,
        timeout: float = 30.0,
        max_retries: int = 5,
    ):
        self.jwt_auth = jwt_auth
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        
        # Rate limiting state
        self._last_request_time = 0.0
        self._min_interval = 0.1
    
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Hacer request autenticado con retry."""
        path = f"{self.BROKERAGE_PATH}{endpoint}"
        url = urljoin(self.BASE_URL, path)
        
        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        
        # P0: Firmar exactamente el path que se enviará, incluyendo query string
        signed_path = path
        if params:
            qs = urlencode(sorted(params.items()), doseq=True)
            signed_path = f"{path}?{qs}"
        
        # Generar JWT para esta llamada
        token = self.jwt_auth.generate_rest_jwt(method, signed_path)
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=self.timeout,
            )
            self._last_request_time = time.time()
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else None
            try:
                error_data = e.response.json() if e.response else None
            except:
                error_data = None
            
            logger.error(f"HTTP {status}: {error_data}")
            raise CoinbaseAPIError(
                f"HTTP {status}: {error_data}",
                status_code=status,
                response=error_data,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {e}")
            raise CoinbaseAPIError(f"Request error: {e}")
    
    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        reraise=True,
    )
    def _request_with_retry(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Request con retry automático."""
        return self._request(method, endpoint, params, json_data)
    
    # ---------- Endpoints ----------
    
    def list_accounts(self) -> List[Dict[str, Any]]:
        """GET /accounts - Listar cuentas y balances."""
        response = self._request_with_retry("GET", "/accounts")
        return response.get("accounts", [])
    
    def get_product(self, product_id: str) -> Dict[str, Any]:
        """GET /products/{product_id} - Metadata del producto."""
        return self._request_with_retry("GET", f"/products/{product_id}")
    
    def get_transaction_summary(self) -> Dict[str, Any]:
        """GET /transaction_summary - Fees y volumen 30d."""
        return self._request_with_retry("GET", "/transaction_summary")
    
    def create_order(
        self,
        client_order_id: str,
        product_id: str,
        side: str,
        order_configuration: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        POST /orders - Crear orden.
        
        CORREGIDO P0: Parsear wrapper correcto.
        Response: { success: bool, success_response: {...}, error_response: {...} }
        """
        body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side,
            "order_configuration": order_configuration,
        }
        
        response = self._request_with_retry("POST", "/orders", json_data=body)
        
        # CORREGIDO P0: Parsear wrapper
        if response.get("success"):
            return response.get("success_response", {})
        else:
            error = response.get("error_response", {})
            raise CoinbaseAPIError(
                f"Order creation failed: {error}",
                response=error,
            )
    
    def create_limit_order_gtc(
        self,
        client_order_id: str,
        product_id: str,
        side: str,
        base_size: Decimal,
        limit_price: Decimal,
        post_only: bool = True,
    ) -> Dict[str, Any]:
        """Crear orden limit GTC."""
        return self.create_order(
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_configuration={
                "limit_limit_gtc": {
                    "base_size": str(base_size),
                    "limit_price": str(limit_price),
                    "post_only": post_only,
                }
            },
        )
    
    def create_market_order(
        self,
        client_order_id: str,
        product_id: str,
        side: str,
        base_size: Optional[Decimal] = None,
        quote_size: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """Crear orden market IOC."""
        config = {}
        if base_size is not None:
            config["base_size"] = str(base_size)
        elif quote_size is not None:
            config["quote_size"] = str(quote_size)
        else:
            raise ValueError("Debe proporcionar base_size o quote_size")
        
        return self.create_order(
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_configuration={
                "market_market_ioc": config
            },
        )
    
    def cancel_orders(self, order_ids: List[str]) -> Dict[str, Any]:
        """POST /orders/batch_cancel - Cancelar órdenes."""
        body = {"order_ids": order_ids}
        return self._request_with_retry("POST", "/orders/batch_cancel", json_data=body)
    
    def list_orders(
        self,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """GET /orders/historical/batch - Listar órdenes."""
        params = {"limit": limit}
        if product_id:
            params["product_id"] = product_id
        if status:
            params["status"] = status
        
        response = self._request_with_retry("GET", "/orders/historical/batch", params=params)
        return response.get("orders", [])
    
    def get_order(self, order_id: str) -> Dict[str, Any]:
        """
        GET /orders/historical/{order_id} - Ver orden específica.
        
        CORREGIDO P0: Parsear wrapper {"order": {...}}
        """
        response = self._request_with_retry("GET", f"/orders/historical/{order_id}")
        
        # CORREGIDO P0: Extraer orden del wrapper
        return response.get("order", {})
    
    def list_fills(
        self,
        product_id: Optional[str] = None,
        order_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """GET /orders/historical/fills - Listar fills."""
        params = {"limit": limit}
        if product_id:
            params["product_id"] = product_id
        if order_id:
            params["order_id"] = order_id
        
        response = self._request_with_retry("GET", "/orders/historical/fills", params=params)
        return response.get("fills", [])


def build_coinbase_client(
    key_name: Optional[str] = None,
    key_secret: Optional[str] = None,
    issuer: str = "cdp",
) -> CoinbaseRESTClient:
    """Factory para crear cliente Coinbase."""
    import os
    
    key_name = key_name or os.getenv("COINBASE_KEY_NAME", "")
    key_secret = key_secret or os.getenv("COINBASE_KEY_SECRET", "")
    
    if not key_name or not key_secret:
        raise ValueError("key_name y key_secret son requeridos")
    
    credentials = CoinbaseCredentials(
        key_name=key_name,
        key_secret=key_secret,
    )
    
    jwt_auth = JWTAuth(credentials, issuer=issuer)
    
    return CoinbaseRESTClient(jwt_auth)
