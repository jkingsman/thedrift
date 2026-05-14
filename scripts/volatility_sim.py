"""Simulate price drift under different volatility parameters and output ASCII charts."""
import random
import sys

# Representative items with different volatility levels
ITEMS = [
    ("Coolant", 6, 1.0),       # low vol, cheap
    ("Crystal Shards", 15, 2.0), # mid vol
    ("Void Dust", 25, 3.0),     # high vol
    ("Warp Drive", 150, 3.0),   # high vol, expensive
]

TICKS = 40
CHART_HEIGHT = 15
CHART_WIDTH = 40

SCENARIOS = [
    # (label, divisor, reversion)
    ("CURRENT: div=100, reversion=0.02", 100, 0.02),
    ("More volatile: div=50, reversion=0.02", 50, 0.02),
    ("Much more volatile: div=25, reversion=0.02", 25, 0.02),
    ("Wild: div=10, reversion=0.02", 10, 0.02),
    ("More volatile + weaker reversion: div=50, reversion=0.01", 50, 0.01),
    ("Much more volatile + weaker reversion: div=25, reversion=0.01", 25, 0.01),
]


def simulate(base_price, volatility, divisor, reversion, ticks, seed=42):
    random.seed(seed)
    prices = [base_price]
    price = base_price
    for _ in range(ticks):
        change_pct = random.uniform(-volatility, volatility) / divisor
        price = price * (1 + change_pct)
        rev = (base_price - price) * reversion
        price += rev
        price = max(0.01, round(price, 2))
        prices.append(price)
    return prices


def ascii_chart(prices, height=CHART_HEIGHT, width=CHART_WIDTH, base_price=None):
    mn, mx = min(prices), max(prices)
    if mn == mx:
        mn -= 1
        mx += 1
    span = mx - mn
    lines = []

    for row in range(height, -1, -1):
        val = mn + (row / height) * span
        label = f"{val:>8.2f} |"
        chars = []
        for i in range(len(prices)):
            y = (prices[i] - mn) / span * height
            if abs(y - row) < 0.5:
                chars.append("*")
            elif base_price and abs(((base_price - mn) / span * height) - row) < 0.5:
                chars.append("-")
            else:
                chars.append(" ")
        lines.append(label + "".join(chars))

    # X axis
    lines.append(" " * 10 + "+" + "-" * len(prices))
    lines.append(" " * 10 + " 0" + " " * (len(prices) - 5) + f"T{len(prices)-1}")
    return "\n".join(lines)


def main():
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42

    for scenario_label, divisor, reversion in SCENARIOS:
        print("=" * 70)
        print(f"  {scenario_label}")
        print("=" * 70)

        for name, base, vol in ITEMS:
            prices = simulate(base, vol, divisor, reversion, TICKS, seed=seed)
            pct_range = (max(prices) - min(prices)) / base * 100
            final_pct = (prices[-1] - base) / base * 100

            print(f"\n  {name} (base={base}, vol={vol})")
            print(f"  Range: {min(prices):.2f} - {max(prices):.2f} ({pct_range:.1f}% of base)")
            print(f"  Final: {prices[-1]:.2f} ({final_pct:+.1f}%)")
            print()
            print(ascii_chart(prices, base_price=base))
            print()
        print()


if __name__ == "__main__":
    main()
