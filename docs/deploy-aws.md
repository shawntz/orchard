# Deploying Orchard on AWS (EC2 + S3)

This guide runs Orchard as a shared website on one EC2 instance:

- Researchers sign in through a login page before they reach Orchard.
- Searches, EyeLink scans and repository sweeps run as **background jobs** on the server. They keep running after the browser closes, and **every signed-in user can watch their progress**.
- Every action is recorded in Orchard's append-only **provenance log (audit trail) under the username that performed it**. That includes the events a background job writes for whoever started it.
- The database and PDFs are continuously **backed up to S3**, and the server can be rebuilt from S3.

Commands use these placeholders. Replace them everywhere:

| Placeholder | Example |
|---|---|
| `orchard.example.org` | DNS name you will point at the server |
| `orchard-data-123456789012` | S3 bucket name (must be globally unique) |
| `us-west-2` | AWS region |

---

## 1. How the pieces fit

```text
 Browser ──HTTPS──▶ nginx (EC2, ports 80/443)
                     │  TLS certificate from Let's Encrypt
                     │  login (HTTP basic auth or SSO)
                     │  sets X-Remote-User: <username>
                     ▼
                   Orchard  (systemd service, listens on 127.0.0.1:8765 only)
                     │  2 background workers: searches, EyeLink scans, sweeps
                     │  every audit event stores the acting username
                     ▼
                   /var/lib/orchard   (EBS disk: orchard.sqlite3 + pdfs/)
                     │
                     ├── Litestream ─────continuous───▶ s3://BUCKET/litestream/
                     ├── orchard-s3-sync (every 10 min) ▶ s3://BUCKET/files/
                     └── orchard-snapshot (daily) ──────▶ s3://BUCKET/snapshots/
```

**Why the live data sits on EBS, not directly on S3.** Orchard stores its data in SQLite and saves PDFs by writing a temporary file and renaming it. S3 is an object store. It provides neither the file locking SQLite's WAL mode depends on nor renames, so S3 mounts (Mountpoint for Amazon S3, s3fs) would corrupt the database or fail PDF uploads. Instead, the working copy lives on the instance's EBS volume and S3 holds the durable copy. Litestream streams every database change to S3 within about a second, and PDFs are synced as immutable, content-addressed files.

**How identity reaches Orchard.** Orchard has no password database of its own. In hosted mode it trusts exactly one request header, named by `ORCHARD_USER_HEADER`, which the proxy sets after login. Orchard refuses any request that lacks the header (HTTP 401). It still binds only to `127.0.0.1`, so the only way to reach it from the network is through nginx, and nginx overwrites the header on every request. A client therefore cannot claim someone else's name.

---

## 2. Prerequisites

- An AWS account and the AWS CLI v2 configured on your own computer (`aws sts get-caller-identity` works).
- A domain or subdomain whose DNS you control. You need it for HTTPS.
- About 30 minutes.

```bash
export REGION=us-west-2
export BUCKET=orchard-data-$(aws sts get-caller-identity --query Account --output text)
export DOMAIN=orchard.example.org
```

---

## 3. Create the S3 bucket

Run these on your own computer:

```bash
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"   # omit this line for us-east-1

# Nothing in this bucket is ever public.
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Versioning keeps prior copies if anything is overwritten or deleted.
aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled

# Encryption at rest (SSE-S3).
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Keep old object versions for 90 days, then expire them.
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration '{
  "Rules":[{"ID":"expire-old-versions","Status":"Enabled","Filter":{},
            "NoncurrentVersionExpiration":{"NoncurrentDays":90}}]}'
```

> **Tamper-evidence (optional).** For a provenance record that nobody can delete, including administrators, create the bucket with `--object-lock-enabled-for-bucket` and set a default retention on the `snapshots/` prefix. This has to be decided when the bucket is created.

---

## 4. Give the instance access to that bucket only

```bash
cat > orchard-trust.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF

cat > orchard-s3.json <<EOF
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":"arn:aws:s3:::$BUCKET"},
  {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":"arn:aws:s3:::$BUCKET/*"}
]}
EOF

aws iam create-role --role-name orchard-ec2 --assume-role-policy-document file://orchard-trust.json
aws iam put-role-policy --role-name orchard-ec2 --policy-name orchard-s3 --policy-document file://orchard-s3.json
# Lets you open a shell via Systems Manager, so port 22 can stay closed.
aws iam attach-role-policy --role-name orchard-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name orchard-ec2
aws iam add-role-to-instance-profile --instance-profile-name orchard-ec2 --role-name orchard-ec2
```

