"""Core module para Coinbase Advanced Trade API."""
from src.core.jwt_auth import JWTAuth, CoinbaseCredentials, load_credentials_from_env
from src.core.coinbase_exchange import CoinbaseRESTClient, CoinbaseAPIError, build_coinbase_client
from src.core.coinbase_websocket import CoinbaseWSFeed, WSMessage
from src.core.quantization import Quantizer, ProductInfo, create_quantizer_from_api_response

__all__ = [
    "JWTAuth",
    "CoinbaseCredentials",
    "load_credentials_from_env",
    "CoinbaseRESTClient",
    "CoinbaseAPIError",
    "build_coinbase_client",
    "CoinbaseWSFeed",
    "WSMessage",
    "Quantizer",
    "ProductInfo",
    "create_quantizer_from_api_response",
]
