# AWS EC2 Assessment 1: SRE Drill for EC2 Performance Degradation  

The application exposes one endpoint:

```text
GET /health
```

When EC2 system health is degrade, the endpoint returns HTTP `503`:

```json
{"status":"unhealthy"}
```

## AWS specifications

| Resource         | Specification           |
| ---------------- | ----------------------- |
| EC2 instance     | `t2.micro`              |
| Operating system | Ubuntu Server 22.04 LTS |
| Root EBS volume  | 10 GiB                  |
| EBS volume type  | `gp3`                   |
| Public IPv4      | Enabled                 |
| Uvicorn workers  | 1                       |

### Security Group

| Port   | Source                                       | Purpose    |
| ------ | -------------------------------------------- | ---------- |
| TCP 22 | Student public IP                            | SSH        |
| TCP 80 | Student public IP or required client network | Nginx HTTP |

Do not expose port `3000` publicly. Uvicorn should listen only on `127.0.0.1`, with Nginx configured as a reverse proxy.

## Deployment

Connect to EC2:

```bash
chmod 400 <custom-private-key>.pem
ssh-add <custom-private-key>.pem
ssh ubuntu@EC2_PUBLIC_IP
```

Install the required packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx
```

Clone the repository:

```bash
git clone REPOSITORY_URL
cd REPOSITORY_DIR
```

Create the log directory:

```bash
sudo mkdir -p /var/log/storage-breaker
sudo chown -R ubuntu:ubuntu /var/log/storage-breaker
```

Install the Python dependencies:

```bash
cd /opt/storage-breaker

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

Start the application:

```bash
cd /opt/storage-breaker

.venv/bin/uvicorn app:app \
  --host 127.0.0.1 \
  --port 3000 \
  --workers 1 \
  --no-access-log
```

Configure Nginx as a reverse proxy from port `80` to:

```text
http://127.0.0.1:3000
```

Test the application locally:

```bash
curl -i http://127.0.0.1:3000/health
```

Test through Nginx:

```bash
curl -i http://EC2_PUBLIC_IP/health
```

Do not use more than one Uvicorn worker. Each worker starts an additional log-generation thread and increases the disk-consumption rate.
