from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "senha-local-123"
    neo4j_database: str = "neo4j"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = Field(300, ge=1, le=86400)
    cache_prefix: str = "recomendador:v2"
    cache_lock_seconds: int = Field(30, ge=20, le=300)
    cache_wait_seconds: float = Field(5, gt=0, le=30)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
