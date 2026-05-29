"""Web layer: FastAPI JSON API + static Plotly frontend.

The math core (simcore/data/atr_strategy/options) is reused unchanged. This package only
adds transport, validation, signal ingestion (TradingView webhooks) and serialization.
Designed stateless + config-driven so it scales horizontally behind a load balancer.
"""
