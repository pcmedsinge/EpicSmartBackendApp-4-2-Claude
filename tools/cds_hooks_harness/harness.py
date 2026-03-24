"""
CDS Hooks test harness — simulates Epic firing a hook at CFIP.

Run from the project root with venv activated:
    python tools/cds_hooks_harness/harness.py           # runs Scenario A
    python tools/cds_hooks_harness/harness.py --scenario B

Requires CFIP to be running:
    python -m app.main

What it does:
  1. Builds a spec-compliant order-select HookRequest for the chosen scenario
  2. POSTs it to localhost:8000/cds-services/cfip-order-intelligence
  3. Prints each returned card in a readable format
"""

import argparse  # stdlib — parses command-line arguments
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

# Add project root to path so we can import tools/scenarios
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools.cds_hooks_harness.scenarios import ALL_SCENARIOS, Scenario

CFIP_BASE_URL = "http://localhost:8000"
HOOK_ENDPOINT = f"{CFIP_BASE_URL}/cds-services/cfip-order-intelligence"


def build_hook_request(scenario: Scenario) -> dict:
    """
    Build a spec-compliant CDS Hooks 2.0 request body from a scenario.

    hookInstance is a fresh UUID per call — Epic generates one per hook firing.
    """
    return {
        "hook": scenario.hook,
        # str(uuid.uuid4()) generates a random UUID string — unique per request
        "hookInstance": str(uuid.uuid4()),
        "context": scenario.context,
        # Only include prefetch key if the scenario has prefetch data
        **({"prefetch": scenario.prefetch} if scenario.prefetch else {}),
    }


def print_card(index: int, card: dict) -> None:
    """Pretty-print a single CDS card to the terminal."""
    # ANSI colour codes — make the indicator visually obvious in the terminal
    # C# analogy: Console.ForegroundColor = ConsoleColor.Blue
    colours = {
        "info":     "\033[94m",   # blue
        "warning":  "\033[93m",   # yellow
        "critical": "\033[91m",   # red
    }
    reset = "\033[0m"
    bold  = "\033[1m"

    indicator = card.get("indicator", "info")
    colour = colours.get(indicator, "\033[94m")

    print(f"\n  {bold}Card {index}{reset}  [{colour}{indicator.upper()}{reset}]")
    print(f"  {'─' * 56}")
    print(f"  {bold}Summary:{reset}  {card.get('summary', '')}")

    detail = card.get("detail", "")
    if detail:
        print(f"\n  {bold}Detail:{reset}")
        # Print each markdown line indented — strip ** bold markers for terminal
        for line in detail.splitlines():
            print(f"    {line}")

    suggestions = card.get("suggestions", [])
    if suggestions:
        print(f"\n  {bold}Suggestions:{reset}")
        for s in suggestions:
            recommended = " ★" if s.get("isRecommended") else ""
            print(f"    [ {s.get('label', '')}{recommended} ]")

    links = card.get("links", [])
    if links:
        print(f"\n  {bold}Links:{reset}")
        for lnk in links:
            print(f"    → {lnk.get('label', '')}  {lnk.get('url', '')}")


async def run(scenario_id: str) -> None:
    scenario = ALL_SCENARIOS.get(scenario_id.upper())
    if not scenario:
        print(f"Unknown scenario: {scenario_id}. Available: {list(ALL_SCENARIOS.keys())}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"  CFIP CDS Hooks Test Harness")
    print(f"  Scenario {scenario.id}: {scenario.name}")
    print("=" * 60)
    print(f"\n  {scenario.description}\n")

    hook_request = build_hook_request(scenario)

    print(f"  Firing hook → {HOOK_ENDPOINT}")
    print(f"  hookInstance: {hook_request['hookInstance']}")

    # ---------------------------------------------------------------------------
    # POST the hook request to CFIP
    # timeout=10.0 — fail fast if the server isn't running
    # ---------------------------------------------------------------------------
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                HOOK_ENDPOINT,
                # json=... serialises the dict to JSON and sets Content-Type header
                # C# analogy: JsonContent.Create(hookRequest)
                json=hook_request,
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
        except httpx.ConnectError:
            print("\n  ✗ Could not connect to CFIP. Is the server running?")
            print("    Start it with: python -m app.main\n")
            sys.exit(1)

    print(f"\n  HTTP {response.status_code}")

    if response.status_code != 200:
        print(f"  ✗ Error response:\n{response.text}")
        sys.exit(1)

    body: dict = response.json()
    cards: list = body.get("cards", [])

    if not cards:
        print("\n  ⚠  No cards returned (empty response)")
    else:
        print(f"\n  ✓ {len(cards)} card(s) returned:")
        for i, card in enumerate(cards, start=1):
            print_card(i, card)

    # Print raw JSON for debugging — useful when building Phase 3+
    print(f"\n{'─' * 60}")
    print("  Raw JSON response:")
    print(json.dumps(body, indent=2))
    print("=" * 60 + "\n")


if __name__ == "__main__":
    # argparse is the stdlib way to handle CLI arguments.
    # C# analogy: parsing args[] manually or using a CLI library like System.CommandLine.
    parser = argparse.ArgumentParser(description="CFIP CDS Hooks test harness")
    parser.add_argument(
        "--scenario",
        default="A",
        help="Scenario ID to run (A, B, C, D). Default: A",
    )
    args = parser.parse_args()

    asyncio.run(run(args.scenario))
