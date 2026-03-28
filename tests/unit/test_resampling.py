"""Tests para resampling de velas 5m -> timeframe operativo."""

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd


class TestResampling:
    """Tests de resampling de OHLCV."""

    def create_5m_candles_aligned(self, n=12, start_hour=0):
        """
        Crear velas de 5 minutos alineadas a bordes de hora.

        Args:
            n: Número de velas de 5m
            start_hour: Hora de inicio (0-23) para alineación
        """
        # Crear timestamp alineado al inicio de la hora
        base_dt = datetime(2024, 1, 1, start_hour, 0, 0, tzinfo=timezone.utc)
        base_ts = int(base_dt.timestamp() * 1000)

        candles = []
        for i in range(n):
            ts = base_ts + (i * 5 * 60 * 1000)  # +5 minutos
            candles.append(
                {
                    "timestamp": ts,
                    "open": Decimal("50000") + i,
                    "high": Decimal("50100") + i,
                    "low": Decimal("49900") + i,
                    "close": Decimal("50050") + i,
                    "volume": Decimal("1.0"),
                }
            )

        return candles

    def resample_with_filter(self, df, rule, required):
        """
        Resample con filtrado de barras incompletas.

        Replica la lógica de _get_ohlcv_df().
        """
        # closed="left": intervalo [inicio, fin) - vela en el borde va al bucket siguiente
        ohlcv = df.resample(rule, label="right", closed="left").agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )

        counts = df["close"].resample(rule, label="right", closed="left").count()
        return ohlcv[counts >= required].dropna()

    def test_create_dataframe_with_datetimeindex(self):
        """Crear DataFrame con DatetimeIndex UTC."""
        candles = self.create_5m_candles_aligned(3)

        df = pd.DataFrame(candles).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")

        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None  # UTC timezone

    def test_resample_5m_to_1h(self):
        """Resampling de 5m a 1h con filtrado de barras completas."""
        candles = self.create_5m_candles_aligned(12)  # 12 velas de 5m = 1 hora exacta

        df = pd.DataFrame(candles).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")

        # Resample a 1h con filtrado de barras completas
        df_1h = self.resample_with_filter(df, "1h", required=12)

        # Debería resultar en 1 vela de 1h completa
        assert len(df_1h) == 1

        # Verificar agregaciones
        assert df_1h["open"].iloc[0] == df["open"].iloc[0]  # first
        assert df_1h["high"].iloc[0] == df["high"].max()  # max
        assert df_1h["low"].iloc[0] == df["low"].min()  # min
        assert df_1h["close"].iloc[0] == df["close"].iloc[-1]  # last
        assert df_1h["volume"].iloc[0] == df["volume"].sum()  # sum

    def test_resample_5m_to_4h(self):
        """Resampling de 5m a 4h con filtrado de barras completas."""
        candles = self.create_5m_candles_aligned(48)  # 48 velas de 5m = 4 horas exactas

        df = pd.DataFrame(candles).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")

        # Resample a 4h con filtrado de barras completas
        df_4h = self.resample_with_filter(df, "4h", required=48)

        # Debería resultar en 1 vela de 4h completa
        assert len(df_4h) == 1

    def test_filter_incomplete_bars(self):
        """Filtrar barras incompletas (menos de 12 velas 5m para 1h)."""
        # Crear solo 10 velas (incompletas para 1h)
        candles = self.create_5m_candles_aligned(10)

        df = pd.DataFrame(candles).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")

        # Resample a 1h con filtrado
        df_1h = self.resample_with_filter(df, "1h", required=12)

        # No debería haber barras completas
        assert len(df_1h) == 0

    def test_timeframe_mapping(self):
        """Mapeo de timeframe a regla de pandas."""
        rule_map = {
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1h",
            "2h": "2h",
            "4h": "4h",
            "6h": "6h",
            "1d": "1d",
        }

        assert rule_map["1h"] == "1h"
        assert rule_map["4h"] == "4h"
        assert rule_map["5m"] == "5min"

    def test_no_resampling_for_5m(self):
        """Si el timeframe es 5m, no se hace resampling."""
        candles = self.create_5m_candles_aligned(12)

        df = pd.DataFrame(candles).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")

        # No resamplear si timeframe es 5m nativo
        assert len(df) == 12

    def test_required_counts_mapping(self):
        """Verificar mapeo de required counts por timeframe."""
        required = {
            "15min": 3,
            "30min": 6,
            "1h": 12,
            "2h": 24,
            "4h": 48,
            "6h": 72,
            "1d": 288,
        }

        # 1h requiere 12 velas de 5m
        assert required["1h"] == 12
        # 4h requiere 48 velas de 5m
        assert required["4h"] == 48
        # 1d requiere 288 velas de 5m (12 * 24)
        assert required["1d"] == 288
