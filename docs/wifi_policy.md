# Arduino WiFi Policy

`bsm_web` stores the Gateway-wide Arduino WiFi policy in:

```text
config/wifi_policy.json
```

`bsm_network` reads that file on each discovery cycle and sends the policy to reachable Arduinos with:

```text
SET_WIFI_POLICY,<POLICY>,GRACE_MIN=<minutes>,WAKE_HOUR=<hour>
```

Automatic policy application is skipped for Arduino firmware older than `4.2`.

Supported policies:

```text
STAY_ACTIVE
MORNING_ONLY
```

`STAY_ACTIVE` is the safe default and preserves the old behavior: after upload, the Arduino stays available for the rest of the WiFi window.

`MORNING_ONLY` allows the Arduino to complete TRIM/upload communication, remain active for `GRACE_MIN`, then drop WiFi until `WAKE_HOUR`. At `WAKE_HOUR`, it returns to active WiFi mode for the remainder of the configured WiFi window.

Safety behavior:

- Arduino reboot defaults back to `STAY_ACTIVE`.
- Invalid or unknown policy values resolve to `STAY_ACTIVE`.
- The policy only quiets WiFi after READY is acknowledged or upload is complete.
- Active file transfer is not interrupted by the policy.
