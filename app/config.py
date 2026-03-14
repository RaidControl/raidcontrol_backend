from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    db_host: str
    db_port: int = 3306
    db_name: str
    db_user: str
    db_password: str

    # Auth
    device_api_key: str
    admin_username: str
    admin_password: str
    jwt_secret: str
    jwt_expires_min: int = 720

    # App
    upload_dir: str = "uploads"
    needs_review_min_conf: float = 0.30
    finish_checkpoint_id: str = "finish"

    # Timezone
    local_tz_offset_hours: float = -3  # ART (Argentina) = UTC-3

    # DigitalOcean Spaces (S3-compatible)
    spaces_access_key: str = ""
    spaces_secret_key: str = ""
    spaces_bucket: str = ""
    spaces_region: str = "nyc3"
    spaces_cdn_domain: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?charset=utf8mb4"
        )

settings = Settings()
