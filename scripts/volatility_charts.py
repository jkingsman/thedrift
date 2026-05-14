"""Generate tall PNG charts comparing volatility scenarios for all items."""
import random
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

ITEMS_LOW = [
    ("Scrap Metal", 8, 1.5),
    ("Crystal Shards", 15, 2.0),
    ("Void Dust", 25, 3.0),
    ("Bio-gel", 10, 1.5),
    ("Plasma Coils", 18, 2.0),
    ("Data Fragments", 12, 1.8),
    ("Rare Earth", 30, 2.5),
    ("Coolant", 6, 1.0),
    ("Hull Plating", 25, 1.5),
    ("Power Cell", 45, 2.0),
    ("Signal Beacon", 35, 1.8),
    ("Neural Lace", 30, 2.0),
]

ITEMS_HIGH = [
    ("Warp Drive", 150, 3.0),
    ("AI Core", 120, 2.5),
    ("Quantum Relay", 130, 2.8),
    ("Habitat Module", 100, 2.0),
    ("Fuel Rod", 2000, 3.0),
]

TICKS = 40
SEED = 42

SCENARIOS = [
    ("Current (div=100, rev=2%)", 100, 0.02),
    ("div=50, rev=2%", 50, 0.02),
    ("div=25, rev=2%", 25, 0.02),
    ("div=10, rev=2%", 10, 0.02),
    ("div=50, rev=1%", 50, 0.01),
    ("div=25, rev=1%", 25, 0.01),
]


def simulate_all(items, divisor, reversion, ticks, seed):
    """Simulate all items together tick-by-tick with independent random draws."""
    rng = random.Random(seed)
    all_prices = {name: [base] for name, base, _ in items}
    current = {name: base for name, base, _ in items}
    vols = {name: vol for name, _, vol in items}
    bases = {name: base for name, base, _ in items}

    for _ in range(ticks):
        for name, base, vol in items:
            change_pct = rng.uniform(-vol, vol) / divisor
            price = current[name] * (1 + change_pct)
            rev = (base - price) * reversion
            price += rev
            price = max(0.01, round(price, 2))
            current[name] = price
            all_prices[name].append(price)

    return all_prices


def draw_panel(ax, items, label, divisor, reversion, colors, seed, show_legend=False):
    ax.set_facecolor('#111827')
    all_prices = simulate_all(items, divisor, reversion, TICKS, seed)

    for i, (name, base, vol) in enumerate(items):
        prices = all_prices[name]
        ax.plot(range(TICKS + 1), prices, color=colors[i], linewidth=1.8,
                label=f"{name} ({base}cr, v={vol})", alpha=0.85)
        ax.axhline(y=base, color=colors[i], linewidth=0.5, linestyle=':', alpha=0.3)

    ax.set_ylim(bottom=0)
    ax.set_xlabel('Tick', color='#94a3b8', fontsize=10)
    ax.set_ylabel('Price (credits)', color='#94a3b8', fontsize=10)
    ax.tick_params(colors='#64748b', labelsize=9)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter('%.0f'))
    ax.grid(True, color='#1e293b', linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_color('#1e293b')
    if show_legend:
        ax.legend(loc='upper left', fontsize=8, ncol=3,
                  facecolor='#1e293b', edgecolor='#334155',
                  labelcolor='#e2e8f0', framealpha=0.9)


def main():
    n = len(SCENARIOS)
    fig, axes = plt.subplots(n, 2, figsize=(20, 5 * n),
                             gridspec_kw={'width_ratios': [3, 2]})
    fig.patch.set_facecolor('#0a0e17')

    colors_low = plt.cm.tab20(range(len(ITEMS_LOW)))
    colors_high = plt.cm.Set1(range(len(ITEMS_HIGH)))

    for i, (label, divisor, reversion) in enumerate(SCENARIOS):
        ax_low = axes[i][0]
        ax_high = axes[i][1]

        draw_panel(ax_low, ITEMS_LOW, label, divisor, reversion, colors_low,
                   seed=SEED, show_legend=(i == 0))
        draw_panel(ax_high, ITEMS_HIGH, label, divisor, reversion, colors_high,
                   seed=SEED + 1, show_legend=(i == 0))

        ax_low.set_title(f'{label} — Raw & Crafted (6-45cr)',
                         color='#38bdf8', fontsize=13, fontweight='bold', pad=10)
        ax_high.set_title(f'{label} — Advanced & Fuel Rod (100-2000cr)',
                          color='#f472b6', fontsize=13, fontweight='bold', pad=10)

    fig.suptitle('Price Volatility Scenarios — All Items (40 ticks)',
                 color='#e2e8f0', fontsize=18, fontweight='bold', y=1.0)
    plt.tight_layout(rect=[0, 0, 1, 0.99])
    out = 'scripts/volatility_scenarios.png'
    fig.savefig(out, dpi=150, facecolor=fig.get_facecolor(), bbox_inches='tight')
    plt.close()
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()
