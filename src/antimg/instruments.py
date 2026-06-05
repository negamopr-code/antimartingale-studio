"""Broad instrument catalog (yfinance tickers).

User asked for "all instruments I can find, not just SPY". These are grouped for the
GUI dropdown; any free-text ticker yfinance understands also works.
Continuous front-month futures use the `=F` suffix; FX uses `=X`.
"""
from __future__ import annotations

CATALOG: dict[str, list[tuple[str, str]]] = {
    "US equity index / ETF": [
        ("SPY", "S&P 500 ETF"),
        ("^GSPC", "S&P 500 index"),
        ("QQQ", "Nasdaq-100 ETF"),
        ("^NDX", "Nasdaq-100 index"),
        ("DIA", "Dow Jones ETF"),
        ("^DJI", "Dow Jones index"),
        ("IWM", "Russell 2000 ETF"),
        ("^RUT", "Russell 2000 index"),
        ("VTI", "Total US market"),
    ],
    "US sectors": [
        ("XLF", "Financials"), ("XLK", "Technology"), ("XLE", "Energy"),
        ("XLV", "Health care"), ("XLI", "Industrials"), ("XLY", "Cons. discr."),
        ("XLP", "Cons. staples"), ("XLU", "Utilities"), ("XLB", "Materials"),
        ("SMH", "Semiconductors"),
    ],
    "Volatility": [
        ("^VIX", "CBOE VIX"),
        ("^VVIX", "Vol of vol"),
        ("VXX", "VIX short-term ETN"),
    ],
    "Equity index futures": [
        ("ES=F", "E-mini S&P 500"), ("NQ=F", "E-mini Nasdaq-100"),
        ("YM=F", "E-mini Dow"), ("RTY=F", "E-mini Russell 2000"),
    ],
    "European indices": [
        ("^GDAXI", "DAX 40"), ("^FCHI", "CAC 40"), ("^STOXX50E", "Euro Stoxx 50"),
        ("^FTSE", "FTSE 100"), ("^IBEX", "IBEX 35"), ("FEZ", "Euro Stoxx 50 ETF"),
    ],
    "Asia indices": [
        ("^N225", "Nikkei 225"), ("^HSI", "Hang Seng"), ("000001.SS", "Shanghai Comp"),
        ("^AXJO", "ASX 200"),
    ],
    "Metals": [
        ("GLD", "Gold ETF"), ("GC=F", "Gold future"), ("MGC=F", "Micro gold"),
        ("SLV", "Silver ETF"), ("SI=F", "Silver future"), ("SIL", "Silver miners"),
        ("SILJ", "Junior silver miners"), ("GDX", "Gold miners"), ("GDXJ", "Junior gold miners"),
        ("PPLT", "Platinum ETF"), ("HG=F", "Copper future"),
    ],
    "Energy": [
        ("USO", "WTI oil ETF"), ("CL=F", "WTI crude future"), ("MCL=F", "Micro WTI"),
        ("BZ=F", "Brent crude"), ("UNG", "Natural gas ETF"), ("NG=F", "Nat gas future"),
        ("RB=F", "Gasoline"),
    ],
    "Agriculture": [
        ("ZC=F", "Corn"), ("ZW=F", "Wheat"), ("ZS=F", "Soybeans"),
        ("KC=F", "Coffee"), ("SB=F", "Sugar"), ("CC=F", "Cocoa"), ("CT=F", "Cotton"),
    ],
    "FX": [
        ("EURUSD=X", "EUR/USD"), ("6E=F", "Euro FX future"), ("GBPUSD=X", "GBP/USD"),
        ("USDJPY=X", "USD/JPY"), ("USDCHF=X", "USD/CHF"), ("AUDUSD=X", "AUD/USD"),
        ("USDCAD=X", "USD/CAD"), ("DX=F", "US Dollar index"),
    ],
    # Crypto: any of these works with the FREE 1-minute scalp feed (Binance public REST, keyless) —
    # the doctrine's ideal instrument (high vol + divisible lots). Tickers are yfinance "-USD" style
    # and map to Binance USDT pairs (BTC-USD→BTCUSDT…) for both the daily fallback and the 1m feed.
    "Crypto (Binance free 1m)": [
        ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
        ("BNB-USD", "BNB"), ("XRP-USD", "XRP"), ("ADA-USD", "Cardano"),
        ("DOGE-USD", "Dogecoin"), ("AVAX-USD", "Avalanche"), ("LINK-USD", "Chainlink"),
        ("DOT-USD", "Polkadot"), ("LTC-USD", "Litecoin"), ("BCH-USD", "Bitcoin Cash"),
        ("TRX-USD", "Tron"), ("ATOM-USD", "Cosmos"), ("ETC-USD", "Ethereum Classic"),
        ("XLM-USD", "Stellar"), ("NEAR-USD", "NEAR"), ("FIL-USD", "Filecoin"),
        ("UNI-USD", "Uniswap"), ("AAVE-USD", "Aave"), ("ICP-USD", "Internet Computer"),
        ("APT-USD", "Aptos"), ("ARB-USD", "Arbitrum"), ("OP-USD", "Optimism"),
        ("INJ-USD", "Injective"), ("SUI-USD", "Sui"), ("HBAR-USD", "Hedera"),
        ("VET-USD", "VeChain"), ("ALGO-USD", "Algorand"), ("SHIB-USD", "Shiba Inu"),
    ],
    "Crypto (equity wrappers)": [
        ("BITO", "Bitcoin strategy ETF"), ("IBIT", "iShares Bitcoin ETF"),
        ("MSTR", "MicroStrategy"), ("COIN", "Coinbase"),
    ],
    "Mega-cap stocks": [
        ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "Nvidia"),
        ("AMZN", "Amazon"), ("GOOGL", "Alphabet"), ("META", "Meta"),
        ("TSLA", "Tesla"), ("BRK-B", "Berkshire B"),
    ],
}


def flat() -> list[tuple[str, str]]:
    """[(ticker, 'TICKER — label'), ...] for a flat dropdown."""
    out: list[tuple[str, str]] = []
    for group, items in CATALOG.items():
        for tk, label in items:
            out.append((tk, f"{tk} — {label}  [{group}]"))
    return out


def flat_with_group() -> list[tuple[str, str, str]]:
    """[(ticker, label, group), ...] — for the cross-instrument scan."""
    return [(tk, label, group)
            for group, items in CATALOG.items()
            for tk, label in items]


def tickers() -> list[str]:
    return [tk for items in CATALOG.values() for tk, _ in items]
