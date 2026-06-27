from __future__ import annotations

import json
from pathlib import Path

DEFAULT_WIFI_POLICY_PATH = "config/wifi_policy.json"
POLICY_STAY_ACTIVE = "STAY_ACTIVE"
POLICY_MORNING_ONLY = "MORNING_ONLY"
ALLOWED_WIFI_POLICIES = {POLICY_STAY_ACTIVE, POLICY_MORNING_ONLY}
DEFAULT_WIFI_POLICY = {
    "policy": POLICY_STAY_ACTIVE,
    "grace_min": 60,
    "wake_hour": 16,
}


def normalize_wifi_policy(policy: dict[str, object] | None) -> dict[str, object]:
    """Return a validated WiFi policy with safe defaults."""
    raw = policy or {}
    mode = str(raw.get("policy", DEFAULT_WIFI_POLICY["policy"])).strip().upper()
    if mode not in ALLOWED_WIFI_POLICIES:
        mode = POLICY_STAY_ACTIVE

    try:
        grace_min = int(str(raw.get("grace_min", DEFAULT_WIFI_POLICY["grace_min"])).strip())
    except Exception:
        grace_min = int(DEFAULT_WIFI_POLICY["grace_min"])
    grace_min = max(0, min(360, grace_min))

    try:
        wake_hour = int(str(raw.get("wake_hour", DEFAULT_WIFI_POLICY["wake_hour"])).strip())
    except Exception:
        wake_hour = int(DEFAULT_WIFI_POLICY["wake_hour"])
    if wake_hour < 0 or wake_hour > 23:
        wake_hour = int(DEFAULT_WIFI_POLICY["wake_hour"])

    return {
        "policy": mode,
        "grace_min": grace_min,
        "wake_hour": wake_hour,
    }


def load_wifi_policy(path_value: str = DEFAULT_WIFI_POLICY_PATH) -> dict[str, object]:
    """Load Gateway WiFi policy, falling back to safe defaults."""
    path = Path(path_value).expanduser()
    if not path.exists():
        return dict(DEFAULT_WIFI_POLICY)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_WIFI_POLICY)
    if not isinstance(data, dict):
        return dict(DEFAULT_WIFI_POLICY)
    return normalize_wifi_policy(data)


def save_wifi_policy(policy: dict[str, object], path_value: str = DEFAULT_WIFI_POLICY_PATH) -> dict[str, object]:
    """Validate and persist Gateway WiFi policy."""
    clean = normalize_wifi_policy(policy)
    path = Path(path_value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return clean


def build_wifi_policy_command(policy: dict[str, object]) -> str:
    """Build Arduino SET_WIFI_POLICY command."""
    clean = normalize_wifi_policy(policy)
    return (
        f"SET_WIFI_POLICY,{clean['policy']},"
        f"GRACE_MIN={clean['grace_min']},WAKE_HOUR={clean['wake_hour']}"
    )
