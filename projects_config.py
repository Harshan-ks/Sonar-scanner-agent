# projects_config.py
"""
Single source of truth for mapping a SonarQube project key to its local
filesystem checkout. Shared by main.py (scan-all) and webhook_listener.py
(the /sonar-webhook and /trigger routes) so both resolve a project's root
the same way, from the same file.
"""
from pathlib import Path

import yaml

PROJECTS_CONFIG_FILE = Path(__file__).parent / "projects.yaml"


def load_projects_config():
    if not PROJECTS_CONFIG_FILE.exists():
        return {}
    with open(PROJECTS_CONFIG_FILE) as f:
        data = yaml.safe_load(f) or {}
    return data.get("projects", {})
