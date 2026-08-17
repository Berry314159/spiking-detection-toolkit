"""
Entry point for the spiking-detection toolkit on Replit.

Run order matters. Start with `validate` -- if the engine does not pass its own
tests in your environment, nothing downstream is trustworthy.

    python main.py validate      # engine self-test, no network needed
    python main.py tempe         # live pull: venue names (Tempe AZ)
    python main.py venuetypes    # venue-type risk ratios (run tempe first)
    python main.py dallas        # live pull: narrative text + receipts denominator

The live pulls hit public endpoints and cache to CSV in this directory, so a
second run is fast. `dallas` and `tempe` each move tens of thousands of rows;
on a small Repl give them a few minutes and do not run both at once.
"""

import subprocess
import sys

STEPS = {
    "validate": ("test_spikescore.py", "Engine self-test against simulated ground truth"),
    "tempe": ("demo_tempe.py", "Tempe AZ calls for service -- the only US feed with venue names"),
    "venuetypes": ("demo_venue_types.py", "Venue-type risk ratios, temporal test, burden estimate"),
    "dallas": ("demo_dallas.py", "Dallas narratives x Texas mixed-beverage receipts"),
}


def main() -> int:
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "validate"

    if arg in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    if arg == "all":
        for key in ("validate", "tempe", "venuetypes", "dallas"):
            code = run_step(key)
            if code != 0:
                print(f"\nStopped: '{key}' exited {code}.")
                return code
        return 0

    if arg not in STEPS:
        print(f"Unknown step '{arg}'.\n")
        print("Available steps:")
        for key, (_, blurb) in STEPS.items():
            print(f"  {key:<12} {blurb}")
        print("  all          run every step in order")
        return 2

    return run_step(arg)


def run_step(key: str) -> int:
    script, blurb = STEPS[key]
    print(f"\n{'=' * 72}\n{key}: {blurb}\n{'=' * 72}\n", flush=True)
    return subprocess.call([sys.executable, script])


if __name__ == "__main__":
    raise SystemExit(main())
