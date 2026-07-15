# Solution: AWS EC2 Assessment 1 — Storage Breaker

## What breaks after ~2.5 hours

The app intentionally fills the root disk.

- Background thread writes JSON logs to `/var/log/storage-breaker/application.log`
- Target: **10 GiB** over **2.5 hours**
- EC2 root volume is also **10 GiB** (effective usable space is less after OS install)
- When disk usage hits **95%**, `GET /health` returns `503` with `{"status":"unhealthy"}`
- Writes may also fail with `ENOSPC` (`disk_full`)

This is expected drill behavior. Fixing **after** it occurs is OK.

---



## Incident investigation and remediation (step by step)



### Symptom

From outside the instance, the public health check fails:

```bash
curl -i http://EC2_PUBLIC_IP/health
```

```text
HTTP/1.1 503 Service Unavailable
Server: nginx/...
{"status":"unhealthy"}
```

SSH into the instance and investigate.

```bash
ssh ubuntu@EC2_PUBLIC_IP
```

---



### Step 1 — Confirm disk pressure (not inodes)

```bash
df -h /
df -i /
```

Root disk 100% full; inodes only 13%

**Finding:** `/` is **100%** used (`Avail: 0`). Inode usage is only **13%**, so this is block-space exhaustion, not inode exhaustion.

---



### Step 2 — Find which top-level directory is large

```bash
sudo du -xh / --max-depth=1 2>/dev/null | sort -h
```

Top-level disk usage; /var is 4.5G

**Finding:** `/var` is the largest directory (**4.5G**).

---



### Step 3 — Narrow into `/var/log`

```bash
sudo du -sh /var/log/* 2>/dev/null | sort -h
```

/var/log/storage-breaker is 4.0G

**Finding:** `/var/log/storage-breaker` is **4.0G**.

---



### Step 4 — Identify the exact file

```bash
ls -lh /var/log/storage-breaker/
du -sh /var/log/storage-breaker/*
ls -lh /var/log/storage-breaker/application.log
```

Directory listing shows application.log at 4.0Gdu confirms application.log is 4.0Gapplication.log size before fix

**Finding / root cause:** `application.log` grew to **~4.0G** and filled the root volume. The app’s background writer targets ~10 GiB of logs over 2.5 hours; once usage ≥ 95%, `/health` returns unhealthy.

---



### Step 5 — Check whether the service is still running

```bash
sudo systemctl status storage-breaker
sudo journalctl -u storage-breaker -n 50 --no-pager
```

storage-breaker.service is active (running)journalctl shows service history

**Finding:** The process can still be **active (running)** while health is unhealthy — the failure mode is disk pressure / write failures, not necessarily a crashed unit.

---



### Step 6 — Free space (truncate the log)

```bash
sudo truncate -s 0 /var/log/storage-breaker/application.log
# alternative:
# sudo rm -f /var/log/storage-breaker/application.log
```

truncate application.log to 0 bytes

---



### Step 7 — Confirm disk recovered

```bash
df -h /
```

Disk usage dropped to ~40% after truncate

**Result:** Usage dropped from **100%** to about **40%**, with free space available again.

---



### Step 8 — Verify health recovered

Direct to Uvicorn:

```bash
curl -i http://127.0.0.1:3000/health
```

Uvicorn /health returns 200 healthy

Through Nginx (port 80):

```bash
curl -i http://127.0.0.1/health
```

Nginx /health returns 200 healthy

Also from your laptop:

```bash
curl -i http://EC2_PUBLIC_IP/health
```

**Expected:** `HTTP/1.1 200 OK` and `{"status":"healthy"}`.

---



### Investigation summary


| Step    | Check                       | Result                       |
| ------- | --------------------------- | ---------------------------- |
| Symptom | External `/health`          | `503 unhealthy`              |
| Disk    | `df -h /`                   | Root **100%** full           |
| Inodes  | `df -i /`                   | Only **13%** — not the issue |
| Hotspot | `du` on `/` then `/var/log` | `/var/log/storage-breaker`   |
| Culprit | `application.log`           | ~**4.0G** log file           |
| Fix     | `truncate -s 0`             | Disk back to ~**40%**        |
| Verify  | curl `/health`              | `200 healthy`                |


The `truncate` above is only the immediate recovery. The permanent fix is below.

---

## Long-term fix

`truncate -s 0` frees space once but does nothing to prevent recurrence. The
writer targets ~10 GiB of logs, so the file simply grows back and refills the
root volume within about an hour.

### Why time-based truncation does not work

A cron job that truncates on a fixed schedule races the failure:

- The disk reaches ~100% (and crosses the 95% unhealthy threshold earlier) in
  roughly **one hour**, not the nominal 2.5 h — the root volume is only 10 GiB.
- A **2.5 h** interval leaves the instance unhealthy for most of each cycle.
- A **1 h** interval has effectively no safety margin: any drift, reboot, or
  variance in write rate pushes usage past 95% before the next run.
- Truncation also discards **all** logs each time, so there is no history left
  to debug the next incident.

