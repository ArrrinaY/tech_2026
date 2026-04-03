from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_FILE = BASE_DIR / ".env"

if not ENV_FILE.exists():
    print(f"⚠️ Warning: .env file not found at {ENV_FILE}")


class Settings(BaseSettings):
    db_user: str = "dating_user"
    db_password: str = "dating_password"
    db_name: str = "dating_db"
    db_host: str = "localhost"
    db_port: int = 5432

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def sync_database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    rabbitmq_user: str = "dating_user"
    rabbitmq_password: str = "dating_password"
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672

    @property
    def rabbitmq_url(self) -> str:
        return f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}@{self.rabbitmq_host}:{self.rabbitmq_port}//"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "dating_user"
    minio_secret_key: str = "dating_password"
    minio_bucket: str = "dating-photos"

    bot_token: str = ""

    profile_service_host: str = "localhost"
    profile_service_port: int = 8001

    class Config:
        env_file = str(ENV_FILE)
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
