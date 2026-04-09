# READ-ONLY REFERENCE — DO NOT IMPORT INTO RUNTIME
# Origin: crypto-bot - dev/Bot Híbrido v2.2 Centinela/risk_manager.py

import logging
import json
import os

class AdvancedRiskManager:
    """
    Gestiona el riesgo con estado persistente para sobrevivir a reinicios.
    VERSIÓN FINAL 2.2 "CENTINELA".
    """
    def __init__(self, config):
        self.config = config
        self.state_file = "risk_manager_state.json"
        self.load_state()
        logging.info(f"AdvancedRiskManager v2.2 (Centinela) inicializado.")
        logging.info(f"Estado inicial cargado: Capital Pico=${self.peak_capital:.2f}, Pérdidas Consecutivas={self.consecutive_losses}")

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.peak_capital = max(state.get('peak_capital', self.config.INITIAL_CAPITAL), self.config.INITIAL_CAPITAL)
                    self.consecutive_losses = state.get('consecutive_losses', 0)
            except (json.JSONDecodeError, TypeError):
                logging.warning("El archivo de estado de riesgo está corrupto o vacío. Reiniciando a valores por defecto.")
                self._set_default_state()
        else:
            self._set_default_state()
        self.total_capital = self.config.INITIAL_CAPITAL
        self.confidence_multiplier = 0.5 if self.consecutive_losses >= self.config.MAX_CONSECUTIVE_LOSSES else 1.0

    def _set_default_state(self):
        self.peak_capital = self.config.INITIAL_CAPITAL
        self.consecutive_losses = 0

    def save_state(self):
        state = {
            'peak_capital': self.peak_capital,
            'consecutive_losses': self.consecutive_losses
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=4)

    def update_capital(self, new_capital):
        self.total_capital = new_capital
        if self.total_capital > self.peak_capital:
            self.peak_capital = self.total_capital
        self.current_drawdown = (self.peak_capital - self.total_capital) / self.peak_capital if self.peak_capital > 0 else 0
        self.save_state()
        return not self.is_drawdown_breached()

    def is_drawdown_breached(self):
        return self.current_drawdown > self.config.MAX_DRAWDOWN_LIMIT

    def record_trade_result(self, pnl):
        if pnl <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if self.consecutive_losses >= self.config.MAX_CONSECUTIVE_LOSSES:
            self.confidence_multiplier = 0.5
            logging.warning(f"Racha de pérdidas alcanzada. Multiplicador de confianza reducido a {self.confidence_multiplier}")
        else:
            self.confidence_multiplier = 1.0
        logging.info(f"Trade PnL: {pnl:+.2f}. Pérdidas consecutivas: {self.consecutive_losses}. Multiplicador actual: {self.confidence_multiplier}")
        self.save_state()

    def calculate_position_size(self, entry_price):
        if entry_price <= 0:
            logging.error("Precio de entrada inválido (<=0).")
            return 0
        base_risk_amount = self.total_capital * self.config.RISK_PER_TRADE
        adjusted_risk_amount = base_risk_amount * self.confidence_multiplier
        position_size = adjusted_risk_amount / entry_price
        logging.info(f"Cálculo de tamaño de posición: Capital Total=${self.total_capital:.2f}, Riesgo Base=${base_risk_amount:.2f}, Riesgo Ajustado=${adjusted_risk_amount:.2f}, Tamaño Posición={position_size:.8f}")
        return position_size
