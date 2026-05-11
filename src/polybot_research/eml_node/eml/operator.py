"""
The EML operator: ``eml(x, y) = exp(x) - ln(y)``.

Implements the Sheffer-stroke for elementary functions per Odrzywołek (2026).
See vault: wiki/concepts/eml-operator.md.

STATUS
------
- ``eml`` (the raw operator): IMPLEMENTED with clamps for numerical stability.
- ``EMLNode`` and ``EMLTree``: STUB. Implement in Phase 2.

Numerical stability notes (from the paper, Section 4.1)
-------------------------------------------------------
- ``exp(x)`` overflows fast. Clamp ``x`` to a safe range before evaluation.
- ``ln(y)`` is undefined at ``y=0`` and discontinuous across the negative real
  axis. The paper uses the principal branch via ``ln(z) = ln|z| + i·arg(z)``;
  ``torch.log`` on ``complex128`` does this by default.
- Bookkeeping: the simplest EML form of ``ln(z)`` is
  ``ln(z) = e - log(e^e / z)`` which jumps by ``2πi`` for negative real ``z``.
  Either accept the jump and correct downstream, or redefine the branch for
  EML itself — see the paper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import torch

# Reasonable-default safety clamps. The paper notes that bare ``exp`` of
# unconstrained training-time logits goes inf almost immediately under Adam.
DEFAULT_EXP_CLAMP_MAX = 50.0  # exp(50) ~ 5e21 — close to BigDecimal headroom
DEFAULT_LN_EPSILON = 1e-30  # avoids ln(0) without distorting the gradient much


def eml(
    x: "torch.Tensor",
    y: "torch.Tensor",
    *,
    exp_clamp_max: float = DEFAULT_EXP_CLAMP_MAX,
    ln_epsilon: float = DEFAULT_LN_EPSILON,
) -> "torch.Tensor":
    """
    Compute ``exp(x) - ln(y)`` with numerical safety clamps.

    Both inputs should be ``complex128`` tensors per the paper's recipe;
    real-only inputs work but lose access to constants like π and i which
    require complex intermediates via Euler's formula.

    Parameters
    ----------
    x, y : complex128 tensors of broadcastable shape
    exp_clamp_max : clamp the real part of ``x`` to ``≤ exp_clamp_max`` before
        exponentiating. Prevents overflow during early training.
    ln_epsilon : floor on ``|y|`` to avoid ``ln(0)``.

    Returns
    -------
    Same shape as broadcast(x, y).
    """
    import torch  # local import — torch is in the [research] extra

    # Clamp real part of x to prevent exp overflow.
    if x.is_complex():
        x_safe = torch.complex(
            torch.clamp(x.real, max=exp_clamp_max), x.imag
        )
    else:
        x_safe = torch.clamp(x, max=exp_clamp_max)

    # Floor |y| to avoid log(0). For complex y, this means scaling magnitude.
    if y.is_complex():
        magnitude = torch.abs(y).clamp(min=ln_epsilon)
        # Reconstruct y on the same direction with floored magnitude.
        unit = torch.where(
            torch.abs(y) > 0,
            y / torch.abs(y).clamp(min=ln_epsilon),
            torch.ones_like(y),
        )
        y_safe = unit * magnitude.to(y.dtype)
    else:
        y_safe = torch.where(
            torch.abs(y) > ln_epsilon,
            y,
            torch.full_like(y, ln_epsilon) * torch.sign(y).clamp(min=1.0),
        )

    return torch.exp(x_safe) - torch.log(y_safe)


class EMLNode:
    """
    A single trainable EML node with Gumbel-softmax gated input choice.

    STUB — implement in Phase 2.

    Each input slot is a linear combination ``α + β·x + γ·f`` where (α, β, γ)
    are Gumbel-softmax-derived gates over {constant 1, input variable x,
    previous EML output f}. See paper Section 4.3 (multi-parameter master formula).
    """

    def __init__(self) -> None:  # pragma: no cover — stub
        raise NotImplementedError(
            "EMLNode is a Phase 2 stub. See vault project page Phase 2 substeps."
        )


class EMLTree:
    """
    A trainable tree of EML nodes, depth-K binary.

    STUB — implement in Phase 2.

    The grammar is ``S → 1 | eml(S, S)``; a depth-K tree has ``2^K`` leaves
    and ``2^K - 1`` internal EML nodes. Total trainable parameters scale as
    ``5 · 2^K - 6`` per the paper.
    """

    def __init__(self, depth: int) -> None:  # pragma: no cover — stub
        raise NotImplementedError(
            "EMLTree is a Phase 2 stub. See vault project page Phase 2 substeps."
        )
