# Troubleshooting

Start with the run's page: the summary names the problem, and the log shows rsync's or restic's own
messages. For well-known rsync errors the summary already includes a hint.

## Testing the connection by hand

To see exactly what the server sees, run ssh from inside the container with Backupkrake's key and
options:

```sh
docker exec -it backupkrake ssh -i /config/ssh/id_ed25519 -p 22 \
    -o UserKnownHostsFile=/config/ssh/known_hosts root@web1
```

- With the restricted key, a working setup answers with an rrsync error such as *rrsync error: Not invoked
  via sshd* (or, if you add a command like `true`, *SSH_ORIGINAL_COMMAND does not run rsync*) and
  disconnects. That means the key was accepted and rrsync was found.
- `Permission denied (publickey)` means the key isn't accepted (see below).
- Add `-v` to see which step fails.

## Connection problems

### `Permission denied (publickey)`

The client doesn't accept the backup key.

- Is the line from the **Setup** page in the right file? `/root/.ssh/authorized_keys` for whole-system
  hosts, `~/.ssh/authorized_keys` of the configured user for home-folder hosts. It must be one line.
- `PermitRootLogin` must be `prohibit-password` (or `yes`) for root logins. Reload sshd after changing it.
- sshd ignores `authorized_keys` if `~/.ssh` or the file is writable by others:
  `chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys`.
- Is the SSH user right? Is `AllowUsers`/`AllowGroups` in `sshd_config` excluding it?
- The client's auth log (`journalctl -u ssh`, `/var/log/auth.log`) says why it refused.

### `Host key verification failed` or `REMOTE HOST IDENTIFICATION HAS CHANGED`

The client's SSH host key differs from the one Backupkrake stored on first contact. After a legitimate
reinstall, use **Danger zone → Forget SSH host key** on the host's page, then **Test connection** again.
If you didn't reinstall the client, find out why the key changed before you trust it again.

For an **SFTP offsite target**, the host key is stored the same way. To forget it, remove the server's
line from `config/ssh/known_hosts`:

```sh
docker exec backupkrake ssh-keygen -R '[uXXXXX.your-storagebox.de]:23' -f /config/ssh/known_hosts
```

### `Connection timed out`, `No route to host`, `Connection refused`

