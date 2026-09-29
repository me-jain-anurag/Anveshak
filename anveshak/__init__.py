"""Anveshak — evidence-first attribution of crypto wallets to the nearest VASP.

Design rule (ADR-0002): nothing in this package produces a probability or an ML/LLM score.
Every output is either an on-chain fact that can be re-verified, a labelled claim with a
named source, or a deterministic rule applied to those — or it says "unknown".
"""

__version__ = "0.1.0"
