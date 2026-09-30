from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, order=True)
class CollectionItem:
    name: str
    version: str
    url: str
    checksum: str
    size: int
    server: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        if not d.get("server"):
            del d["server"]
        return d
