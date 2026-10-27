"""The one error type every Kampung operation raises."""


class KampungError(Exception):
    """A failure the caller can act on, reported as {"error", "hint"}."""

    def __init__(self, code: str, hint: str) -> None:
        super().__init__(f"{code}: {hint}")
        self.code = code
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "hint": self.hint}
