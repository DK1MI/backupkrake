# Adding hosts

A *host* is a machine Backupkrake backs up. There are two kinds:

| Kind | Logs in as | Backs up | Typical use |
|---|---|---|---|
| **Whole system** | `root` | absolute paths such as `/etc`, `/home`, `/var` | your own servers, NAS, desktops |
| **Home folder only** | a normal user | paths inside that user's home folder | shared Unix systems, university logins, hosted shells |

## The SSH keys

On first start Backupkrake creates three key pairs in `config/ssh/`:

| Key | File | Used for | Authorized on the client as |
|---|---|---|---|
| Backup key | `id_ed25519` | pulling backups, content checks | `command="rrsync -ro /"` (read only) |
| Restore key | `id_ed25519_restore` | restores from the web UI (optional) | `command="rrsync -wo /"` (write only) |
| Offsite key | `id_ed25519_offsite` | `sftp:` offsite repositories | not on clients; see [Offsite copies](offsite.md#sftp-repositories) |

The **Setup** page shows the public keys and the ready-made `authorized_keys` lines.

`rrsync` is a small script that ships with rsync 3.2.4 and newer. Used as a forced `command=` in
`authorized_keys`, it allows nothing but rsync, in one direction (`-ro` read only, `-wo` write only), below one
directory. `restrict` additionally turns off port forwarding, agent forwarding and terminals. Even if
someone steals the backup key from the server, it can only *read* files from the client; it can't run
commands or change anything.

Separate keys mean the backup key never needs write access. Clients that you only back up, and never
restore to through the UI, don't need the restore key at all.

### Restricted or unrestricted key

| | Restricted (`rrsync -ro`) | Unrestricted (plain key) |
|---|---|---|
| Backups | ✓ | ✓ |
| Pre/post commands (database dumps, stopping containers) | ✗ | ✓ |
| What a stolen key can do | read files | everything root can |

Use the restricted line unless you need pre/post commands. You can tighten either variant further by
allowing it only from the backup server's address. Put `from="192.168.1.5",` at the start of the line:

```
from="192.168.1.5",command="rrsync -ro /",restrict ssh-ed25519 AAAA… backupkrake
```

## Adding a whole-system host

### 1. Prepare the client

Install rsync:

```sh
apt install rsync             # Debian / Ubuntu
dnf install rsync             # Fedora / RHEL
pacman -S rsync               # Arch
apk add rsync rrsync python3  # Alpine
```

Authorize the backup key for root. Copy the line from the **Setup** page; it looks like this:

```sh
mkdir -p /root/.ssh && chmod 700 /root/.ssh
echo 'command="rrsync -ro /",restrict ssh-ed25519 AAAA… backupkrake' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

Allow key logins for root in `/etc/ssh/sshd_config`, then reload sshd (`systemctl reload ssh` or `sshd`):

```
PermitRootLogin prohibit-password
```

`prohibit-password` allows keys only, never passwords.

*Optional:* to restore from the web UI, also add the restore key line from the Setup page:

```
command="rrsync -wo /",restrict ssh-ed25519 AAAA… backupkrake-restore
```

### 2. Add the host in the web UI

**Hosts → Add host**, choose **Whole system**, and fill in:

- **Name**: used as the snapshot folder name (`/data/hosts/<name>/`). Letters, digits, `.`, `_` and
  `-`; it can't be changed later.
- **Address**: hostname or IP address (IPv6 works too).
- **SSH user**: `root`.
- **SSH port**: `22`, or whatever port sshd listens on. Nothing else is needed for a non-standard port.

The other sections are covered in [Configuring backups](backups.md). The defaults are a good start: paths
`/etc /home /root /var /opt /srv /usr/local`, caches and container storage excluded, a backup every night
at 02:00, and retention of 3 last, 7 daily, 4 weekly and 12 monthly snapshots.

### 3. Test the connection

On the host's page, click **Test connection**. It:

1. connects with the backup key and lists `/` with rsync, using the same ACL/xattr options as a real
   backup, and
2. checks with a harmless dry run whether the restore key is authorized too.

On first contact Backupkrake stores the client's SSH host key in `config/ssh/known_hosts` (*trust on
first use*). If the key changes later, connections are refused, because that may mean someone is
impersonating the client. After a legitimate reinstall, use **Danger zone → Forget SSH host key** on the
host's page.

The test page explains the common errors. More are in [Troubleshooting](troubleshooting.md).

### 4. Run the first backup

Click **Back up now**. The run page shows a live log and a progress bar. The first backup copies
everything and can take hours for large hosts. Later ones only transfer what changed.

## Adding a home-folder host

Use this for a machine where you only have a normal account. No root access is needed there.

1. **Add host** and choose **Home folder only**. Enter your username. Paths are relative to your home
   folder (`.` = all of it, `Documents` = just that folder). Excludes are anchored at the home folder,
   e.g. `/.cache/`.
2. On that system, add this line to **your** `~/.ssh/authorized_keys` (the form and the Setup page show
   the exact line):

    ```
    command="rrsync -ro .",restrict ssh-ed25519 AAAA… backupkrake
    ```

    `.` means your home folder, because ssh starts forced commands there. So the key can only read inside
    it. For restores, add the restore key with `rrsync -wo .` the same way.

3. **Test connection**, then back up.

Notes:

- **No `rrsync` there?** It's a Python script. Copy it to e.g. `~/bin/rrsync` (it needs `python3`) and
  use its full path in `command="…"`.
- **No Python either:** use the plain key line. Relative paths still resolve in your home folder, but the
  key then has the same rights as your account.
- **Permissions:** sshd ignores `authorized_keys` if `~/.ssh` or the file is writable by others:
  `chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys`.
- **Rootless containers:** Podman/Docker storage (`~/.local/share/containers/`, `~/.local/share/docker/`)
  is excluded by default. Its files belong to container sub-UIDs your account can't read, and images can
  be pulled again. Back up important volumes explicitly, e.g. with a pre-command `podman volume export`
  (needs the plain key).
- **ACLs and extended attributes** are off by default for these hosts, because many shared and non-Linux
  systems don't support them over rsync.
- **Restores** go to `~/.backupkrake-restore/…` by default, inside your home folder where other users
  can't see them.
- **Be a good neighbour:** respect the system's usage policy. A [backup window and bandwidth
  limit](backups.md#backup-windows) keep the load low.

## BSD, macOS and other Unix systems

Backupkrake only needs rsync and sshd on the client, so FreeBSD, NetBSD, OpenBSD and macOS work too.

- Install a current rsync from the system's package collection (`pkg install rsync`,
  `pkgin install rsync`, Homebrew). macOS ships an old `openrsync` that doesn't understand all options.
- If the package doesn't include `rrsync`, copy the script from rsync's source tree (`support/rrsync`),
  make it executable, and use its full path in the key line.
- **Untick "Preserve ACLs and extended attributes"**. rsync builds without ACL support refuse the whole
  run. The connection test catches this, because it uses the same options.
- Root logins must be allowed for key authentication, as on Linux.

## Editing and deleting hosts

**Edit** on a host's page changes all settings except the name. Changing the host kind (whole system ↔
home folder) changes the snapshot layout, so the next backup is a full copy.

**Danger zone → Delete host** removes the host and its schedule. Its snapshots stay in
`/data/hosts/<name>/` unless you also tick *also delete all snapshots*. A running backup must be
cancelled first.

To rename a host, add a new one with the new name. You can move the old snapshot folder to the new
name on the server's disk before the first backup, so that backup isn't a full copy:

```sh
docker compose stop
mv /srv/backups/hosts/oldname /srv/backups/hosts/newname
docker compose start
```

Pins and notes belong to the old host entry and are removed with it, so set them again on the new host.
