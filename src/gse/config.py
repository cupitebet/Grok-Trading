import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Asset:
    symbol: str
    pair: str
    query: str


@dataclass(frozen=True)
class Settings:
    database_url: str
    x_bearer_token: str | None
    xai_api_key: str | None
    grok_model: str
    binance_base_url: str
    assets: tuple[Asset, ...]
    x_max_pages: int
    metric_delay_minutes: int


def load_assets(path: Path) -> tuple[Asset, ...]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assets = tuple(Asset(a["symbol"], a["pair"], a["query"]) for a in data.get("asset", []))
    if not assets:
        raise ValueError(f"tidak ada [[asset]] di {path}")
    symbols = [a.symbol for a in assets]
    if len(set(symbols)) != len(symbols):
        raise ValueError(f"simbol duplikat di {path}")
    return assets


def load_settings(env=os.environ) -> Settings:
    default_assets = Path(__file__).resolve().parents[2] / "config" / "assets.toml"
    return Settings(
        database_url=env.get("DATABASE_URL", "postgresql://gse:gse@localhost:5432/gse"),
        x_bearer_token=env.get("X_BEARER_TOKEN") or None,
        xai_api_key=env.get("XAI_API_KEY") or None,
        grok_model=env.get("GROK_MODEL", "grok-4"),
        binance_base_url=env.get("BINANCE_BASE_URL", "https://api.binance.com"),
        assets=load_assets(Path(env.get("GSE_ASSETS_FILE", default_assets))),
        x_max_pages=int(env.get("X_MAX_PAGES", "2")),
        metric_delay_minutes=int(env.get("METRIC_DELAY_MINUTES", "60")),
    )


def require(value: str | None, name: str) -> str:
    if not value:
        raise SystemExit(f"environment variable {name} belum diisi")
    return value
