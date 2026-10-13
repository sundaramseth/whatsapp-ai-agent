from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    WHATSAPP_VERIFY_TOKEN: str = ""
    META_APP_SECRET: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    META_GRAPH_VERSION: str = ""  # Set to a currently supported version in your Meta app.
    ADMIN_API_KEY: str = ""
    DATABASE_PATH: str = "data/agent.sqlite3"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = ""
    AI_ENABLED: bool = False
    WORKER_ENABLED: bool = True
    RETENTION_DAYS: int = 30
    APP_PORT: int = 8000
    APP_HOST: str = "0.0.0.0"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
