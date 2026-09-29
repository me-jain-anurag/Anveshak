from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from anveshak.assets import AssetRegistry
from anveshak.chain import Chain
from anveshak.config import DATA_DIR, Settings
from anveshak.directory import VaspDirectory
from anveshak.domain import Category, Label, SourceClass, Transfer, TransferKind

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def registry() -> AssetRegistry:
    return AssetRegistry.load(DATA_DIR / "assets.yaml")


@pytest.fixture(scope="session")
def real_directory() -> VaspDirectory:
    return VaspDirectory.load(DATA_DIR / "vasp_directory.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        etherscan_api_key="TESTKEY-SECRET",
        etherscan_base_url="https://api.etherscan.test/v2/api",
        trongrid_api_key="",
        trongrid_base_url="https://api.trongrid.test",
        esplora_base_url="https://esplora.test/api",
        var_dir=tmp_path / "var",
        api_token="",
    )


def label(chain: Chain, address: str, klass: SourceClass, source: str, entity: str | None = "ex1", category: Category | None = Category.EXCHANGE, **kw) -> Label:
    return Label(
        chain=chain,
        address=address,
        entity_id=entity,
        entity_name=entity.upper() if entity else None,
        category=category,
        text=kw.pop("text", f"{entity} wallet"),
        source_id=kw.pop("source_id", "test"),
        source_class=klass,
        primary_source=source,
        as_of=date(2026, 1, 1),
        dataset_ref="tests",
        **kw,
    )


def xfer(asset, sender: str, receiver: str, amount: int, minutes: int, name: str | None = None, block: int | None = None) -> Transfer:
    import hashlib

    tx = hashlib.sha256((name or f"{sender}{receiver}{amount}{minutes}").encode()).hexdigest()
    return Transfer(
        chain=asset.chain,
        tx_hash=tx,
        kind=TransferKind.TOKEN if asset.contract else TransferKind.NATIVE,
        position=0,
        sender=sender,
        receiver=receiver,
        asset=asset,
        amount=amount,
        block_number=block if block is not None else 1_000 + minutes,
        timestamp=BASE + timedelta(minutes=minutes),
        evidence_id="test",
    )
