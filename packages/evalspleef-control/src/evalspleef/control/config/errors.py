"""Configuration errors that name the file and the path of the problem."""

from __future__ import annotations

from evalspleef.spi.errors import SpleefError


class ConfigError(SpleefError):
    """A configuration file failed to load or validate.

    ``file`` is the path of the offending document, ``path`` the dotted or
    bracketed location inside it (for example ``models[1].source.revision``).
    The message never contains secret values.
    """

    error_class = "version"
    disposition = "manual"

    def __init__(self, file: str, path: str, message: str) -> None:
        where = f"{file}: {path}: " if path else f"{file}: "
        super().__init__(where + message)
        self.file = file
        self.path = path

    @staticmethod
    def _display_loc(loc: object) -> str:
        parts: list[str] = []
        for item in loc if isinstance(loc, (list, tuple)) else (loc,):
            if isinstance(item, int):
                parts.append(f"[{item}]")
            else:
                parts.append(f"{item}" if not parts else f".{item}")
        return "".join(parts)

    @classmethod
    def from_validation(cls, file: str, exc: Exception) -> ConfigError:
        """Build one precise error from a pydantic ``ValidationError``."""
        errors = getattr(exc, "errors", None)
        if callable(errors):
            first = errors()[0]
            path = cls._display_loc(first.get("loc", ()))
            message = first.get("msg", "invalid value")
            if first.get("type"):
                message = f"{message} ({first['type']})"
            return cls(file, path, message)
        return cls(file, "", str(exc))
