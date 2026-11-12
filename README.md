# Altron Contact Centre Health Check — V1.1

Polls AudioCodes and Genesys Cloud infrastructure on a configured schedule,
generates a unified PDF health check report, and emails it to configured recipients.
Also sends a daily Genesys Cloud User Login Report. Runs as a native Windows Service.

---

## What's new in V1.1

- **AudioCodes SSL resilience** — HTTPS calls to AudioCodes devices retry up to 3 times
  (3s → 6s back-off) on SSL handshake failures. On service start, the service waits for the
  network/TLS stack (TCP probe to the first device on port 443, max 120s) before the first run.
- **Genesys Cloud User Login Report** — daily PDF + CSV email listing active users who have
  not logged in for `thresholdDays` (default 30) and users who have never logged in.
  Columns: Display Name, Division, Last Login Date.
- **Log handler fix** — each run now releases its log file handler, so runs inside the
  long-lived service no longer write into earlier runs' log files.

---

## Production Deployment — File Layout

```
C:\Altron CX\Altron Health Checks\
    main.py
    service.py
    install_service.py
    healthcheck_config.yaml          (create from healthcheck_config.example.yaml — not in git)
    healthcheck_config.example.yaml
    requirements.txt
    Altron_HealthCheck_Support_Guide.pdf
    healthcheck\
        __init__.py
        audiocodes_alarm_reporter.py
        genesys_health_reporter.py
        genesys_user_login_reporter.py
        pdf_merger.py
        email_sender.py
```

---

## Go-Live Checklist

- [ ] Copy `healthcheck_config.example.yaml` to `healthcheck_config.yaml` (git-ignored — never commit credentials)
- [ ] Fill in `smtp.host` with your real relay IP
- [ ] Fill in `genesys.clientId` and `clientSecret`
- [ ] Fill in `audiocodes.authHeader`, `devices` and `deviceCountry`
- [ ] Set `reportDir` and `logDir` to your preferred output paths
- [ ] Add **Directory > User > View** to the Genesys OAuth client's role (user login report)
- [ ] Test both reports manually (see Quick Reference)
- [ ] Run `python install_service.py` from an Administrator terminal
- [ ] Confirm `Scheduled daily user login report at 06:00` in `healthcheck_service.log`
- [ ] Confirm first run in `services.msc` and check both emails

---

## User Login Report — configuration

```yaml
userLoginReport:
  enabled: true          # false = not scheduled
  time: "06:00"          # HH:MM, server local time
  thresholdDays: 30      # inactivity threshold
  subject: "Genesys Cloud User Login Report"
```

- Recipients: `smtp.to` (same as the health check).
- Source: `GET /api/v2/users?state=active&expand=dateLastLogin`. Active users only.
- If no user at all returns a last-login date, the email subject reads
  `DATA WARNING — check log` instead of reporting everyone as never logged in.
- When both reports are due at the same time they run one after the other
  (health check first), never in parallel.
- Each run writes its own log: `userlogin_YYYYMMDD_HHMMSS.log` in `logDir`.

---

## Quick Reference

| Task | Command |
|---|---|
| Install service | `python install_service.py` |
| Uninstall service | `python install_service.py --remove` |
| Check service status | `sc query AltronHealthCheck` |
| Start service | `sc start AltronHealthCheck` |
| Stop service | `sc stop AltronHealthCheck` |
| Run health check manually | `python main.py` |
| Run user login report manually | `python main.py --report user-login` |
| Validate config | `python -c "import yaml; yaml.safe_load(open('healthcheck_config.yaml')); print('OK')"` |

---

## Documentation

See `Altron_HealthCheck_Support_Guide.pdf` for full installation, configuration,
troubleshooting, and operations documentation. The guide predates V1.1 and does not
yet cover the user login report or the SSL retry behaviour described above.
