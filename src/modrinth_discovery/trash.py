from __future__ import annotations

import os
import uuid
from pathlib import Path
from .models import Instance, InstalledMod


class TrashManager:
    def __init__(self, root: Path) -> None: self.root=root

    def remove(self, instance: Instance, mods: list[InstalledMod]) -> list[Path]:
        identity=instance.instance_id or instance.path.name
        target_dir=self.root/_safe(identity); target_dir.mkdir(parents=True,exist_ok=True)
        moved=[]
        originals=[]
        try:
            for mod in mods:
                source=mod.path.resolve()
                if source.parent != instance.mods_directory.resolve():
                    raise ValueError(f"mods 디렉터리 밖의 파일은 제거할 수 없습니다: {source}")
                target=target_dir/f"{uuid.uuid4().hex}-{source.name}"
                os.replace(source,target); moved.append(target); originals.append(source)
        except Exception:
            for source,target in reversed(list(zip(originals,moved))):
                if target.exists(): os.replace(target,source)
            raise
        return moved


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value) or "instance"