The server can't reach the client's SSH port: the machine is off, the address or port is wrong, or a
firewall is in the way. Check from the Docker host with `nc -vz web1 22`. For machines that are
regularly off, enable [retries](backups.md#laptops-and-desktops-that-arent-always-on).

### `rrsync: command not found` or `rrsync: not found`

The client has no `rrsync` on its `PATH`. Install rsync 3.2.4 or newer (on Alpine: `apk add rrsync`), or
use the full path in the key line, e.g. `command="/usr/local/bin/rrsync -ro /"`. On older Debian/Ubuntu
releases the script is at `/usr/share/doc/rsync/scripts/rrsync` (maybe gzipped: unpack it and make it
executable).

### `rsync: command not found` / `rsync: connection unexpectedly closed`

rsync isn't installed on the client, or isn't on the `PATH` of non-interactive SSH sessions. Install it
from the system's packages.

### `ACLs are not supported` / `extended attributes are not supported`

The client's rsync was built without ACL or xattr support (common on BSDs and macOS). Untick **Preserve
ACLs and extended attributes** in the host's settings.

### `do not use .. in arg`

A configured path contains `..`, which rrsync refuses. Write the path without it.

## Backup problems

### Paths are reported as *missing on client*

The paths don't exist on that client, so they were skipped. That's informational. Remove them from the
host's paths if they'll never exist there.

### `rsync exit code 23` / `24` (warning)

- **23:** some files couldn't be read, e.g. permission denied, or files on FUSE mounts that root can't
  read. The log lists them. Fix the permissions or exclude the files.
- **24:** files vanished while rsync was copying them: temporary files, caches. Harmless. Exclude the
  directory if it's noisy.

The snapshot is complete apart from those files.

### `rsync exit code 12` or `255` (failed)

The SSH connection broke: the network dropped, the client rebooted, or sshd closed the connection. The
`.partial` data is kept, and the next run resumes. If it happens repeatedly at the same point, look at the
client's logs. A very slow line can need a [bandwidth limit](backups.md#bandwidth-limit).

### `No space left on device`

The backup disk is full. The run fails, and its partial data stays for the next run. Free space by lowering
retention, deleting old snapshots or `.partial` folders of hosts you removed, then run the backup again.
The [storage alert](notifications.md#backup-storage-running-low) warns before this happens.

### The pre-command failed

The run log shows the command's output and exit code. Common causes:

- The key is the restricted `rrsync` one. Pre/post commands need the unrestricted key.
- A placeholder from a preset (`CONTAINER`, `/PATH/TO/…`) wasn't replaced.
- The dump tool isn't installed, or the database user can't log in. Run the command by hand on the client,
  as the SSH user, to see the error.

### A scheduled backup didn't run

- Is **Scheduled backups enabled** ticked, and the schedule not empty? The host's page shows the next run.
- Is `TZ` set? Without it, schedules run in UTC.
- Was it **skipped**? Look for a run with that status: outside the window, or the host was unreachable.
- Was the previous backup of the host still running? Then the new one is dropped.
- Was the server down at the time? Missed runs aren't caught up; the next scheduled one runs.

### The backup is much larger than usual

Look at the [disk usage card](snapshots.md#the-disk-usage-card) and the run's *Files changed* figure. Typical
causes: a renamed directory, a `chown -R` or `chmod -R`, a VM image or database file, a change of the
host kind, or a moved snapshot folder. To find the files, compare the two snapshots on the server:

```sh
cd /srv/backups/hosts/web1
find 2026-11-14_030000 -type f -links 1 -size +100M    # big files that aren't shared with another snapshot
```

## Restore problems

### *is the restore key authorized on the client?*

The restore key isn't in the target host's `authorized_keys`, or it is there with the wrong command. The
line must contain `rrsync -wo /` (or `-wo .` for home-folder hosts). **Test connection** shows whether
the restore key works.

### Restored files have the wrong owner

Owners are restored by number. On a different host than the original, the same uid can belong to another
user, or to none. Fix them with `chown`, or restore to the original host.

## Offsite problems

### `repository does not exist`

- The repository hasn't been initialised: click **Initialise repository** once.
- For a USB disk: the disk isn't mounted, or isn't passed into the container at that path.
- For SFTP: check that the path is right. `sftp://user@host:23//backupkrake` is the absolute path
  `/backupkrake`, while `sftp://user@host:23/backupkrake` is relative to the login directory.

### `wrong password or no key found`

The password in the target's settings doesn't match the repository. Repositories can't be "re-keyed" from
Backupkrake, so enter the original password.

### `repository is already locked`

Another restic process holds a lock, or a run was killed (power loss, container restart) and left a
stale lock. Make sure nothing else uses the repository, then click **Remove stale locks**.

### SFTP: `Permission denied` or `Host key verification failed`

The offsite key (shown on the target's page) isn't authorized on the SFTP server, or its host key changed.
See [SFTP repositories](offsite.md#sftp-repositories) and [above](#host-key-verification-failed-or-remote-host-identification-has-changed).

## Verification problems

### Content check reports differing files

The file's content differs between the client and the snapshot, although size and modification time are
the same. Compare them:

```sh
# on the client
sha256sum /etc/important.conf
# on the Docker host
sha256sum /srv/backups/hosts/web1/latest/etc/important.conf
sha256sum /srv/backups/hosts/web1/2026-10-01_020000/etc/important.conf   # an older snapshot
```

If the snapshot differs from both the client and older snapshots, the backup disk is the suspect. Check
`smartctl -a`, `dmesg`, and run a scrub on ZFS or btrfs. If the client differs from all snapshots, the
client's disk or an application changed the file in place while keeping its timestamp. Some applications
do that on purpose, e.g. some databases and VM images. Exclude those or back them up as dumps.

## The web interface

### I forgot the admin password

Set a new `BACKUPKRAKE_ADMIN_PASSWORD` in `.env` and run `docker compose up -d`. If you never set one,
the generated password is in `config/initial_admin_password`.

### I'm logged out all the time

The session cookie lasts 14 days. If you're logged out more often, your browser may be blocking or
clearing cookies for the site. `config/secret_key` signs the sessions; if it changes, everyone is logged
out once.

### Times are shown in the wrong timezone

Set `TZ` in `docker-compose.yml` and recreate the container. Snapshot names created before the change
keep their old times.