`s3:DeleteObject` is needed because Litestream prunes its own old WAL segments. With versioning on, a deleted object is still recoverable for 90 days.

---

## 5. Network and instance

```bash
VPC=$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --region "$REGION" --vpc-id "$VPC" \
      --group-name orchard-web --description "Orchard HTTPS" --query GroupId --output text)
# 80 is needed for Let's Encrypt and the HTTPS redirect. To restrict access to
# your institution, replace 0.0.0.0/0 with its CIDR range (Let's Encrypt still needs 80 open).
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" --protocol tcp --port 443 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG" --protocol tcp --port 80  --cidr 0.0.0.0/0

INSTANCE=$(aws ec2 run-instances --region "$REGION" \
  --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --instance-type t3.small \
  --security-group-ids "$SG" \
  --iam-instance-profile Name=orchard-ec2 \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1 \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":40,"VolumeType":"gp3","Encrypted":true}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=orchard}]' \
  --query 'Instances[0].InstanceId' --output text)

# A fixed public IP for DNS.
ALLOC=$(aws ec2 allocate-address --region "$REGION" --query AllocationId --output text)
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE"
aws ec2 associate-address --region "$REGION" --instance-id "$INSTANCE" --allocation-id "$ALLOC"
aws ec2 describe-addresses --region "$REGION" --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text
```

Create a DNS **A record** for `orchard.example.org` that points at the printed IP, and wait until `dig +short orchard.example.org` returns it.

**Sizing.** Orchard is a single Python process with two background workers, and most of a job's time is spent waiting on remote APIs. A `t3.small` (2 vCPU, 2 GB) is enough for a lab-sized team. PDFs make up most of the disk use, so increase `VolumeSize` if you expect many thousands of them.

Open a shell on the instance without SSH:

```bash
aws ssm start-session --region "$REGION" --target "$INSTANCE"   # requires the Session Manager plugin
```

Run everything from here on **on the instance**.

---

## 6. Install Orchard

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git nginx certbot python3-certbot-nginx \
                        apache2-utils sqlite3 poppler-utils unzip
# AWS CLI v2 (official installer; it uses the instance role automatically).
curl -fsSLo /tmp/awscliv2.zip https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip
unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install

# Dedicated service account with no login shell.
sudo useradd --system --home /var/lib/orchard --shell /usr/sbin/nologin orchard
sudo install -d -o orchard -g orchard -m 750 /var/lib/orchard

sudo git clone https://github.com/shawntz/orchard.git /opt/orchard
# Optional but recommended: better PDF text extraction.
sudo python3 -m venv /opt/orchard/.venv
sudo /opt/orchard/.venv/bin/pip install --quiet pypdf
```

Orchard itself needs no third-party packages. `pypdf` and `poppler-utils` (`pdftotext`) only improve PDF text extraction.

### Configuration

```bash
sudo install -d -m 755 /etc/orchard
sudo tee /etc/orchard/orchard.env >/dev/null <<EOF
ORCHARD_DATA_DIR=/var/lib/orchard
# Public address users type in the browser (no trailing slash, no path).
ORCHARD_PUBLIC_URL=https://$DOMAIN
# Header that nginx fills with the signed-in username. Requests without it are refused.
ORCHARD_USER_HEADER=X-Remote-User
# Optional:
# ORCHARD_UNPAYWALL_EMAIL=lab@example.org
# ORCHARD_GITHUB_TOKEN=
EOF
sudo chmod 640 /etc/orchard/orchard.env
sudo chown root:orchard /etc/orchard/orchard.env
```

### systemd service

```bash
sudo tee /etc/systemd/system/orchard.service >/dev/null <<'EOF'
[Unit]
Description=Orchard literature workspace
After=network-online.target
Wants=network-online.target

