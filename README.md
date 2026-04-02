# LAN development database manager

Django app for managing **MySQL** and **MariaDB** dev instances on a single machine via **Docker**. Each [Managed database](dbinstances/models.py) row maps to a container with a persisted Docker volume and a **host port** published on `0.0.0.0` so teammates on the LAN can connect.

## Requirements

- Python 3.12+ (tested with 3.14)
- Docker Engine on the **same host** as the app (socket access)
- A virtualenv (recommended)

## Setup

```bash
cd /path/to/dbmanager
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, DJANGO_DEBUG
python manage.py migrate
python manage.py createsuperuser
```

## Environment variables

| Variable | Meaning |
|----------|---------|
| `DJANGO_SECRET_KEY` | Required in production; long random string |
| `DJANGO_DEBUG` | `true` or `false` (use `false` on the LAN server) |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts/IPs, e.g. `localhost,127.0.0.1,192.168.1.10,dbbox.local` |
| `DOCKER_HOST` | Optional; defaults to the Docker SDK env (usually `unix:///var/run/docker.sock`) |

## Hosting on the LAN

1. Set `DJANGO_ALLOWED_HOSTS` to your server’s **hostname** and **LAN IP** (and `localhost` if you want local admin).
2. Set `DJANGO_DEBUG=false` and a strong `DJANGO_SECRET_KEY`.
3. Bind the HTTP server to all interfaces, e.g. Gunicorn:

   ```bash
   gunicorn config.wsgi:application --bind 0.0.0.0:8000
   ```

4. Open the **admin port** (e.g. 8000) and each **MySQL `host_port`** you assign to instances in the firewall.
5. Only **staff/superusers** should reach `/admin`; use your network firewall or VPN if needed.

### Django in Docker

If the web app runs in a container, mount the socket and ensure the process user can use it:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

On Linux, the container user often needs to match the `docker` group GID or run as root (document for your environment).

## Using the admin

1. Log in at `http://<server-ip>:8000/admin/`.
2. Add a **Managed database**: name, **engine** (MySQL or MariaDB), **image tag** (e.g. `8.0`, `11`), **host port** (unique), root password, optional initial database name.
3. Select the row and run **Create container and start** (pulls the image if needed). Data lives in Docker volume `dbmanager_data_<id>`.
4. After a container exists, **engine** and **image tag** are read-only unless you remove the container; change **host port** or image by stopping, using **Recreate container (keep volume)**, or remove and re-provision.
5. **Sync status from Docker** refreshes running/stopped state.

Admin actions:

- **Create container and start** — pull, create volume, run with `restart` policy `unless-stopped`, publish `0.0.0.0:<host_port>->3306/tcp`.
- **Start** / **Stop** — lifecycle on the existing container id.
- **Recreate container** — remove container only, then create again with current settings (same volume).
- **Remove container** — delete container, **keep** volume.
- **Remove container and delete volume** — destructive; deletes the named volume.

## Example client connection

Replace host, port, and password with values from the admin row:

```bash
mysql -h 192.168.1.10 -P 13306 -u root -p
```

Connection URL style: `mysql://root:<password>@192.168.1.10:13306/`

## Security notes

- Root passwords are stored in the Django database in **plain text** suitable only for **trusted dev networks**. Do not expose this app to the public internet without hardening (HTTPS, stricter auth, field encryption, etc.).
- Treat Docker socket access as **root-equivalent** on the host.

## Development server (quick try)

```bash
python manage.py runserver 0.0.0.0:8000
```

Not for production; use Gunicorn (or similar) behind a reverse proxy if needed.
