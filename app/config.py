from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    WHATSAPP_VERIFY_TOKEN: str = ""
    NGROK_AUTHTOKEN: str = ""
    APP_PORT: int = 8000
    APP_HOST: str = "0.0.0.0"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()