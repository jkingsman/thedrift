#!/usr/bin/env python3
"""
Simulate the progressive upkeep system over time.

Models an agent who logs in at regular intervals, performs actions,
earns credits, and pays upkeep charges between sessions.

Usage:
    python scripts/tax_simulator.py
    python scripts/tax_simulator.py --login-interval 2 --actions 40 --income 400 --days 14
    python scripts/tax_simulator.py --login-interval 12 --actions 100 --income 1500 --start-credits 3000

Arguments:
    --login-interval  Hours between logins (default: 6)
    --actions         Meaningful actions per login session (default: 70)
    --income          Net credits earned per session before taxes (default: 800)
    --inventory-value Avg inventory value held between sessions (default: 500)
    --start-credits   Starting credits (default: 500)
    --days            Days to simulate (default: 7)
    --tick-interval   Seconds per tick (default: 60)
"""

import argparse

# ── Upkeep constants (must match station/tick.py) ──────────────────

UPKEEP_INTERVAL_TICKS = 30
UPKEEP_ACTIVITY_CAP_ACTIONS = 80
UPKEEP_CREDIT_EXEMPTION = 1500.0
UPKEEP_INVENTORY_EXEMPTION = 2500.0
UPKEEP_ACTIVITY_WINDOW_HOURS = 12
UPKEEP_CREDIT_RATE = 0.0025
UPKEEP_INVENTORY_RATE = 0.00125
UPKEEP_MAX_WALLET_RATE = 0.005

# Event-based taxes
STATION_REQUISITION_CHANCE_PER_EVENT = 3 / 93  # weight 3 out of ~93 total
EVENT_FIRE_CHANCE = 0.70
EVENT_COOLDOWN_TICKS = 20
REQUISITION_RATE = 0.035  # avg of 2-5%
IDLE_THRESHOLD_TICKS = 10


def simulate(
    login_interval_hours: float,
    actions_per_session: int,
    income_per_session: float,
    inventory_value: float,
    start_credits: float,
    days: int,
    tick_interval_seconds: int,
):
    ticks_per_hour = 3600 / tick_interval_seconds
    total_ticks = int(days * 24 * ticks_per_hour)
    login_interval_ticks = int(login_interval_hours * ticks_per_hour)
    sessions_per_day = 24 / login_interval_hours

    # Estimate session duration in ticks (roughly 1 tick per action for fast bots)
    session_duration_ticks = max(actions_per_session, 10)

    credits = start_credits
    total_upkeep_paid = 0.0
    total_requisition_loss = 0.0
    total_earned = 0.0

    # Track daily snapshots
    daily_snapshots = []
    last_day_credits = start_credits

    # Track action history for activity factor
    # Simplified: track how many actions happened in the last ACTIVITY_WINDOW
    activity_window_ticks = int(UPKEEP_ACTIVITY_WINDOW_HOURS * ticks_per_hour)
    action_log = []  # list of tick numbers when actions happened

    last_login_tick = -login_interval_ticks  # force first login at tick 0
    last_event_tick = -EVENT_COOLDOWN_TICKS
    station_idle_since = 0

    for tick in range(total_ticks):
        hour = tick / ticks_per_hour
        day = hour / 24

        # Daily snapshot
        if tick > 0 and tick % int(24 * ticks_per_hour) == 0:
            day_num = int(day)
            delta = credits - last_day_credits
            pct = (delta / last_day_credits * 100) if last_day_credits > 0 else 0
            daily_snapshots.append({
                "day": day_num,
                "credits": credits,
                "delta": delta,
                "pct": pct,
                "upkeep_cumulative": total_upkeep_paid,
                "earned_cumulative": total_earned,
            })
            last_day_credits = credits

        # Check if agent logs in this tick
        is_session_active = False
        if tick - last_login_tick >= login_interval_ticks:
            last_login_tick = tick
            # Agent logs in, performs actions, earns credits
            credits += income_per_session
            total_earned += income_per_session
            # Record actions in the log
            for i in range(actions_per_session):
                action_log.append(tick + i)
            station_idle_since = tick + session_duration_ticks
            is_session_active = True

        # Is the station active? (within session or shortly after)
        station_active = tick < station_idle_since + IDLE_THRESHOLD_TICKS

        if not station_active:
            continue

        # Upkeep charge
        if tick % UPKEEP_INTERVAL_TICKS == 0 and tick > 0:
            # Count recent meaningful actions
            cutoff = tick - activity_window_ticks
            recent_actions = sum(1 for a in action_log if a >= cutoff)
            # Prune old entries
            action_log = [a for a in action_log if a >= cutoff]

            if recent_actions > 0:
                activity_factor = min(1.0, recent_actions / UPKEEP_ACTIVITY_CAP_ACTIONS)

                taxable_credits = max(0.0, credits - UPKEEP_CREDIT_EXEMPTION)
                taxable_inventory = max(0.0, inventory_value - UPKEEP_INVENTORY_EXEMPTION)
                uncapped_tax = (
                    taxable_credits * UPKEEP_CREDIT_RATE
                    + taxable_inventory * UPKEEP_INVENTORY_RATE
                ) * activity_factor
                max_tax = credits * UPKEEP_MAX_WALLET_RATE
                tax = round(min(uncapped_tax, max_tax), 2)

                if tax >= 0.01:
                    credits -= tax
                    total_upkeep_paid += tax

        # Station requisition (rare event during active periods)
        if station_active and tick - last_event_tick >= EVENT_COOLDOWN_TICKS:
            # Check if an event fires
            import random
            if random.random() < EVENT_FIRE_CHANCE:
                last_event_tick = tick
                # Check if it's a requisition
                if random.random() < STATION_REQUISITION_CHANCE_PER_EVENT:
                    # Lose some inventory value (simplified as credit equivalent)
                    loss = inventory_value * REQUISITION_RATE
                    total_requisition_loss += loss
                    # Don't subtract from credits directly — it takes items

    # Final day snapshot
    day_num = days
    delta = credits - last_day_credits
    pct = (delta / last_day_credits * 100) if last_day_credits > 0 else 0
    daily_snapshots.append({
        "day": day_num,
        "credits": credits,
        "delta": delta,
        "pct": pct,
        "upkeep_cumulative": total_upkeep_paid,
        "earned_cumulative": total_earned,
    })

    return credits, total_upkeep_paid, total_requisition_loss, total_earned, daily_snapshots


