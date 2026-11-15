"""Ready-made pre/post-commands for consistent database and container backups.

Copying the files of a running database can give a backup that won't restore; a
dump taken by the database itself is always consistent. Each preset writes its dump
into a dump folder on the client, which is added to the host's paths.

Every command is a self-contained ``( set -e; ... )`` block so that presets can be
combined with ``&&`` and a failing dump fails the pre-command, which aborts the
backup (instead of backing up a stale or half-written dump). Placeholders that the
user must edit are written in CAPITALS.
"""

DUMP_DIR = {"system": "/var/backups/backupkrake", "home": "$HOME/.backupkrake-dumps"}
DUMP_PATH = {"system": "/var/backups/backupkrake", "home": ".backupkrake-dumps"}

_PRESETS = [
    {
        "id": "postgresql", "label": "PostgreSQL (all databases)", "scopes": ["system"],
        "pre": "( set -e; umask 077; mkdir -p {dir}; su -s /bin/sh postgres -c pg_dumpall > {dir}/postgresql.sql; "
               "gzip -f {dir}/postgresql.sql )",
        "note": "Dumps every database as the postgres user into {dir}/postgresql.sql.gz.",
    },
    {
        "id": "mysql", "label": "MySQL / MariaDB (all databases)", "scopes": ["system"],
        "pre": "( set -e; umask 077; mkdir -p {dir}; $(command -v mariadb-dump || command -v mysqldump) "
               "--all-databases --single-transaction --routines --events > {dir}/mysql.sql; gzip -f {dir}/mysql.sql )",
        "note": "Uses root's socket login (the default on Debian/Ubuntu). If root needs a password, put it into "
                "/root/.my.cnf on the client instead of the command.",
    },
    {
        "id": "sqlite", "label": "SQLite database file", "scopes": ["system", "home"],
        "pre": "( set -e; umask 077; mkdir -p {dir}; sqlite3 {sqlite_db} \".backup '{dir}/app.db'\" )",
        "note": "Replace {sqlite_db} with the database file and app.db with a name of your choice. "
                "Needs the sqlite3 command on the client.",
        "vars": {"sqlite_db": {"system": "/PATH/TO/DATABASE.db", "home": "$HOME/PATH/TO/DATABASE.db"}},
    },
    {
        "id": "docker-postgres", "label": "Docker: PostgreSQL container", "scopes": ["system"],
        "pre": "( set -e; umask 077; mkdir -p {dir}; docker exec CONTAINER pg_dumpall -U postgres "
               "> {dir}/CONTAINER-postgresql.sql; gzip -f {dir}/CONTAINER-postgresql.sql )",
        "note": "Replace CONTAINER (twice) with the container name, and postgres with the database superuser "
                "if your image uses another one (POSTGRES_USER).",
    },
    {
        "id": "docker-mariadb", "label": "Docker: MySQL / MariaDB container", "scopes": ["system"],
        "pre": "( set -e; umask 077; mkdir -p {dir}; docker exec CONTAINER sh -c 'exec $(command -v mariadb-dump "
               "|| command -v mysqldump) --all-databases --single-transaction -uroot "
               "-p\"${{MARIADB_ROOT_PASSWORD:-$MYSQL_ROOT_PASSWORD}}\"' > {dir}/CONTAINER-mysql.sql; "
               "gzip -f {dir}/CONTAINER-mysql.sql )",
        "note": "Replace CONTAINER (three times) with the container name. Reads the root password from the "
                "container's MARIADB_ROOT_PASSWORD or MYSQL_ROOT_PASSWORD variable.",
    },
    {
        "id": "docker-volumes", "label": "Docker: all named volumes as .tar.gz", "scopes": ["system"],
        "pre": "( set -e; umask 077; mkdir -p {dir}/volumes; for v in $(docker volume ls -q); do "
               "docker run --rm -v \"$v\":/v:ro -v {dir}/volumes:/out alpine tar czf \"/out/$v.tar.gz\" -C /v .; done )",
        "note": "Exports every volume (Docker's own storage is excluded by default). Pulls the small alpine "
                "image once. For databases inside containers use a database preset as well: a volume copied "
                "while the database writes to it may not restore.",
    },
    {
        "id": "compose-stop", "label": "Docker Compose: stop the project during the backup", "scopes": ["system"],
        "pre": "( set -e; cd /PATH/TO/PROJECT; docker compose stop )",
        "post": "( cd /PATH/TO/PROJECT; docker compose start )",
        "path": "/PATH/TO/PROJECT",
        "note": "Replace /PATH/TO/PROJECT (three times) with the folder holding compose.yaml. The containers are "
                "stopped while the backup runs, so their files are consistent. The post-command always runs, "
                "even if the backup fails or is cancelled, so the project is started again.",
    },
]


def presets(scope: str) -> list[dict]:
    """Presets for the host form with the dump folder for this scope filled in."""
    scope = "home" if scope == "home" else "system"
    d = DUMP_DIR[scope]
    out = []
    for p in _PRESETS:
        vals = {"dir": d, **{k: v[scope] for k, v in p.get("vars", {}).items()}}
        out.append({
            "id": p["id"], "label": p["label"], "available": scope in p["scopes"],
            "pre": p["pre"].format(**vals),
            "post": p.get("post", "").format(**vals),
            "path": p.get("path", DUMP_PATH[scope]),
            "note": p["note"].format(**vals),
        })
    return out
