# READ-ONLY REFERENCE — DO NOT IMPORT INTO RUNTIME
# Origin: crypto-bot - dev/Bot Híbrido v2.2 Centinela/strategy_manager.py

import logging
import pandas as pd
import pandas_ta as ta
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
import time
import json
import os

class StrategyManager:
    """
    Gestiona la lógica de trading, el grid adaptativo y el filtro de IA.
    Guarda su estado para reanudar operaciones.
    VERSIÓN FINAL 2.2 "CENTINELA".
    """
    def __init__(self, config):
        self.config = config
        self.state_file = "strategy_manager_state.json"
        self.model = None
        self.last_ai_retrain = 0
        self.current_grid = {'buy_levels': [], 'sell_levels': []}
        self.load_state()
        logging.info("StrategyManager v2.2 (Centinela) inicializado.")
        if self.model:
            logging.info("Modelo de IA cargado desde estado anterior.")
        else:
            logging.info("No se encontró un modelo de IA previo. Se entrenará en el primer ciclo.")

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.current_grid = state.get('current_grid', {'buy_levels': [], 'sell_levels': []})
                    self.last_ai_retrain = state.get('last_ai_retrain', 0)
            except (json.JSONDecodeError, TypeError):
                logging.warning("Archivo de estado de estrategia corrupto. Reiniciando a valores por defecto.")
                self._set_default_state()
        else:
            self._set_default_state()

    def _set_default_state(self):
        self.current_grid = {'buy_levels': [], 'sell_levels': []}
        self.last_ai_retrain = 0

    def save_state(self):
        state = {
            'current_grid': self.current_grid,
            'last_ai_retrain': self.last_ai_retrain
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=4)

    def _diagnose_market_regime(self, df):
        adx = df.ta.adx()
        if adx is None or adx.empty: return 'RANGO'
        last_adx = adx.iloc[-1]['ADX_14']
        if last_adx > 25:
            return 'TENDENCIA'
        else:
            return 'RANGO'

    def _prepare_features(self, df):
        df.ta.rsi(length=14, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        df.ta.bbands(length=20, append=True)
        df['atr'] = df.ta.atr(length=14)
        df['regime'] = self._diagnose_market_regime(df)
        regime_dummies = pd.get_dummies(df['regime'], prefix='regime')
        df = pd.concat([df, regime_dummies], axis=1)
        df['target'] = (df['high'].shift(-1) > df['close'] + df['atr'] * 0.5).astype(int)
        df.dropna(inplace=True)
        feature_cols = [col for col in df.columns if col.startswith(('RSI', 'MACD', 'BBL', 'BBM', 'BBU', 'regime_'))]
        return df, feature_cols

    def train_ai_filter(self, df):
        logging.info("Iniciando entrenamiento del filtro de IA...")
        df_featured, feature_cols = self._prepare_features(df.copy())
        if df_featured.empty or len(df_featured) < 50:
            logging.warning("No hay suficientes datos para entrenar el modelo de IA.")
            self.model = None
            return
        X = df_featured[feature_cols]
        y = df_featured['target']
        if len(y.unique()) < 2:
            logging.warning("La variable objetivo solo tiene una clase.")
            self.model = None
            return
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
        self.model = RandomForestClassifier(n_estimators=100, random_state=42, class_weight='balanced')
        self.model.fit(X_train, y_train)
        accuracy = self.model.score(X_test, y_test)
        logging.info(f"Entrenamiento del filtro de IA completado. Precisión: {accuracy:.2f}")
        self.last_ai_retrain = time.time()
        self.save_state()

    def update_adaptive_grid(self, df):
        last_price = df['close'].iloc[-1]
        atr = df.ta.atr(length=14).iloc[-1]
        if not self.current_grid['buy_levels']:
            is_update_needed = True
        else:
            highest_buy = self.current_grid['buy_levels'][0]
            recenter_threshold = atr * self.config.GRID_RECENTER_THRESHOLD
            is_update_needed = abs(last_price - highest_buy) > recenter_threshold
        if is_update_needed:
            self.current_grid['buy_levels'] = []
            self.current_grid['sell_levels'] = []
            step = atr * self.config.GRID_ATR_MULTIPLIER
            for i in range(1, self.config.GRID_LEVELS + 1):
                buy_price = last_price - (i * step)
                sell_price = last_price + (i * step)
                self.current_grid['buy_levels'].append(buy_price)
                self.current_grid['sell_levels'].append(sell_price)
            self.save_state()
            return True
        return False

    def check_signals(self, df):
        if time.time() - self.last_ai_retrain > self.config.AI_RETRAIN_INTERVAL_SECONDS:
            self.train_ai_filter(df)
        self.update_adaptive_grid(df)
        last_price = df['close'].iloc[-1]
        signal = {'side': None, 'price': None}
        if self.current_grid['buy_levels'] and last_price < self.current_grid['buy_levels'][0]:
            signal_price = self.current_grid['buy_levels'].pop(0)
            if self.model:
                df_featured, feature_cols = self._prepare_features(df.copy())
                last_features = df_featured[feature_cols].iloc[-1:]
                prediction = self.model.predict(last_features)
                if prediction[0] == 0:
                    logging.warning(f"Señal de COMPRA a ${signal_price:.2f} VETADA por el filtro de IA.")
                    self.current_grid['buy_levels'].insert(0, signal_price)
                    return signal
            signal = {'side': 'buy', 'price': signal_price}
            logging.info(f"¡SEÑAL DE COMPRA GENERADA! Precio cruzó el nivel: ${signal_price:.2f}")
        elif self.current_grid['sell_levels'] and last_price > self.current_grid['sell_levels'][0]:
            signal_price = self.current_grid['sell_levels'].pop(0)
            signal = {'side': 'sell', 'price': signal_price}
            logging.info(f"¡SEÑAL DE VENTA GENERADA! Precio cruzó el nivel: ${signal_price:.2f}")
        if signal['side']:
            self.save_state()
        return signal
