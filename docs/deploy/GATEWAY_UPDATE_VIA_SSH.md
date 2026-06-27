# Gateway Update via SSH

Use this procedure to update `bsm_web` and `bsm_network` on the Gateway after
changes have been pushed to GitHub.

## Connect

```bash
ssh recomputer@<gateway-ip>
```

If using Tailscale, `<gateway-ip>` may be the Gateway Tailscale IP.

## Pull Latest Code

```bash
cd ~/Arduino_WIFI
git status
git pull
```

If `git status` shows unexpected local changes, stop and inspect before pulling.

## Find Service Names

The service names may use hyphens instead of underscores. List matching services:

```bash
systemctl list-units --type=service | grep -i bsm
```

Also check installed service files:

```bash
systemctl list-unit-files | grep -i bsm
```

## Restart Services

Use the exact names shown by `systemctl`. Common examples:

```bash
sudo systemctl restart bsm-web
sudo systemctl restart bsm-network
```

Then check status:

```bash
sudo systemctl status bsm-web
sudo systemctl status bsm-network
```

If a service name is different, substitute the actual name.

## Check Logs

Follow live logs:

```bash
journalctl -u bsm-web -f
journalctl -u bsm-network -f
```

Show recent logs:

```bash
journalctl -u bsm-web -n 100
journalctl -u bsm-network -n 100
```

## If No Service Is Found

Check whether the process is running manually or under another manager:

```bash
ps aux | grep -i bsm_web
ps aux | grep -i bsm_network
```

If the service names are missing, inspect the Gateway setup docs and systemd
unit files before changing anything.
