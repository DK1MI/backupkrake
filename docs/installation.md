# Installation

Backupkrake ships as a Docker image for **amd64 and arm64** (e.g. a Raspberry Pi 4/5 or an ARM NAS) at
`ghcr.io/dk1mi/backupkrake`. You don't need to clone the repository to run it.

## Requirements

**On the backup server**

- Docker with the Compose plugin (`docker compose`), or Podman with `podman compose`.
- A disk for the snapshots, mounted on the Docker host (see [Planning the storage](#planning-the-storage)).
- Network access to the clients' SSH port.

**On each client**

- An SSH server.
- `rsync`, version 3.2.4 or newer if you want the read-only `rrsync` key (recommended). Older versions work
  with the [unrestricted key](hosts.md#restricted-or-unrestricted-key).

Nothing else: no agent, no Backupkrake software on the clients.

## Planning the storage

All snapshots live under the container's `/data` volume. Choose it carefully, because moving it later
means copying hard-linked trees (possible with `rsync -aH`, but slow).

- **Filesystem:** a local filesystem with hard links: ext4, XFS, btrfs or ZFS. NFS and SMB shares from
  another machine don't work reliably (hard links, ownership and numeric IDs), and neither do FAT or exFAT.
- **Its own disk.** Keep the backups off the system disk. A full `/data` should never stop the server
  itself, and a dying system disk shouldn't take the backups with it.
- **Size.** Roughly the data of all clients once, plus every file version that changes within your
  retention period. A server whose 100 GB rarely change needs little more than 100 GB for weeks of
  snapshots. A 30 GB VM image that changes daily costs 30 GB per kept snapshot; see
  [Limits of hard-link deduplication](snapshots.md#limits-of-hard-link-deduplication). The dashboard
  shows a fill-up forecast once Backupkrake runs.
- **Many small files** need inodes: every snapshot adds a directory entry per file, plus all
  directories. On ext4 with millions of files, check `df -i` now and then.

`/config` is small (database, keys, logs) and can stay next to the compose file.

## Install with Docker Compose

Create a directory:

```sh
mkdir -p /opt/backupkrake && cd /opt/backupkrake
```

Create `docker-compose.yml`:

```yaml
services:
  backupkrake:
    image: ghcr.io/dk1mi/backupkrake:0.6.0     # or :latest to follow main
    container_name: backupkrake
    restart: unless-stopped
    ports:
      - "8080:8080"                           # e.g. "192.168.1.5:8080:8080" to bind to one LAN address
    environment:
      TZ: Europe/Berlin                       # schedules and snapshot names use this timezone
      BACKUPKRAKE_ADMIN_USER: admin
      BACKUPKRAKE_ADMIN_PASSWORD: ${BACKUPKRAKE_ADMIN_PASSWORD:?set it in .env}
      BACKUPKRAKE_MAX_PARALLEL: "2"           # host backups running at the same time
    volumes:
      - /srv/backups:/data                    # snapshots: point this at your big disk
      - ./config:/config                      # database, SSH keys, logs, restic cache
```

Put the admin password in a `.env` file next to it, so it isn't in the compose file:

```sh
printf 'BACKUPKRAKE_ADMIN_PASSWORD=%s\n' "$(openssl rand -base64 18)" > .env
chmod 600 .env
cat .env      # note the password
```

Start it:

```sh
docker compose pull
docker compose up -d
docker compose logs -f      # Ctrl+C to stop following
```

Open `http://<server>:8080` and log in. On first start Backupkrake creates three SSH key pairs in
`config/ssh/` (backup, restore and offsite). The **Setup** page shows the lines to put on your clients.
Continue with [Adding hosts](hosts.md).

!!! note "Why the container runs as root"
    Backups keep each file's owner and group as numeric IDs (`--numeric-ids`). Only root can create files
    owned by other users, so the container runs as root on purpose. It doesn't need any extra privileges
    or capabilities beyond that.

!!! tip "Forgot to set a password?"
    Without `BACKUPKRAKE_ADMIN_PASSWORD`, Backupkrake generates one on first start, prints it to the log
    and stores it in `config/initial_admin_password`.

### Settings in the compose file

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for schedules, backup windows and snapshot names. Set it to your local zone. |
| `BACKUPKRAKE_ADMIN_USER` | `admin` | Login name for the web UI |
| `BACKUPKRAKE_ADMIN_PASSWORD` | generated | Password for the web UI |
| `BACKUPKRAKE_MAX_PARALLEL` | `2` | How many backups and restores run at the same time. Raise it for many small hosts and a fast disk; lower it to `1` on a Raspberry Pi with a USB disk. |

The [Reference](reference.md#environment-variables) lists all variables.

## Network access and HTTPS

The web UI is plain HTTP with a single admin login. That's fine on a trusted home or office LAN, but:

- **Never forward port 8080 to the internet.** Whoever gets in can read every backup and write to every
  client that has the restore key.
- For remote access, prefer a VPN (WireGuard, Tailscale). Or put a reverse proxy with HTTPS and an extra
  authentication layer (e.g. an SSO proxy or client certificates) in front of it.

Backupkrake must be served at the root of its own (sub)domain, e.g. `https://backup.example.lan/`, not
under a path like `/backupkrake/`. An example with Caddy:

```
backup.example.lan {
    reverse_proxy 127.0.0.1:8080
}
```

With nginx, also raise the read timeout so large `.tar.gz` downloads, which are streamed while they're
being packed, don't get cut off:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_read_timeout 1h;
    proxy_buffering off;
}
```

When a proxy runs on the same Docker host, bind the port to localhost only: `"127.0.0.1:8080:8080"`.

## Updating

```sh
cd /opt/backupkrake
# when pinned: change the version in docker-compose.yml first, e.g. 0.6.0 -> 0.7.0
docker compose pull
docker compose up -d
```

- Database changes are applied automatically on start.
- Runs that are active during the restart are recorded as *failed* with the summary *interrupted (server restart)*. A
  backup's partial data is kept, and the next run resumes from it. Update outside your backup hours if
  you can.
- To roll back, set the previous version and run `docker compose up -d` again. Downgrades across a
  version that changed the database aren't tested. Before a big update, keep a copy of `config/` (see
  [Backing up Backupkrake itself](operations.md#backing-up-backupkrake-itself)).

Read the [release notes](https://github.com/DK1MI/backupkrake/releases) before updating.

### Image tags

| Tag | Meaning |
|-----|---------|
| `0.6.0`, `0.6` | Released versions (from git tags such as `v0.6.0`). **Recommended.** |
| `latest` | The current `main` branch. Newest features, least tested. |
| `sha-<commit>` | One specific commit |

## Building the image yourself

```sh
git clone https://github.com/DK1MI/backupkrake.git && cd backupkrake
# edit docker-compose.yml (password, timezone, data path)
docker compose up -d --build
```

## Uninstalling

```sh
cd /opt/backupkrake
docker compose down
```

This leaves your data alone. Snapshots stay in the `/data` folder on the host and the configuration in
`config/`; delete them yourself when you no longer need them. Also remove the Backupkrake lines from
the clients' `authorized_keys`.
