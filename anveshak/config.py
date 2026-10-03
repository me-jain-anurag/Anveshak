"""Runtime configuration from environment variables (optionally a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Public JSON-RPC endpoints used for EVM chains that the configured Etherscan plan does not
# cover. Each was checked on 2026-10-03 to accept eth_getLogs with a contract-address filter
# over the span given in DEFAULT_RPC_MAX_SPAN. Override with ANVESHAK_RPC_<CHAIN>.
DEFAULT_EVM_RPC = {
    "bsc": "https://bsc-rpc.publicnode.com",
    "polygon": "https://polygon-bor-rpc.publicnode.com",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "base": "https://base-rpc.publicnode.com",
    "optimism": "https://optimism-rpc.publicnode.com",
    "avalanche": "https://avalanche-c-chain-rpc.publicnode.com",
}
DEFAULT_RPC_MAX_SPAN = {"polygon": 1000}  # blocks per eth_getLogs call; others 5000
# Reference data (labels, asset registry, directory, policies). Overridable for installed deployments.
DATA_DIR = Path(os.environ.get("ANVESHAK_DATA_DIR", str(PROJECT_ROOT / "data")))


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
    etherscan_paid: bool = False  # the Etherscan key's plan covers chains beyond the free tier
    evm_rpc_urls: dict = field(default_factory=lambda: dict(DEFAULT_EVM_RPC))
    rpc_max_span: dict = field(default_factory=lambda: dict(DEFAULT_RPC_MAX_SPAN))
    logscan_window_hours: int = 72
    standalone_approvals: bool = False
    crosschain_resolvers: tuple[str, ...] = ("thorchain", "wormhole", "layerzero", "across")
    api_clients_file: Path | None = None  # default: <var>/api_clients.yaml when it exists
    max_body_bytes: int = 1_048_576
    callback_secret: str = ""  # HMAC key for callback signatures (falls back to ANVESHAK_API_TOKEN)
    data_dir: Path = DATA_DIR

    @property
    def evidence_dir(self) -> Path:
        return self.var_dir / "evidence"

    @property
    def clients_path(self) -> Path:
        return self.api_clients_file or self.var_dir / "api_clients.yaml"

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
        etherscan_paid=env.get("ETHERSCAN_PAID", "").strip().lower() in ("1", "true", "yes"),
        evm_rpc_urls={k: env.get(f"ANVESHAK_RPC_{k.upper()}", v).strip() for k, v in DEFAULT_EVM_RPC.items()}
        | {k[len("ANVESHAK_RPC_"):].lower(): v.strip() for k, v in env.items() if k.startswith("ANVESHAK_RPC_") and v.strip()},
        logscan_window_hours=int(env.get("ANVESHAK_LOGSCAN_HOURS", "72")),
        standalone_approvals=env.get("ANVESHAK_STANDALONE_APPROVALS", "").strip().lower() in ("1", "true", "yes"),
        crosschain_resolvers=tuple(
            r.strip().lower() for r in env.get("ANVESHAK_RESOLVERS", "thorchain,wormhole,layerzero,across").split(",") if r.strip()
        ),
        api_clients_file=Path(env["ANVESHAK_API_CLIENTS_FILE"]) if env.get("ANVESHAK_API_CLIENTS_FILE", "").strip() else None,
        max_body_bytes=int(env.get("ANVESHAK_MAX_BODY_BYTES", "1048576")),
        callback_secret=env.get("ANVESHAK_CALLBACK_SECRET", "").strip(),
    )
