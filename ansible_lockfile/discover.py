from __future__ import annotations

import logging
from pathlib import Path

from .galaxy import GalaxyError

logger = logging.getLogger(__name__)

# Relative to project root (directory containing ansible.cfg, or cwd).
REQUIREMENTS_CANDIDATES = (
    "requirements.yml",
    "requirements.yaml",
    "collections/requirements.yml",
    "collections/requirements.yaml",
)


def find_requirements_file(project_dir: Path) -> Path:
    """Find the first requirements.yml / requirements.yaml in known locations."""
    project_dir = project_dir.resolve()
    if project_dir.is_file():
        project_dir = project_dir.parent

    for rel in REQUIREMENTS_CANDIDATES:
        path = project_dir / rel
        if path.is_file():
            logger.info("Using requirements file %s", path)
            return path

    raise GalaxyError(
        f"No requirements.yml or requirements.yaml found under {project_dir} "
        f"(looked for: {', '.join(REQUIREMENTS_CANDIDATES)})"
    )


def project_dir_for(cfg_path: Path | None, cwd: Path | None = None) -> Path:
    """Project root: directory of ansible.cfg when found, else cwd."""
    if cfg_path is not None:
        return cfg_path.parent.resolve()
    return (cwd or Path.cwd()).resolve()
