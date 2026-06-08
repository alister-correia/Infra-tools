from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    vcd_username: str = Field("", env="VCD_USERNAME")   # now supplied per-user at login
    vcd_password: str = Field("", env="VCD_PASSWORD")   # now supplied per-user at login
    vcd_org: str = Field("", env="VCD_ORG")  # optional — set via UI org selector
    # Comma-separated list of Name:https://host pairs e.g. "Prod:https://vcd1.co,Dev:https://vcd2.co"
    vcd_environments: str = Field("", env="VCD_ENVIRONMENTS")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
