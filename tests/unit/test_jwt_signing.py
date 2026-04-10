"""
Tests for JWT signing and REST client URI construction.

Invariants tested:
- JWT URI for REST endpoints uses path-only (no query string)
- Query params are sent on the HTTP URL but not signed into the JWT
- Endpoints with and without params produce correct signed URIs
"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.coinbase_exchange import CoinbaseRESTClient
from src.core.jwt_auth import JWTAuth


@pytest.fixture
def mock_jwt_auth():
    """JWTAuth that records the path it signs."""
    auth = MagicMock(spec=JWTAuth)
    auth.generate_rest_jwt.return_value = "mock-token"
    return auth


@pytest.fixture
def client(mock_jwt_auth):
    """CoinbaseRESTClient with mocked auth and HTTP session."""
    c = CoinbaseRESTClient(jwt_auth=mock_jwt_auth)
    c.session = MagicMock()
    return c


def _setup_response(client, data: dict):
    """Configure mock session to return a successful response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = data
    mock_resp.raise_for_status.return_value = None
    client.session.request.return_value = mock_resp


class TestJWTURINoQueryString:
    """JWT URI must never include query parameters."""

    def test_list_orders_signs_path_only(self, client, mock_jwt_auth):
        """list_orders signs /orders/historical/batch without query params."""
        _setup_response(client, {"orders": []})

        client.list_orders("BTC-USD")

        method, path = mock_jwt_auth.generate_rest_jwt.call_args[0]
        assert method == "GET"
        assert path == "/api/v3/brokerage/orders/historical/batch"
        assert "?" not in path, "JWT signed path must not contain query string"

        # HTTP request URL includes query params
        call_kwargs = client.session.request.call_args
        actual_url = call_kwargs.kwargs.get("url", "")
        assert "product_id=BTC-USD" in actual_url
        assert "limit=100" in actual_url

    def test_list_fills_signs_path_only(self, client, mock_jwt_auth):
        """list_fills signs /orders/historical/fills without query params."""
        _setup_response(client, {"fills": []})

        client.list_fills("BTC-USD")

        method, path = mock_jwt_auth.generate_rest_jwt.call_args[0]
        assert path == "/api/v3/brokerage/orders/historical/fills"
        assert "?" not in path

    def test_list_accounts_no_params(self, client, mock_jwt_auth):
        """list_accounts (no query params) still signs path-only."""
        _setup_response(client, {"accounts": []})

        client.list_accounts()

        method, path = mock_jwt_auth.generate_rest_jwt.call_args[0]
        assert path == "/api/v3/brokerage/accounts"
        assert "?" not in path

    def test_get_product_path_param_no_query(self, client, mock_jwt_auth):
        """get_product uses path param, not query — signs correctly."""
        _setup_response(client, {"product_id": "BTC-USD"})

        client.get_product("BTC-USD")

        method, path = mock_jwt_auth.generate_rest_jwt.call_args[0]
        assert path == "/api/v3/brokerage/products/BTC-USD"
        assert "?" not in path

    def test_multiple_query_params_not_in_jwt(self, client, mock_jwt_auth):
        """list_orders with order_status list — JWT still path-only."""
        _setup_response(client, {"orders": []})

        client.list_orders("BTC-USD", order_status=["OPEN", "FILLED"], limit=50)

        method, path = mock_jwt_auth.generate_rest_jwt.call_args[0]
        assert "?" not in path
        assert "order_status" not in path
        assert "limit" not in path

        actual_url = client.session.request.call_args.kwargs.get("url", "")
        assert "order_status" in actual_url
        assert "product_id=BTC-USD" in actual_url