[Service]
User=orchard
Group=orchard
WorkingDirectory=/opt/orchard
EnvironmentFile=/etc/orchard/orchard.env
ExecStart=/opt/orchard/.venv/bin/python -m orchard --port 8765
# Orchard shuts down cleanly on Ctrl-C (SIGINT). Running jobs are marked
# "interrupted" at the next start and can be resumed from Search history.
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
RestartSec=5
# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/orchard

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now orchard
sudo journalctl -u orchard -n 20 --no-pager
```

The log should show:

```text
Orchard is running at http://127.0.0.1:8765
Database: /var/lib/orchard/orchard.sqlite3
Accepting proxied requests for https://orchard.example.org
Signed-in username read from the X-Remote-User header
```

**Run exactly one Orchard process against `/var/lib/orchard`.** Background jobs are coordinated inside that process. Do not scale out to several instances or containers sharing the data.

---

## 7. Logins and HTTPS (nginx)

### Create user accounts

Each person gets their own account. **The username is what appears in the provenance log**, so use stable, recognisable names (for example, institutional IDs) and never share accounts.

```bash
sudo htpasswd -cB /etc/nginx/orchard.htpasswd shawn     # -c creates the file; first user only
sudo htpasswd -B  /etc/nginx/orchard.htpasswd ashna     # add more users without -c
sudo chown root:www-data /etc/nginx/orchard.htpasswd && sudo chmod 640 /etc/nginx/orchard.htpasswd
```

To remove someone, run `sudo htpasswd -D /etc/nginx/orchard.htpasswd <name>`. Their past actions stay in the log under their name.

### Certificate

```bash
sudo certbot certonly --nginx -d "$DOMAIN" --agree-tos -m you@example.org --no-eff-email \
     --deploy-hook "systemctl reload nginx"
sudo rm -f /etc/nginx/sites-enabled/default      # replaced by the Orchard site below
```

Certbot installs a timer that renews the certificate automatically.

### Site configuration

```bash
sudo tee /etc/nginx/sites-available/orchard >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;        # nginx ≥ 1.25.1 prefers "listen 443 ssl;" plus "http2 on;"
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;

    auth_basic           "Orchard";
    auth_basic_user_file /etc/nginx/orchard.htpasswd;

    # PDF uploads can be up to 60 MB; exports and repository scans can be slow.
    client_max_body_size 64m;
    proxy_read_timeout   300s;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        # Orchard checks Host and Origin against ORCHARD_PUBLIC_URL.
        proxy_set_header Host \$host;
        # Always overwritten, so a browser cannot supply its own username.
        proxy_set_header X-Remote-User \$remote_user;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/orchard /etc/nginx/sites-enabled/orchard
sudo nginx -t && sudo systemctl reload nginx
```

### Check it

```bash
curl -sI https://$DOMAIN/ | head -1                          # HTTP/2 401  (login required)
curl -s -u shawn https://$DOMAIN/api/session                 # {"user": "shawn", "authenticated": true}
```

The trust boundary is the instance itself. Remote clients can reach Orchard only through nginx, but any process **on** the instance can call `127.0.0.1:8765` and set the header. Keep shell access limited to administrators.

Open `https://orchard.example.org`, sign in, and check that the sidebar footer reads **Signed in as shawn**.

### Using institutional single sign-on instead of passwords

nginx basic auth is the simplest setup. To use Google, Microsoft Entra, Okta, or another OIDC provider instead, run [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) on the same instance with `--set-xauthrequest`. Then replace the two `auth_basic` lines with nginx's `auth_request` pattern and pass its user header on:

```nginx
auth_request /oauth2/auth;
error_page 401 = /oauth2/sign_in;
auth_request_set $user $upstream_http_x_auth_request_user;   # or ..._email
# inside location /:
proxy_set_header X-Remote-User $user;
```

Follow the oauth2-proxy nginx guide for the `/oauth2/` locations. Orchard needs no changes either way. It reads whatever username arrives in `X-Remote-User`.

---

## 8. Continuous S3 backup

### Database: Litestream

