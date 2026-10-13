from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KO_", extra="ignore")
    database_url: str = "postgresql://knowledgeops@127.0.0.1:55434/knowledgeops"
    public_origin: str = "http://127.0.0.1:8044"
    secure_cookies: bool = False
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    run_ttl_seconds: int = Field(default=1800, ge=30, le=86400)
    max_active_runs: int = Field(default=8, ge=1, le=100)
    max_session_runs: int = Field(default=2, ge=1, le=10)
    runs_per_minute: int = Field(default=12, ge=1, le=120)
    lease_seconds: int = Field(default=30, ge=1, le=300)
    max_attempts: int = Field(default=3, ge=1, le=5)
    worker_poll_seconds: float = 0.2
    private_token: str = ""
    private_tenant: str = "private"
    private_role: str = "engineering"
    agentops_url: str = ""
    # The currently available AgentOps v1.1 contract accepts synthetic tasks only.
    # Content inference stays off until a compatible private gateway is explicitly configured.
    agentops_content_contract: str = "disabled"
    agentops_token: str = ""
    agentops_policy_expires: str = ""
    agentops_policy_version: str = ""
    agentops_region: str = "eu"
    private_daily_budget: float = Field(default=0.0, ge=0, le=100)
    private_request_budget: float = Field(default=0.0, ge=0, le=1)
    contracts_source_revision: str = ""
    contracts_environment: str = "development"
    knowledge_credentials_file: str = ""
    # Separate, optional empty-context synthetic routing profile; never private content.
    agentops_synthetic_profile_file: str = ""

    @model_validator(mode="after")
    def validate_policy(self):
        if self.private_token and len(self.private_token) < 32:
            raise ValueError("Private API token must contain at least 32 characters")
        if self.public_origin.startswith("https://") and not self.secure_cookies:
            raise ValueError("HTTPS origins require secure cookies")
        if self.agentops_url and not self.agentops_url.startswith("https://"):
            raise ValueError("AgentOps URL must use HTTPS")
        if self.private_tenant == "demo":
            raise ValueError("Private and demo tenant must differ")
        return self
