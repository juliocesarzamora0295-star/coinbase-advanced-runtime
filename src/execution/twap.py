"""
TWAP (Time-Weighted Average Price) executor.

Splits a large order into N slices at regular intervals.
For paper mode, simulates timing. For live, schedules slices.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Optional

logger = logging.getLogger("TWAP")


@dataclass(frozen=True)
class TWAPSlice:
    """A single slice of a TWAP order."""

    slice_index: int
    qty: Decimal
    price: Decimal  # execution price of this slice
    ts_ms: int


@dataclass(frozen=True)
class TWAPResult:
    """Result of a complete TWAP execution."""

    total_qty: Decimal
    avg_price: Decimal
    slices: list  # List[TWAPSlice]
    total_slices: int
    completed_slices: int

    @property
    def fill_ratio(self) -> Decimal:
        if self.total_slices == 0:
            return Decimal("0")
        return Decimal(str(self.completed_slices)) / Decimal(str(self.total_slices))


class TWAPExecutor:
    """
    Splits an order into N equal slices executed at regular intervals.

    Args:
        num_slices: number of slices (default 5)
        interval_seconds: seconds between slices (default 60)
        execute_fn: callback to execute a single slice
            signature: (symbol, side, qty, price) -> bool (success)
    """

    def __init__(
        self,
        num_slices: int = 5,
        interval_seconds: float = 60.0,
        execute_fn: Optional[Callable] = None,
    ) -> None:
        self.num_slices = max(1, num_slices)
        self.interval_seconds = interval_seconds
        self.execute_fn = execute_fn

    def execute(
        self,
        symbol: str,
        side: str,
        total_qty: Decimal,
        price: Decimal,
        simulate: bool = True,
    ) -> TWAPResult:
        """
        Execute a TWAP order.

        If simulate=True, fills all slices immediately at price (for backtesting).
        If simulate=False, waits interval_seconds between slices and calls execute_fn.
        """
        slice_qty = total_qty / Decimal(str(self.num_slices))
        slices: List[TWAPSlice] = []
        total_filled = Decimal("0")
        completed = 0

        for i in range(self.num_slices):
            # Adjust last slice for remainder
            remaining = total_qty - total_filled
            qty = min(slice_qty, remaining)
            if qty <= Decimal("0"):
                break

            if simulate:
                ts = int(time.time() * 1000) + i * int(self.interval_seconds * 1000)
                slices.append(TWAPSlice(
                    slice_index=i, qty=qty, price=price, ts_ms=ts,
                ))
                total_filled += qty
                completed += 1
            else:
                if i > 0:
                    time.sleep(self.interval_seconds)

                success = False
                if self.execute_fn:
                    success = self.execute_fn(symbol, side, qty, price)

                if success:
                    slices.append(TWAPSlice(
                        slice_index=i, qty=qty, price=price,
                        ts_ms=int(time.time() * 1000),
                    ))
                    total_filled += qty
                    completed += 1
                else:
                    logger.warning(
                        "TWAP slice %d/%d failed for %s %s qty=%s",
                        i + 1, self.num_slices, symbol, side, qty,
                    )

        avg_price = (
            sum(s.price * s.qty for s in slices) / total_filled
            if total_filled > Decimal("0")
            else Decimal("0")
        )

        result = TWAPResult(
            total_qty=total_filled,
            avg_price=avg_price,
            slices=slices,
            total_slices=self.num_slices,
            completed_slices=completed,
        )

        logger.info(
            "TWAP complete: %s %s filled=%s/%s slices=%d/%d avg_price=%s",
            symbol, side, total_filled, total_qty,
            completed, self.num_slices, avg_price,
        )
        return result
