"""Runtime configuration from environment variables (optionally a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    etherscan_api_key: str
    etherscan_base_url: str
    trongrid_api_key: str
    trongrid_base_url: str
    esplora_base_url: str
    var_dir: Path
    api_token: str
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_max_signatures: int = 300
    callback_allowlist: tuple[str, ...] = ()
    monitor_interval_seconds: int = 600
    embedded_workers: int = 2
    chainalysis_api_key: str = ""
    etherscan_nametags: bool = False
    evm_probe_chains: tuple[str, ...] = ("ethereum", "polygon", "arbitrum", "bsc")
    auto_follow_up: bool = True
    data_dir: Path = DATA_DIR

    @property
    def evidence_dir(self) -> Path:
        return self.var_dir / "evidence"

    @property
    def db_path(self) -> Path:
        return self.var_dir / "cases.sqlite3"

    @property
    def reports_dir(self) -> Path:
        return self.var_dir / "reports"


def load_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    env = os.environ
    var_dir = Path(env.get("ANVESHAK_VAR_DIR", str(PROJECT_ROOT / "var")))
    return Settings(
        etherscan_api_key=env.get("ETHERSCAN_API_KEY", "").strip(),
        etherscan_base_url=env.get("ETHERSCAN_BASE_URL", "https://api.etherscan.io/v2/api").strip(),
        trongrid_api_key=env.get("TRONGRID_API_KEY", "").strip(),
        trongrid_base_url=env.get("TRONGRID_BASE_URL", "https://api.trongrid.io").rstrip("/"),
        esplora_base_url=env.get("ESPLORA_BASE_URL", "https://blockstream.info/api").rstrip("/"),
        var_dir=var_dir,
        api_token=env.get("ANVESHAK_API_TOKEN", "").strip(),
        solana_rpc_url=env.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip(),
        solana_max_signatures=int(env.get("SOLANA_MAX_SIGNATURES", "300")),
        callback_allowlist=tuple(h.strip().lower() for h in env.get("ANVESHAK_CALLBACK_ALLOWLIST", "").split(",") if h.strip()),
        monitor_interval_seconds=int(env.get("ANVESHAK_MONITOR_INTERVAL", "600")),
        embedded_workers=int(env.get("ANVESHAK_EMBEDDED_WORKERS", "2")),
        chainalysis_api_key=env.get("CHAINALYSIS_API_KEY", "").strip(),
        etherscan_nametags=env.get("ETHERSCAN_NAMETAGS", "").strip().lower() in ("1", "true", "yes"),
        evm_probe_chains=tuple(c.strip() for c in env.get("ANVESHAK_EVM_CHAINS", "ethereum,polygon,arbitrum,bsc").split(",") if c.strip()),
        auto_follow_up=env.get("ANVESHAK_AUTO_FOLLOW_UP", "1").strip().lower() in ("1", "true", "yes"),
    )
