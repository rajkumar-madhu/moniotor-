"""Load Azure AD config with client secret from Vault / environment."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from ms_identity_web.configuration import AADConfig


def load_aad_config(base_dir: str, get_secret) -> SimpleNamespace:
    config_path = os.path.join(base_dir, "login", "aad.config.json")
    with open(config_path, encoding="utf-8") as handle:
        raw = json.load(handle)

    secret = get_secret("AZURE_CLIENT_SECRET", env_var="AZURE_CLIENT_SECRET", default="")
    if secret:
        raw.setdefault("client", {})["client_credential"] = secret

    parsed = json.loads(json.dumps(raw), object_hook=lambda d: SimpleNamespace(**d))
    AADConfig.sanity_check_configs(parsed)
    return parsed
