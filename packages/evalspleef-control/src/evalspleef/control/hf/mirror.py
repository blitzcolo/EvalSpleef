"""The one place that decides whether a Hugging Face model gets a frozen private mirror.

``models.yaml`` parsing and the endpoint manager both read this constant, so a spec without an
explicit choice means the same thing on every path. TECH-DESIGN 6.5 step 4 (ADR-v3-05) says the
mirror is on by default for the discontinued ``novita-dedicated`` adapter and optional for every
adapter that pins a revision itself; no remaining adapter needs it, and creating a mirror takes
a write-capable token, so the default stays off here. A model that asks for ``mirror: true``
always gets the ADR behaviour. Flipping the default is a one-line change of this constant.
"""

from __future__ import annotations

__all__ = ["MIRROR_DEFAULT"]

MIRROR_DEFAULT = False
