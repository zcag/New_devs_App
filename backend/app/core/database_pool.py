import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import logging
from ..config import settings

logger = logging.getLogger(__name__)


def _async_dsn(url: str) -> str:
    """Return the configured database URL using the asyncpg driver."""
    # settings.database_url is the single source of truth for the DB connection
    # (and is what docker-compose provides). Normalize it to the async driver.
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def initialize(self):
        """Initialize database connection pool (idempotent)."""
        if self.session_factory is not None:
            # Already initialized — reuse the engine/pool. Re-creating it on every
            # request would leak a new async engine + connection pool each time.
            return
        try:
            # Build the async engine from the configured database_url.
            # (Previously this was assembled from settings.supabase_db_* keys that
            # do not exist on Settings, so the engine never initialized and the
            # revenue service silently fell back to hard-coded mock data.)
            database_url = _async_dsn(settings.database_url)

            self.engine = create_async_engine(
                database_url,
                # NB: async engines manage their own async-compatible pool; passing
                # the sync QueuePool here would raise. Keep the standard tuning.
                pool_size=20,  # Number of connections to maintain
                max_overflow=30,  # Additional connections when needed
                pool_pre_ping=True,  # Validate connections
                pool_recycle=3600,  # Recycle connections every hour
                echo=False  # Set to True for SQL debugging
            )
            
            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False
            )
            
            logger.info("✅ Database connection pool initialized")
            
        except Exception as e:
            logger.error(f"❌ Database pool initialization failed: {e}")
            self.engine = None
            self.session_factory = None
    
    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
    
    def get_session(self) -> AsyncSession:
        """Get a database session from the pool.

        Returns the AsyncSession directly (it is itself an async context
        manager) so callers can use ``async with db_pool.get_session()``.
        Declaring this ``async`` would return a coroutine instead, which does
        not support the async-context-manager protocol.
        """
        if not self.session_factory:
            raise Exception("Database pool not initialized")
        return self.session_factory()

# Global database pool instance
db_pool = DatabasePool()

async def get_db_session() -> AsyncSession:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