A fixed schedule is the wrong trigger. The correct trigger is **size**.

### Chosen fix — size-based rotation in the app

The writer now caps the log file by size and keeps a bounded number of
rotated backups, so disk usage is self-limiting regardless of write rate. This
is race-free (tied to size, not the clock) and preserves recent logs.

Implemented in `app.py`:

```python
LOG_MAX_BYTES = 200 * 1024**2   # cap per file
LOG_BACKUP_COUNT = 5            # rotated backups to keep

# before each write:
if get_current_log_size() >= LOG_MAX_BYTES:
    rotate_logs()   # application.log -> application.log.1, shift others
```

Worst-case footprint is bounded: `LOG_MAX_BYTES * (LOG_BACKUP_COUNT + 1)` ≈
**1.2 GiB**, well under the 10 GiB volume. Because the writer reopens the file
on every write (`open("ab")`), it starts a fresh `application.log` immediately
after a rotation with no held-inode / ghost-space problem.

### Defense in depth (recommended alongside)

- **logrotate** (OS-level equivalent) if you cannot change the app:

```
# /etc/logrotate.d/storage-breaker
/var/log/storage-breaker/*.log {
    maxsize 200M
    rotate 5
    missingok
    notifempty
    compress
    delaycompress
    create 0644 ubuntu ubuntu
}
```

Run it frequently (logrotate's default daily cron is far too slow for this
fill rate) via a systemd timer every ~2 min.

- **Separate EBS volume** mounted at `/var/log/storage-breaker` so log growth
  can never fill the root/OS filesystem.
- **Monitoring/alerting**: CloudWatch agent alarm on `disk_used_percent` at
  ~80% so pressure is caught early instead of at 95%.

Increasing the EBS volume size alone is **not** a fix — it only delays the same
failure.

---



## Deploy on EC2



### 1. Install packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx
```

If `python3 -m venv` fails with `ensurepip is not available`:

```bash
sudo apt install -y python3-venv python3-pip
```



### 2. App directories and clone

```bash
sudo mkdir -p /opt/storage-breaker
sudo chown -R ubuntu:ubuntu /opt/storage-breaker

sudo mkdir -p /var/log/storage-breaker
sudo chown -R ubuntu:ubuntu /var/log/storage-breaker

cd /opt/storage-breaker
git clone <REPOSITORY_URL>
```

App path used here:

```text
/opt/storage-breaker/ape-aws-ec2-assessment-1
```



### 3. Virtualenv and dependencies

```bash
cd /opt/storage-breaker
rm -rf .venv   # only if a previous venv failed
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r ape-aws-ec2-assessment-1/requirements.txt
```



### 4. systemd unit

```bash
sudo nano /etc/systemd/system/storage-breaker.service
```

```ini
[Unit]
Description=Storage Breaker FastAPI App
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/storage-breaker/ape-aws-ec2-assessment-1
ExecStart=/opt/storage-breaker/.venv/bin/uvicorn app:app \
  --host 127.0.0.1 \
  --port 3000 \
  --workers 1 \
  --no-access-log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Important:

- `WorkingDirectory` must be the folder that contains `app.py`
- Use **1** Uvicorn worker only

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now storage-breaker
sudo systemctl status storage-breaker
```

Test (wait a second after start so uvicorn can bind):

```bash
curl -i http://127.0.0.1:3000/health
```

Expected:

```text
HTTP/1.1 200 OK
{"status":"healthy"}
```

Useful commands:

```bash
sudo journalctl -u storage-breaker -n 50 --no-pager
sudo journalctl -u storage-breaker -f
sudo systemctl restart storage-breaker
sudo systemctl stop storage-breaker
```



### 5. Nginx reverse proxy (required)

Do **not** expose port `3000` publicly. Nginx listens on port `80` and proxies to `127.0.0.1:3000`.

One-time setup:

```bash
sudo apt update
sudo apt install -y nginx

sudo tee /etc/nginx/sites-available/storage-breaker >/dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/storage-breaker /etc/nginx/sites-enabled/storage-breaker
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

curl -i http://127.0.0.1/health
curl -i http://EC2_PUBLIC_IP/health
```

Security group: allow inbound **TCP 80** (and SSH 22). Do not open **3000**.

---



## Troubleshooting faced during setup



### `python3 -m venv` fails (`ensurepip is not available`)

```bash
rm -rf /opt/storage-breaker/.venv
sudo apt update
sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
```

Do not rely on `apt install python3.14-venv` if that package has no candidate; use `python3-venv`.

### systemd: `Could not import module "app"`

Cause: wrong `WorkingDirectory` (e.g. `/opt/storage-breaker` instead of the repo folder).

Fix: set

```ini
WorkingDirectory=/opt/storage-breaker/ape-aws-ec2-assessment-1
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart storage-breaker
```



### `curl: Failed to connect` right after `systemctl start`

Uvicorn needs a moment to bind. Retry after ~1–2 seconds, or check:

```bash
sudo systemctl status storage-breaker
sudo journalctl -u storage-breaker -n 50 --no-pager
```
