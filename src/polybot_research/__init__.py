"""
polybot_research — research / experimental modules for polymarket-bot.

Sibling package to `polybot`. Heavy dependencies (torch, torchdiffeq, mlflow,
pyarrow, polars) are gated behind the `[research]` optional-dependency group
in pyproject.toml so production installs of polybot stay lean.

Subpackages
-----------
- `polybot_research.eml_node` — EML-parameterized Neural ODE on Polymarket
  LMSR-implied-probability time series. See subpackage README and the vault
  project page at:
    Vault of Knowledge/wiki/projects/eml-neural-ode-polymarket.md

Reuses from `polybot`:
- `polybot.utils.retry.async_retry` — async exponential backoff
- `polybot.config.Settings` (pattern; research config extends it via .env)
- `polybot.api.gamma.GammaClient` (pattern; we wrap a closed-markets variant)
"""

__all__: list[str] = []
