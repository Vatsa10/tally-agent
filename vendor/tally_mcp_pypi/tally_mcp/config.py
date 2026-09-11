from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    tally_host: str = "localhost"
    tally_port: int = 9000
    tally_username: str = ""
    tally_password: str = ""
    tally_default_company: str = ""
    server_host: str = "0.0.0.0"
    server_port: int = 8765
    auth_token: str = ""
    require_auth: bool = False

    @property
    def tally_url(self) -> str:
        return f"http://{self.tally_host}:{self.tally_port}"

    model_config = {"env_prefix": "TALLY_MCP_"}


settings = Settings()
