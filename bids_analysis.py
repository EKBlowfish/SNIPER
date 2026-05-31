from __future__ import annotations

"""Utilities for inspecting and forecasting price history data.

This script reads stored prices for a particular listing from the SQLite
database and can print the raw history, generate a graph and attempt a simple
linear prediction of future prices.
"""

import argparse
import sqlite3
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def read_history(db_path: str, key: str):
    """Return lists of datetimes and prices for an item.

    Args:
        db_path: Path to the SQLite database.
        key: Listing identifier stored in the ``ads`` table.

    Returns:
        Two lists: ``times`` containing :class:`datetime` objects and ``prices``
        containing corresponding numeric prices.
    """

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT seen_at, price FROM price_history WHERE key=? ORDER BY seen_at ASC",
        (key,),
    )
    rows = cur.fetchall()
    conn.close()
    times = [datetime.fromisoformat(ts) for ts, _ in rows]
    prices = [price for _, price in rows]
    return times, prices


def print_log(times, prices) -> None:
    """Print the bid history to standard output."""

    for t, p in zip(times, prices):
        print(f"{t.isoformat()} -> {p}")


def plot_graph(times, prices, out_path: str, key: str) -> None:
    """Plot price history and save to a file.

    Args:
        times: Sequence of :class:`datetime` values.
        prices: List of prices matching ``times``.
        out_path: Destination path for the PNG graph.
        key: Item key used in the graph title.
    """

    plt.figure(figsize=(8, 4))
    plt.plot(times, prices, marker="o")
    plt.title(f"Price history for {key}")
    plt.xlabel("Time")
    plt.ylabel("Price")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_path)
    plt.close()


def predict_price(times, prices, future_hours: float) -> float | None:
    """Predict a future price using a simple linear model.

    Args:
        times: List of :class:`datetime` values representing the history.
        prices: Corresponding prices for each time point.
        future_hours: How many hours into the future to project.

    Returns:
        The predicted price or ``None`` if insufficient data is available.
    """

    if len(prices) < 2:
        return prices[-1] if prices else None
    t0 = times[0]
    t_sec = np.array([(t - t0).total_seconds() for t in times])
    y = np.array(prices)
    slope, intercept = np.polyfit(t_sec, y, 1)
    t_end = t_sec[-1] + future_hours * 3600
    return slope * t_end + intercept


def main() -> None:
    """Command-line interface for inspecting bid history."""
    parser = argparse.ArgumentParser(description="Analyze bid history")
    parser.add_argument("--db", default="ads.sqlite3", help="Path to SQLite database")
    parser.add_argument("--key", required=True, help="Item key to analyze")
    parser.add_argument(
        "--future-hours",
        type=float,
        default=1.0,
        help="Hours ahead to predict price",
    )
    parser.add_argument(
        "--graph", default="bid_history.png", help="Output PNG for graph"
    )
    args = parser.parse_args()

    times, prices = read_history(args.db, args.key)
    if not times:
        print("No price history found for item")
        return

    print_log(times, prices)
    plot_graph(times, prices, args.graph, args.key)
    pred = predict_price(times, prices, args.future_hours)
    if pred is not None:
        print(
            f"Predicted price after {args.future_hours}h: {pred:.2f}"
        )


if __name__ == "__main__":
    main()