def main():
    parser = argparse.ArgumentParser(
        description="Simulate The Drift's progressive upkeep over time",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--login-interval", type=float, default=6, help="Hours between logins (default: 6)")
    parser.add_argument("--actions", type=int, default=70, help="Meaningful actions per session (default: 70)")
    parser.add_argument("--income", type=float, default=800, help="Net credits earned per session (default: 800)")
    parser.add_argument("--inventory-value", type=float, default=500, help="Avg inventory value between sessions (default: 500)")
    parser.add_argument("--start-credits", type=float, default=500, help="Starting credits (default: 500)")
    parser.add_argument("--days", type=int, default=7, help="Days to simulate (default: 7)")
    parser.add_argument("--tick-interval", type=int, default=60, help="Seconds per tick (default: 60)")
    args = parser.parse_args()

    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  TAX SIMULATOR                                          ║")
    print(f"╠══════════════════════════════════════════════════════════╣")
    print(f"║  Login every {args.login_interval:.0f}h, {args.actions} actions/session, ¤{args.income:.0f} income  ║")
    print(f"║  Starting: ¤{args.start_credits:.0f}, Inventory: ~¤{args.inventory_value:.0f}, Horizon: {args.days}d        ║")
    print(f"╚══════════════════════════════════════════════════════════╝")
    print()

    final, upkeep, requisition, earned, snapshots = simulate(
        login_interval_hours=args.login_interval,
        actions_per_session=args.actions,
        income_per_session=args.income,
        inventory_value=args.inventory_value,
        start_credits=args.start_credits,
        days=args.days,
        tick_interval_seconds=args.tick_interval,
    )

    sessions_per_day = 24 / args.login_interval
    total_sessions = args.days * sessions_per_day

    print(f"{'Day':<5} {'Credits':>10} {'Daily Δ':>10} {'Daily %':>8} {'Upkeep Paid':>12} {'Total Earned':>13}")
    print(f"{'-'*5} {'-'*10} {'-'*10} {'-'*8} {'-'*12} {'-'*13}")
    for s in snapshots:
        sign = "+" if s["delta"] >= 0 else ""
        print(f"{s['day']:<5} ¤{s['credits']:>9.2f} {sign}¤{s['delta']:>8.2f} {s['pct']:>+7.1f}% ¤{s['upkeep_cumulative']:>10.2f} ¤{s['earned_cumulative']:>11.2f}")

    print()
    print(f"  SUMMARY ({args.days} days)")
    print(f"  {'─' * 45}")
    print(f"  Sessions:           {total_sessions:.0f}")
    print(f"  Total earned:       ¤{earned:.2f}")
    print(f"  Total upkeep paid:  ¤{upkeep:.2f}")
    print(f"  Upkeep % of income: {(upkeep/earned*100) if earned else 0:.1f}%")
    print(f"  Requisition loss:   ~¤{requisition:.2f} (inventory items)")
    print(f"  Net credits:        ¤{final:.2f}")
    print(f"  Net growth:         ¤{final - args.start_credits:.2f} ({(final/args.start_credits - 1)*100:+.1f}%)")
    print()

    # Tax bracket comparison
    print(f"  TAX BRACKET COMPARISON")
    print(f"  {'─' * 45}")
    for label, credits_level in [("Poor (¤500)", 500), ("Middle (¤1500)", 1500), ("Rich (¤3000)", 3000), ("Whale (¤10000)", 10000)]:
        taxable = max(0, credits_level - UPKEEP_CREDIT_EXEMPTION)
        af = min(1.0, args.actions / UPKEEP_ACTIVITY_CAP_ACTIONS)
        per_cycle = round(min(taxable * UPKEEP_CREDIT_RATE * af, credits_level * UPKEEP_MAX_WALLET_RATE), 2)
        daily = per_cycle * (24 * 60 / (args.tick_interval * UPKEEP_INTERVAL_TICKS)) * (args.actions * args.login_interval / (24 * UPKEEP_ACTIVITY_CAP_ACTIONS))
        print(f"  {label:<20} ¤{per_cycle:.2f}/cycle, ~¤{daily:.2f}/day")


if __name__ == "__main__":
    main()
