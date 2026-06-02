"""
Phase 2 — EML primitive in PyTorch.

STATUS: STUB. Interface defined, implementation pending.

Per the project plan (vault: projects/eml-neural-ode-polymarket.md, Phase 2),
this module must:

1. Implement `eml(x, y) = exp(x) - ln(y)` with `complex128` and careful
   overflow / branch-cut handling.
2. Implement parameterized EML trees with Gumbel-softmax-gated input choice.
3. Implement training recipe: Adam → hardening phase → snap to nearest vertex.
4. Reproduce Odrzywołek (2026) verification results: 100% snap recovery at
   depth 2, ~25% at depths 3-4, <1% at depth 5 — gate Phase 3 on this.

Do not import torch from this package's top-level __init__ — torch is in the
[research] optional extra and a bare `import polybot_research.eml_node.eml`
should not crash a base install.
"""

__all__: list[str] = []
