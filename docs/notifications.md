# Notifications

A backup system you don't watch is a backup system that fails silently. Backupkrake sends push messages
through [ntfy](https://ntfy.sh) and/or [Pushover](https://pushover.net). Both are set up on the
**Notifications** page.

## Setting up ntfy

1. Pick a **topic**. On the public `ntfy.sh` server anyone who knows the topic can read it, so use
   something hard to guess, e.g. `backupkrake-x7f3k2q9`.
2. Subscribe to the topic in the ntfy app on your phone.
3. On the Notifications page, enable ntfy, enter the **server** (`https://ntfy.sh` or your own instance)
   and the topic.
4. For a protected topic on your own server, open **Authentication** and enter an access token
   (`tk_…`), or a username and password. The token takes precedence.
5. **Save**, then **Send test**.

**Priority on failure** (default 4, *high*) is used for failures, overdue alerts, low free space and
**Send test**. Warnings, including the fill-up forecast, are sent with priority 3, and "OK again" messages
with 2.

## Setting up Pushover

1. Copy your **user key** from the Pushover dashboard (a group key works too).
2. Create an application at [pushover.net/apps/build](https://pushover.net/apps/build) and copy its
   **API token**.
3. Enable Pushover, enter both, **Save**, then **Send test**.

**Priority on failure** (default 0, *normal*; 1 = *high* bypasses quiet hours) is used for the same
events. Warnings and "OK again" messages are sent with -1 (no sound).

You can use ntfy and Pushover at the same time. Secrets you've saved are never shown again; type a new
value to replace one, or tick *remove saved value*.

## When you are notified

| Event | Notified | Setting |
|---|---|---|
| Host backup failed | always | |
| Backup looks incomplete ([plausibility check](verification.md#plausibility-check)) | always | |
| Content check failed | always | |
| Offsite backup or offsite restore test failed | by default | *Offsite backup failed* |
| Restore to a client failed | by default | *Restore failed* (dry runs only notify when they fail) |
| Run finished with warnings (unreadable or vanished files, post-command failed) | off by default | *Finished with warnings* |
| First good run after a failure, or after an overdue alert | by default | *Backup works again* |
| Host overdue | per [overdue setting](#overdue-backups) | |
| Backup storage low or filling up | per [storage setting](#backup-storage-running-low) | |

Not notified: cancelled runs, runs **skipped** outside a backup window or because a host was
unreachable with retries enabled, and runs **interrupted** at the end of a window. They're normal
operation. If they keep a host from ever finishing, the overdue alert catches it.

## Overdue backups

Independently of individual runs, an hourly check alerts when a host has had **no successful backup for
*N* days** (default 2, 0 = off). Hosts can override it in their settings, e.g. `1` for a critical
server or `14` for a laptop.

- It reminds you once a day while the problem lasts, and sends *Backups up to date again* when it's
  resolved.
- Overdue hosts are marked on the dashboard.
- It catches what never produces a failed run: a schedule that was switched off, a host that's always
  outside its window, a scheduler that stopped, a server that was down.

The first check runs about two minutes after Backupkrake starts.

## Backup storage running low

Every hour Backupkrake records how full `/data` is. From the growth over the last 30 days it estimates
when the disk will be full. The dashboard shows the estimate: *full in about 48 days (+1.2 GiB/day)*.

A notification is sent when

- free space drops below **10 %**, or
- the forecast says the disk is **full within 30 days**.

Both thresholds are configurable (0 = off). Reminders repeat once a day, and an *OK again* message follows
when it clears. The forecast starts after two days of data.

To get space back: lower the retention of the hosts that use the most (see their [disk usage
card](snapshots.md#the-disk-usage-card)), exclude large files that change often, or delete old
snapshots.

## Heartbeat

Backupkrake can't tell you that it is itself down. For that, set a **heartbeat URL**, for example:

- a [healthchecks.io](https://healthchecks.io) check with a period of 1 hour and a grace time of 1 hour, or
- an Uptime Kuma *push* monitor with an interval of 3600 seconds.

Backupkrake fetches the URL every hour. When the pings stop (server off, container crashed, scheduler
stuck), the external service alerts you.

## Message contents

Messages include the run summary and the last lines of the log. With **Backupkrake URL** set (e.g.
`http://nas.lan:8080`), tapping a notification opens the run.

Delivery errors are logged and shown on the Notifications page (*Last delivery*). A broken notification
service never affects backups.
