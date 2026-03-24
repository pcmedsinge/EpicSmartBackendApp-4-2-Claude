"""
Phase 1 verification script — proves Epic auth + FHIR reads are working.

Run from the project root with the venv activated:
    python tools/verify_epic.py

What it does:
  1. Loads config from .env
  2. Authenticates with Epic (SMART Backend Services JWT flow)
  3. Fetches Patient erXuFYUfucBZaryVksYEcMg3
  4. Fetches active Coverage for that patient
  5. Prints a clean summary to the terminal

Success looks like:
  ✓ Access token acquired
  ✓ Patient: Derrick Lin | DOB: 1973-11-05 | Gender: male
  ✓ Coverage (1): UnitedHealthcare | active
"""

import asyncio  # stdlib — Python's async runtime
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Add the project root to sys.path so we can import `app.*` when running
# this script directly (not as an installed package).
#
# __file__ is the path to this script. .parent gives tools/, .parent again
# gives the project root. sys.path is the list of directories Python searches
# for importable modules. C# analogy: adding a reference to a project.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.fhir.auth import get_access_token
from app.fhir.client import FhirClient

# ---------------------------------------------------------------------------
# Logging setup — basicConfig configures the root logger.
# format includes the logger name and level for easy debugging.
# C# analogy: builder.Logging.AddConsole() with a format template.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Known Epic sandbox patient
TEST_PATIENT_ID = "erXuFYUfucBZaryVksYEcMg3"


async def main() -> None:
    print("\n" + "=" * 60)
    print("  CFIP — Phase 1 Epic Sandbox Verification")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Step 1: OAuth — get a bearer token
    # ------------------------------------------------------------------
    print("\n[1/3] Authenticating with Epic...")
    try:
        token = await get_access_token()
        # Show only the first 40 chars — enough to confirm it's a real token
        print(f"  ✓ Access token acquired: {token[:40]}...")
    except Exception as e:
        print(f"  ✗ Auth failed: {e}")
        sys.exit(1)  # non-zero exit code signals failure to the shell

    # ------------------------------------------------------------------
    # Step 2 & 3: FHIR reads inside a single client context
    # `async with` ensures the HTTP connection pool is closed when done.
    # ------------------------------------------------------------------
    async with FhirClient() as client:

        # Step 2: Patient
        print(f"\n[2/3] Fetching Patient {TEST_PATIENT_ID}...")
        try:
            patient = await client.get_patient(TEST_PATIENT_ID)
            print(f"  ✓ Patient     : {patient.display_name}")
            print(f"    DOB         : {patient.birth_date}")
            print(f"    Gender      : {patient.gender}")
            print(f"    FHIR ID     : {patient.id}")
        except Exception as e:
            print(f"  ✗ Patient fetch failed: {e}")
            sys.exit(1)

        # Step 3: Coverage
        print(f"\n[3/3] Fetching active Coverage for {TEST_PATIENT_ID}...")
        try:
            coverages = await client.get_coverage(TEST_PATIENT_ID)

            if not coverages:
                print("  ⚠  No active Coverage found for this patient")
            else:
                print(f"  ✓ Found {len(coverages)} coverage record(s):")
                for i, cov in enumerate(coverages, start=1):
                    # Multi-line f-string using implicit string concatenation
                    print(
                        f"    [{i}] Payor     : {cov.payor_name}\n"
                        f"        Status    : {cov.status}\n"
                        f"        Period    : {cov.period.start if cov.period else 'N/A'}"
                        f" → {cov.period.end if cov.period else 'N/A'}\n"
                        f"        FHIR ID   : {cov.id}"
                    )
        except Exception as e:
            print(f"  ✗ Coverage fetch failed: {e}")
            sys.exit(1)

    print("\n" + "=" * 60)
    print("  Phase 1 complete — Epic auth + FHIR reads verified ✓")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# asyncio.run() is the entry point for async code called from synchronous
# context (i.e. the terminal). It creates an event loop, runs main() to
# completion, then closes the loop.
# C# analogy: Task.Run(() => Main()).GetAwaiter().GetResult()
# or simply: await Main() inside an async Main in C# 7.1+
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(main())