[Litestream](https://litestream.io) replicates SQLite to S3 continuously. It picks up the instance role credentials automatically.

```bash
# Check https://github.com/benbjohnson/litestream/releases for the current release.
# The config below uses the 0.3.x format.
LS=v0.3.13
curl -fsSLo /tmp/litestream.deb \
  https://github.com/benbjohnson/litestream/releases/download/$LS/litestream-$LS-linux-amd64.deb
sudo dpkg -i /tmp/litestream.deb

sudo tee /etc/litestream.yml >/dev/null <<EOF
dbs:
  - path: /var/lib/orchard/orchard.sqlite3
    replicas:
      - url: s3://$BUCKET/litestream/orchard.sqlite3
        region: $REGION
        retention: 720h        # keep 30 days of point-in-time history
EOF

# Litestream must be able to read and write the database's WAL files.
sudo mkdir -p /etc/systemd/system/litestream.service.d
sudo tee /etc/systemd/system/litestream.service.d/user.conf >/dev/null <<'EOF'
[Service]
User=orchard
Group=orchard
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now litestream
sudo journalctl -u litestream -n 20 --no-pager
aws s3 ls "s3://$BUCKET/litestream/" --recursive | head
```

### PDFs and other files: periodic sync

PDFs are named by their SHA-256 and never change, so a plain `aws s3 sync` is safe and cheap.

```bash
sudo tee /usr/local/bin/orchard-s3-sync >/dev/null <<EOF
#!/bin/sh
set -eu
# Everything except the live SQLite files (Litestream handles those) and partial uploads.
exec aws s3 sync /var/lib/orchard "s3://$BUCKET/files/" \\
     --exclude 'orchard.sqlite3*' --exclude '*.tmp-*' --exclude '.*' --only-show-errors
EOF

sudo tee /usr/local/bin/orchard-snapshot >/dev/null <<EOF
#!/bin/sh
set -eu
# sqlite3 .backup produces a consistent copy while Orchard keeps running.
tmp=\$(mktemp -d); trap 'rm -rf "\$tmp"' EXIT
sqlite3 /var/lib/orchard/orchard.sqlite3 ".backup '\$tmp/orchard.sqlite3'"
gzip "\$tmp/orchard.sqlite3"
aws s3 cp "\$tmp/orchard.sqlite3.gz" "s3://$BUCKET/snapshots/orchard-\$(date -u +%Y%m%dT%H%MZ).sqlite3.gz" --only-show-errors
EOF
sudo chmod 755 /usr/local/bin/orchard-s3-sync /usr/local/bin/orchard-snapshot

for job in orchard-s3-sync orchard-snapshot; do
sudo tee /etc/systemd/system/$job.service >/dev/null <<EOF
[Unit]
Description=$job
[Service]
Type=oneshot
User=orchard
ExecStart=/usr/local/bin/$job
EOF
done

sudo tee /etc/systemd/system/orchard-s3-sync.timer >/dev/null <<'EOF'
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF
sudo tee /etc/systemd/system/orchard-snapshot.timer >/dev/null <<'EOF'
[Timer]
OnCalendar=daily
Persistent=true
[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now orchard-s3-sync.timer orchard-snapshot.timer
sudo systemctl start orchard-s3-sync orchard-snapshot      # run once now
aws s3 ls "s3://$BUCKET/" --recursive | head
```

The daily snapshot gives you a plain SQLite file you can download and open anywhere, independent of Litestream.

---

## 9. Background processing: what users see

These operations run on Orchard's background workers, not in the browser:

| Operation | Started from | Watched in |
|---|---|---|
| Europe PMC search (new, refresh, resume) | Search planner / Search history | **Search history** (refreshes every 5 s) |
| EyeLink scan (one paper or a batch) | Paper page / EyeLink finder | **EyeLink finder**, paper page (refreshes every 5 s) |
| Repository sweep and documentation reading | Repository sweep | **Repository sweep** (refreshes every 6 s) |

- Job status lives in the shared database, so **every signed-in user sees the same jobs and progress**, whoever started them. Closing the browser or signing out does not stop a job.
- Two jobs run at a time. Anything further waits in the queue in `queued` state.
- Restarting or redeploying Orchard stops running jobs. At the next start they are marked `interrupted` and can be **Resumed** (searches) or re-run (scans). Nothing already retrieved is lost.
- Server-side progress also appears in the log, prefixed with the user who made each request:

  ```bash
  sudo journalctl -u orchard -f
  # 127.0.0.1 - - [06/Nov/2026 20:24:35] user=shawn "POST /api/search HTTP/1.1" 200 -
  ```

---

## 10. Provenance log: who did what

Every row in Orchard's append-only `audit` table now has an **`actor`** column holding the username:

| Event | `actor` recorded |
|---|---|
| Anything a user does in the browser (imports, query approvals, criteria, screening, metadata corrections, duplicate decisions, PDF uploads, resource checks, exports…) | that user |
| Events written by a background job (`search_started`, `provider_page_received`, `paper_created`, `search_completed`, `eyelink_scan_completed`, repository sweep findings…) | the user who **started** the job |
| Jobs marked `interrupted` when Orchard restarts | `system` |
| Events recorded before hosted mode was enabled | empty (shown as `local`) |

In addition:

- **Screening decisions** store the signed-in username as the reviewer. The reviewer field is read-only once you are signed in, and the server ignores any other submitted name.
- Database triggers reject edits to and deletions of audit rows, screening decisions and query versions. Corrections are always new events.

**Viewing it.**

- **In the app:** go to *Audit & export*. Each event shows its username, and the filter box matches usernames, event names and record IDs. For example, type `ashna` to see everything Ashna did.
- **In an export:** *Download export ZIP* includes `audit.csv` with `id, event, entity_id, detail, created, actor`.
- **On the server:**

  ```bash
  sudo -u orchard sqlite3 -readonly -header -column /var/lib/orchard/orchard.sqlite3 \
    "SELECT created, actor, event, entity_id FROM audit ORDER BY id DESC LIMIT 25;"

  # Actions per person
  sudo -u orchard sqlite3 -readonly /var/lib/orchard/orchard.sqlite3 \
    "SELECT coalesce(actor,'local'), event, count(*) FROM audit GROUP BY 1,2 ORDER BY 1,3 DESC;"
  ```

**What it does and doesn't prove.** The log records which signed-in account performed each action. It is only as trustworthy as account hygiene (one person per account) and server access: anyone with root on the instance can change the database directly. Litestream's 30-day history, S3 versioning and, if you enabled it, S3 Object Lock on `snapshots/` give you independent copies to compare against.

---

## 11. Updating Orchard

```bash
sudo systemctl start orchard-snapshot                  # fresh backup first
cd /opt/orchard && sudo git fetch && sudo git checkout <tag-or-commit>
sudo systemctl restart orchard
sudo journalctl -u orchard -n 20 --no-pager
```

To confirm which build is live, look at the bottom-left corner of the sidebar (for example `v0.2.0 · 04e321f`; hover for the full SHA), or run `curl -s -H 'X-Remote-User: check' -H "Host: $DOMAIN" http://127.0.0.1:8765/api/version` on the instance. The startup log line `Version … (commit …)` shows the same. The commit is read from `/opt/orchard/.git` when Orchard starts, so the page only changes after `systemctl restart orchard`. If you deploy without a `.git` folder, set `ORCHARD_COMMIT=<sha>` in `/etc/orchard/orchard.env`.

Schema changes are additive and applied at startup. For example, adding the `actor` column is schema version 3. Jobs running at restart become `interrupted` (see §9), so restart when nothing important is running.

---

## 12. Restoring from S3

On a fresh instance set up through §8. §6 started Orchard, which created an **empty** database. Remove that empty database before restoring. **Never run the `rm` on the server whose data you are trying to recover.**

```bash
sudo systemctl stop orchard litestream
sudo rm -f /var/lib/orchard/orchard.sqlite3 /var/lib/orchard/orchard.sqlite3-wal /var/lib/orchard/orchard.sqlite3-shm
sudo -u orchard litestream restore -config /etc/litestream.yml -o /var/lib/orchard/orchard.sqlite3 \
     /var/lib/orchard/orchard.sqlite3
#   …or a point in time:   add  -timestamp 2026-11-01T12:00:00Z
#   …or from a daily snapshot:
#   aws s3 cp s3://$BUCKET/snapshots/<file>.sqlite3.gz - | gunzip | sudo -u orchard tee /var/lib/orchard/orchard.sqlite3 >/dev/null
sudo -u orchard aws s3 sync "s3://$BUCKET/files/" /var/lib/orchard/
sudo systemctl start litestream orchard
```

Test a restore onto a scratch instance once after setup, so you know it works before you need it.

---

## 13. Limits to know about

- **No roles.** Every signed-in user can do everything, including exporting the whole workspace (documents, full text and the audit log) and recording screening decisions. Give accounts only to people who should have that access.
- **Basic-auth sessions** last until the browser is closed, and there is no sign-out button. Use SSO (§7) if you need session expiry or MFA.
- **Single instance.** No load balancing or high availability. If the instance fails, restore from S3 (§12), with up to about a second of database changes and up to 10 minutes of new PDFs at risk.
- **Outbound access.** Orchard calls Europe PMC, PMC's open-data bucket, Unpaywall and repository APIs over HTTPS. The default security-group egress (allow all) covers this. Orchard's own request guard already refuses private and link-local addresses, including the EC2 metadata service.
- **Rough cost** (on-demand, us-west-2, before free tier): `t3.small` about $15/month, 40 GB gp3 about $3/month, Elastic IP about $4/month, S3 a few cents per GB-month. Check current AWS pricing.

## Configuration reference

| Setting | Flag | Meaning |
|---|---|---|
| `ORCHARD_DATA_DIR` | `--data-dir` | Folder for `orchard.sqlite3` and `pdfs/` |
| `ORCHARD_PUBLIC_URL` | `--public-url` | Public `https://host` users browse to. Adds that Host/Origin to the allow-list. Requires `ORCHARD_USER_HEADER`. |
| `ORCHARD_USER_HEADER` | `--user-header` | Header carrying the proxy-authenticated username. When set, requests without it get HTTP 401, and the value is recorded as `actor` on every audit event. |
| — | `--port` | Loopback port (default 8765). Orchard never listens on a public interface. |
