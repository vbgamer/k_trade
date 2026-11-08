# Run on a cloud server, control from your phone

The laptop does not need to be on. A small Linux server in India runs the platform all day;
you open `https://<your-name>.duckdns.org` on your phone, log in, and start/stop strategies.
Strategies keep running when you close the phone, and resume automatically if the server restarts.

What runs on the server (`docker-compose.cloud.yml`):

| Container | Job |
|---|---|
| `caddy` | HTTPS (free Let's Encrypt certificate, auto-renewed) on ports 80/443 |
| `api` | FastAPI + dashboard + market feed + strategy loops (in-process) |
| `db` | Postgres, not reachable from the internet |

## 1. Create the server

Any Ubuntu 22.04/24.04 VM with **1 vCPU / 1 GB RAM or more** in **Mumbai or Hyderabad** works:

- **Oracle Cloud Always Free** (₹0): Compute → Create instance → Ubuntu, shape `VM.Standard.E2.1.Micro`
  or `VM.Standard.A1.Flex` (ARM, if capacity is available). Needs a card for verification.
- **AWS Lightsail Mumbai** or **DigitalOcean Bangalore**: about $5–7/month, simplest setup.

Allow inbound **TCP 22, 80, 443** in the provider's firewall (Oracle: VCN → Security List; Lightsail: Networking tab).
Note the server's **public IP**.

> Oracle's Ubuntu images also block ports in iptables. On Oracle only, run after logging in:
> ```bash
> sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
> sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
> sudo netfilter-persistent save
> ```

## 2. Get a free domain name

HTTPS (needed for a secure login from your phone) requires a hostname, not a bare IP.

1. Go to https://www.duckdns.org, sign in, create a subdomain, e.g. `mytrader`.
2. Put the server's public IP in its "current ip" field and click *update ip*.

## 3. Install Docker on the server

```bash
ssh ubuntu@<server-ip>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && exit   # log out and ssh in again

# 1 GB machines: add swap so the image build doesn't run out of memory
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Get the code and configure it

Push your latest code to GitHub from the laptop first, then on the server:

```bash
git clone https://github.com/vbgamer/k_trade.git && cd k_trade
# private repo: use a GitHub personal access token as the password

cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
DOMAIN=mytrader.duckdns.org
EOF

cat > backend/.env <<EOF
SECRET_KEY=$(openssl rand -hex 32)
ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
DEMO_MODE=false
ALLOW_REGISTRATION=true
EOF
```

Keep a copy of `backend/.env` somewhere safe: losing `ENCRYPTION_KEY` makes saved broker credentials unreadable.

## 5. Start it

```bash
docker compose -f docker-compose.cloud.yml up -d --build
docker compose -f docker-compose.cloud.yml logs -f api   # Ctrl+C to stop watching
```

The first build takes a few minutes. Then open `https://mytrader.duckdns.org` on your phone.

## 6. Create your account, then lock sign-ups

1. Register on the site from your phone.
2. On the server, stop strangers from registering:
   ```bash
   sed -i 's/ALLOW_REGISTRATION=true/ALLOW_REGISTRATION=false/' backend/.env
   docker compose -f docker-compose.cloud.yml up -d
   ```
3. In the phone browser menu choose **Add to Home Screen**, which gives it an app icon.

## Daily use

Phone → open the app → log in → start the strategy. Check P&L any time from the dashboard.
Outside market hours a strategy can only be started if its subscription has `"bypass_scheduler": true`.

## Maintenance

```bash
cd k_trade
git pull && docker compose -f docker-compose.cloud.yml up -d --build      # deploy updates
docker compose -f docker-compose.cloud.yml logs --tail 200 api             # recent logs
docker compose -f docker-compose.cloud.yml exec db pg_dump -U postgres options_trading > backup.sql
```

## Notes

- Market data is currently the built-in simulator (5-second candles), so this is paper trading on simulated
  prices, not the live NSE feed.
- Before moving to live broker orders: SEBI's retail algo framework expects API orders to come from a static IP
  registered with your broker. A cloud VM with a reserved/static IP fits that; check your broker's current rules.
