# LAN development database manager

Django app for managing **MySQL** and **MariaDB** dev servers on a single machine via **Docker**.

- **[`DatabaseEngine`](dbinstances/models.py)** — one Docker container, published **host port**, data volume, and server-level state.
- **[`LogicalDatabase`](dbinstances/models.py)** — a MySQL **schema** (`schema_name`) belonging to one engine; used for `CREATE DATABASE` and for user grants.
- **[`ManagedDatabaseUser`](dbinstances/models.py)** — **Root** (one per engine; drives `MYSQL_ROOT_PASSWORD` and provisioning) or **Application** users with optional **`granted_databases`** (M2M to logical DBs). Empty grants means **`GRANT ALL ON *.*`** (dev only).

The published port is on `0.0.0.0` so teammates on the LAN can connect.

## Requirements

- Python 3.12+ (tested with 3.14)
- Docker Engine on the **same host** as the app (socket access)
- **PyMySQL** (in `requirements.txt`) for `127.0.0.1:<host_port>` provisioning
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

4. Open the **admin port** (e.g. 8000) and each **MySQL `host_port`** in the firewall.
5. Only **staff/superusers** should reach `/admin`; use your network firewall or VPN if needed.

### Django in Docker

If the web app runs in a container, mount the socket and ensure the process user can use it:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

Provisioning connects from the **Django host** to **`127.0.0.1:<host_port>`**, so that must reach the published container port.

## Using the admin

1. Log in at `http://<server-ip>:8000/admin/`.
2. Add a **Database engine**: name, **vendor** (MySQL or MariaDB), **image tag**, unique **host port**.
3. Inlines:
   - **Logical databases**: `schema_name` (and optional `label`) for each schema this server should have. Provisioning runs `CREATE DATABASE IF NOT EXISTS` for each.
   - **Managed database users**:
     - **Root**: Omit and save; on first **Create container and start**, a **root** row with a generated password is created. Or define Root + password first.
     - **Application**: `username`, `password`, **host** (often `%`). Choose **Granted databases** to limit grants to `schema.*` for each selected logical DB; leave empty for `*.*` (dev only).
4. **Create container and start**: pulls if needed, starts container. If the engine has **exactly one** logical database, **`MYSQL_DATABASE`** is set for first-time init; otherwise schemas are created only via SQL after startup.
5. **Sync application users into the database** after changing users or grants while the container is running.

After a container exists, **vendor** and **image tag** are read-only unless you remove the container. Change **host port** or image via **Recreate container (keep volume)** or remove and re-provision.

**Existing volume note:** `MYSQL_ROOT_PASSWORD` only affects **first** data-dir init. Changing the Root row in Django does not change an existing server’s root password.

## Admin actions

- **Create container and start** — volume, container, optional `MYSQL_DATABASE`, wait for MySQL, create schemas, sync application users.
- **Start** / **Stop** — container lifecycle.
- **Sync application users into the database** — SQL user/grant sync.
- **Recreate container** — same volume, new container options.
- **Remove container** / **Remove container and delete volume** — as before.

## Example client connection

```bash
mysql -h 192.168.1.10 -P 13306 -u myapp -p myschema
```

## Security notes

- Passwords in Django are **plain text** — **trusted dev networks** only.
- Docker socket access is **root-equivalent** on the host.

## Development server (quick try)

```bash
python manage.py runserver 0.0.0.0:8000
```

Not for production; use Gunicorn (or similar) behind a reverse proxy if needed.
