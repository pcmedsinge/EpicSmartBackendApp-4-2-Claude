import logging
from contextlib import asynccontextmanager  # stdlib — used to define startup/shutdown logic
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.cds_hooks import router as cds_router
from app.config import get_settings

# ---------------------------------------------------------------------------
# Module-level logger
# logging.getLogger(__name__) creates a logger named after the current module
# ("app.main"). C# analogy: ILogger<T> injected via DI, but here we get it
# directly — no injection needed for module-level use.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan context manager — startup and shutdown logic
#
# @asynccontextmanager turns a generator function into an async context manager.
# FastAPI calls everything BEFORE the `yield` on startup, and everything AFTER
# the `yield` on shutdown.
#
# C# analogy: IHostedService.StartAsync / StopAsync, or WebApplication
# builder.Services + app.Lifetime.ApplicationStarted events.
#
# Why not the old @app.on_event("startup") decorator?
# FastAPI deprecated that in favour of lifespan — it keeps startup/shutdown
# in one place and works correctly with pytest's test client.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    settings = get_settings()
    logger.info("CFIP starting up")
    logger.info("Epic FHIR base URL : %s", settings.epic_fhir_base_url)
    logger.info("Epic client ID     : %s", settings.epic_client_id)
    logger.info("zrok public URL    : %s", settings.zrok_public_url)
    logger.info("Server             : http://%s:%s", settings.app_host, settings.app_port)

    logger.info("CDS Hooks discovery : http://%s:%s/cds-services", settings.app_host, settings.app_port)
    logger.info("Test harness UI     : http://%s:%s/harness/", settings.app_host, settings.app_port)

    yield  # <-- application runs here

    # --- Shutdown ---
    logger.info("CFIP shutting down")


# ---------------------------------------------------------------------------
# FastAPI application instance
# The lifespan parameter wires up our startup/shutdown logic above.
# FastAPI auto-generates OpenAPI docs at /docs and /redoc — no config needed.
# ---------------------------------------------------------------------------
app = FastAPI(
    title="CFIP — Clinical-Financial Intelligence Platform",
    description=(
        "Agentic SMART on FHIR Backend Service bridging clinical decision support, "
        "revenue cycle intelligence, and pharmacogenomics."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routers
#
# include_router() registers all routes defined in a router onto the main app.
# C# analogy: app.MapControllers() or manually calling app.UseEndpoints(...)
#             with a controller registration.
# ---------------------------------------------------------------------------
app.include_router(cds_router)

# ---------------------------------------------------------------------------
# Static files — serves the CDS Hooks test harness browser UI.
#
# StaticFiles mounts a directory so every file in it is served as-is.
# "html=True" makes /harness/ serve index.html automatically (like a web server).
# C# analogy: app.UseStaticFiles() with a PathString prefix.
#
# The directory is created lazily — only mounted if it exists, so the server
# starts cleanly even before D5 (index.html) is written.
# ---------------------------------------------------------------------------
_harness_static = Path("tools/cds_hooks_harness/static")
if _harness_static.exists():
    app.mount(
        "/harness",
        StaticFiles(directory=_harness_static, html=True),
        name="harness",
    )

# ---------------------------------------------------------------------------
# Health endpoint
#
# @app.get("/health") is a decorator that registers the function below as the
# handler for GET /health requests.
# C# analogy: [HttpGet("health")] on a controller action.
#
# FastAPI automatically serialises the returned dict to JSON and sets
# Content-Type: application/json — no JsonResult wrapper needed.
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Infrastructure"])
async def health() -> dict:
    """Liveness probe — confirms the service is running."""
    return {
        "status": "ok",
        "service": "CFIP",
        "version": "0.1.0",
    }


# ---------------------------------------------------------------------------
# Entry point — allows running with: python -m app.main
#
# __name__ == "__main__" is True only when this file is executed directly,
# not when it is imported as a module.
# C# analogy: static void Main(string[] args) in Program.cs.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",      # module path : variable name — uvicorn imports this
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level,
        reload=True,         # auto-restart on file changes (dev only)
    )
