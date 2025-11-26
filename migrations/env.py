"""Alembic environment configuration for score persistence."""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from logging.config import fileConfig
from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config
from sqlmodel import SQLModel

from app.config import settings
from app.models import score_record, subscription  # noqa: F401 - ensure models are imported

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("fundsignal.alembic")
logger.setLevel(logging.INFO)
target_metadata = SQLModel.metadata


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _log_database_url(url: str, source: str) -> None:
    try:
        rendered = make_url(url).render_as_string(hide_password=True)
    except Exception:  # pragma: no cover - log only
        rendered = "<invalid DATABASE_URL>"
    logger.info("Alembic resolved DATABASE_URL from %s: %s", source, rendered)
    config.print_stdout(f"[Alembic] DATABASE_URL source={source}: {rendered}")


def _config_database_url() -> str | None:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    runtime_section = config.get_section("alembic:runtime")
    if runtime_section:
        return runtime_section.get("sqlalchemy.url")
    return None


def _build_supabase_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()

    ca_file = os.environ.get("ALEMBIC_SUPABASE_CA_FILE")
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    elif certifi is not None:
        ctx.load_verify_locations(certifi.where())

    if _env_flag("ALEMBIC_SUPABASE_TLS_INSECURE"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.warning("Supabase TLS verification DISABLED for Alembic.")
        config.print_stdout("[Alembic] WARNING Supabase TLS verification DISABLED.")

    return ctx


def _supabase_adjustments(url: URL) -> tuple[str, dict[str, Any]]:
    """Return a normalized URL + connect args for Supabase."""
    host = (url.host or "").lower()
    if "supabase.co" not in host:
        return url.render_as_string(hide_password=False), {}

    connect_args: dict[str, Any] = {"ssl": _build_supabase_ssl_context()}
    new_url = url
    if url.port != 6543:
        new_url = new_url.set(port=6543)
    if url.query:
        query = dict(url.query)
        if query:
            query.pop("ssl", None)
            query.pop("sslmode", None)
            new_url = new_url.set(query=query)
    logger.info("Enforcing Supabase pooled port + TLS for Alembic.")
    config.print_stdout(
        "[Alembic] Normalized Supabase DATABASE_URL to pooled port + TLS."
    )
    return new_url.render_as_string(hide_password=False), connect_args


def _normalize_database_url(url: str) -> tuple[str, dict[str, Any]]:
    try:
        parsed = make_url(url)
    except Exception:
        logger.warning(
            "Unable to parse DATABASE_URL for normalization; using value as-is."
        )
        return url, {}
    normalized_url, connect_args = _supabase_adjustments(parsed)
    if (
        connect_args.get("ssl") is None
        and os.environ.get("PGSSLMODE", "").lower() == "require"
    ):
        connect_args["ssl"] = ssl.create_default_context()
    return normalized_url, connect_args


def _resolve_database_config() -> tuple[str, dict[str, Any]]:
    candidates = [
        ("environment variable", os.environ.get("DATABASE_URL")),
        ("alembic.ini", _config_database_url()),
        ("app settings", settings.database_url),
    ]
    for source, value in candidates:
        if not value:
            continue
        normalized, connect_args = _normalize_database_url(value)
        _log_database_url(normalized, source)
        return normalized, connect_args
    raise RuntimeError("DATABASE_URL must be set to run migrations.")


def run_migrations_offline() -> None:
    """Run migrations offline (e.g., CI)."""
    url, _ = _resolve_database_config()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations using an async engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    url, connect_args = _resolve_database_config()
    configuration["sqlalchemy.url"] = url
    if connect_args:
        existing_args = configuration.get("sqlalchemy.connect_args", {})
        merged_args = {**existing_args, **connect_args}
        configuration["sqlalchemy.connect_args"] = merged_args
    connectable: AsyncEngine = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online_sync() -> None:
    asyncio.run(run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online_sync()
