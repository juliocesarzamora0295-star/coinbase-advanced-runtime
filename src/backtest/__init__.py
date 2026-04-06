"""
Backtest framework — validación de estrategias con datos históricos.

Separado del runtime de producción. No importa WebSocket, Coinbase API,
ni main.py. Reutiliza módulos de dominio (strategy, sizing, risk).
"""
