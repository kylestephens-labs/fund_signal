import logging
from contextlib import asynccontextmanager

try:
    import sentry_sdk
except ModuleNotFoundError:  # Sentry optional in local/test envs
    sentry_sdk = None
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

try:
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
except ModuleNotFoundError:  # Sentry optional during local dev/tests
    FastApiIntegration = None
    LoggingIntegration = None

from app.api.routes import auth as auth_routes
from app.api.routes import delivery as delivery_routes
from app.api.routes import example, health, scores
from app.config import settings
from app.core.database import init_database
from app.core.metrics import setup_metrics

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")

    # Initialize Sentry if DSN is provided
    if sentry_sdk and FastApiIntegration and LoggingIntegration and settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            integrations=[
                FastApiIntegration(auto_enabling_instrumentations=False),
                LoggingIntegration(level=logging.INFO),
            ],
            traces_sample_rate=0.1,
            environment=settings.environment,
        )
        logger.info("Sentry initialized")

    # Initialize database
    await init_database()

    logger.info("Application startup complete")

    yield

    # Shutdown
    logger.info("Shutting down application")


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="A production-ready FastAPI template",
    lifespan=lifespan,
    debug=settings.debug,
)

# Add CORS middleware
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Add security middleware
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=["*"] if settings.debug else ["localhost", "127.0.0.1"]
)


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all requests."""
    logger.info(f"{request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"Response status: {response.status_code}")
    return response


# Setup metrics
setup_metrics(app)
logger.info("Prometheus metrics enabled")

# Include routers
app.include_router(health.router, prefix="/health", tags=["health"])
app.include_router(example.router, prefix="/api", tags=["example"])
app.include_router(scores.router, prefix="/api", tags=["scores"])
app.include_router(auth_routes.router, tags=["auth"])
app.include_router(delivery_routes.router, tags=["delivery", "billing", "leads"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": f"Welcome to {settings.app_name}",
        "version": settings.app_version,
        "environment": settings.environment,
    }
