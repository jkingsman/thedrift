from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ActionResult:
    success: bool
    message: str
    data: Optional[dict] = None
    pending: list[dict] = field(default_factory=list)
    state: Optional[dict] = None
    share: Optional[dict] = None

    def to_dict(self) -> dict:
        d: dict = {"success": self.success, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        if self.pending:
            d["pending"] = self.pending
        if self.state is not None:
            d["state"] = self.state
        if self.share is not None:
            d["share"] = self.share
        return d
