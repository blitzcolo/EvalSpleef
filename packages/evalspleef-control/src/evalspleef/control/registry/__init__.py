"""Model registry on top of the control-plane store.

``ModelRegistry`` persists immutable SPI :class:`~evalspleef.spi.model.ModelSpec`
versions through :class:`~evalspleef.control.store.repos.ModelSpecRepository`
(FR-MOD-01: any change is a new ``spec_version``) and resolves ``slug@version``
references. The judge family rule of PRD appendix C.5 lives here as a pure
helper so planners and CLI commands share one implementation.
"""

from .model_registry import ModelNotFoundError, ModelRegistry, SameFamilyJudgeError
from .serialization import model_spec_from_row, model_spec_to_row

__all__ = [
    "ModelNotFoundError",
    "ModelRegistry",
    "SameFamilyJudgeError",
    "model_spec_from_row",
    "model_spec_to_row",
]
