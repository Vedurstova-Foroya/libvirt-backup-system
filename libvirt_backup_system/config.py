from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from .config_data import COMMENTED_ENV_KEYS, CONFIG_KEYS, DEFAULTS, ENV_TEMPLATE
from .logging_json import event


def _read_machine_id(prefix: Path) -> str:
    path = prefixed("/etc/machine-id", prefix)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def root_prefix(value: str | None = None) -> Path:
    raw = value if value is not None else os.environ.get("LIBVIRT_BACKUP_ROOT_PREFIX", "/")
    return Path(raw).resolve()


def prefixed(path: str | Path, prefix: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        return prefix / path
    return prefix / str(path).lstrip("/")


def default_config_path(prefix: Path | None = None) -> Path:
    return prefixed("/etc/libvirt-backup-system/libvirt-backup.env", prefix or root_prefix())


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                value = shlex.split(value)[0]
            except ValueError:
                value = value[1:-1]
        values[key] = value
    return values


def bool_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_value(values: dict[str, str], key: str) -> int:
    return int(values[key])


def float_value(values: dict[str, str], key: str) -> float:
    return float(values[key])


def split_words(value: str) -> list[str]:
    return [item.strip() for item in value.replace(",", " ").split() if item.strip()]


# ``frozen=True`` here protects the three attribute bindings (``values``,
# ``path``, ``prefix``) from rebind after construction — it does NOT freeze the
# ``values`` dict's contents. ``installer.install`` deliberately mutates
# ``values`` in-place to apply ``INSTALL_TIME_ENV_KEYS`` from the process
# environment on a first install, and the unit tests rely on the same pattern.
# If you need a true immutable view, copy ``values`` at the boundary; do not
# remove ``frozen=True`` without auditing every install/test path.
@dataclass(frozen=True)
class Config:
    values: dict[str, str]
    path: Path
    prefix: Path

    @classmethod
    def load(
        cls,
        config_path: str | None = None,
        prefix: str | None = None,
        *,
        apply_env_overrides: bool = True,
    ) -> Config:
        root = root_prefix(prefix)
        raw_path = config_path or os.environ.get("LIBVIRT_BACKUP_CONFIG") or str(default_config_path(root))
        path = Path(raw_path)
        values = dict(DEFAULTS)
        values.update(parse_env_file(path))
        if apply_env_overrides:
            for key in CONFIG_KEYS:
                if key in os.environ:
                    env_value = os.environ[key]
                    if values.get(key) != env_value:
                        event("info", "env override", key=key, source="environ")
                    values[key] = env_value
        if not values.get("HOST_ID"):
            # Fall back to /etc/machine-id. If the file is missing or empty
            # leave HOST_ID="" so _validate_required_present surfaces a clean
            # "HOST_ID must not be empty".
            values["HOST_ID"] = _read_machine_id(root)
        return cls(values=values, path=path, prefix=root)

    def get(self, key: str) -> str:
        return self.values[key]

    def path_value(self, key: str) -> Path:
        return Path(self.values[key])

    def enabled(self, key: str) -> bool:
        return bool_value(self.values[key])

    @property
    def blacklist(self) -> set[str]:
        return set(split_words(self.values["VM_BLACKLIST"]))

    def render_env(self) -> str:
        lines: list[str] = []
        for item in ENV_TEMPLATE:
            if item is None:
                lines.append("")
            elif item in DEFAULTS:
                prefix = "# " if item in COMMENTED_ENV_KEYS else ""
                lines.append(f"{prefix}{item}={self.values.get(item, DEFAULTS[item])}")
            else:
                lines.append(item)
        return "\n".join(lines) + "\n"
