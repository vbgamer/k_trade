#!/usr/bin/env python
"""
AlgoTrade Pro - Market Ticks Simulation Script
Generates simulated tick prices for Nifty & BankNifty strike options.
Useful for testing algo trigger conditions and paper execution.
"""
import sys
import os
import time
import random
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def simulate_ticks(duration_seconds=60, interval=1.0):
    print("=" * 60)
    print("  AlgoTrade Pro - Live Market Tick Simulator")
    print(f"  Emulating ticks for {duration_seconds}s at {interval}s interval")
    print("=" * 60)

    nifty_spot = 24530.0
    banknifty_spot = 52140.0
    ce_price = 152.40
    pe_price = 118.60

    start_time = time.time()
    count = 0

    try:
        while time.time() - start_time < duration_seconds:
            count += 1
            # Random walk
            nifty_spot += round(random.uniform(-4.5, 4.8), 2)
            banknifty_spot += round(random.uniform(-12.0, 13.0), 2)
            ce_delta = (nifty_spot - 24530.0) * 0.55 + random.uniform(-1.5, 1.5)
            pe_delta = -(nifty_spot - 24530.0) * 0.45 + random.uniform(-1.5, 1.5)

            curr_ce = max(1.0, round(ce_price + ce_delta, 2))
            curr_pe = max(1.0, round(pe_price + pe_delta, 2))

            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] TICK #{count:03d} | NIFTY: {nifty_spot:.2f} | 24500 CE: Rs.{curr_ce:<6.2f} | 24500 PE: Rs.{curr_pe:<6.2f}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nSimulator stopped by user.")

    print(f"\nCompleted {count} ticks.")


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    simulate_ticks(duration_seconds=duration)
