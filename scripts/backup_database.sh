#!/bin/sh
# The backups of the database, for the backup service of compose.yaml, which
# runs this with the image of the database: its pg_dump is of its version.
#
#   sh /backup.sh                one now and then, every BACKUP_INTERVAL seconds (a day by default)
#   sh /backup.sh once           one now
#   sh /backup.sh restore FILE   the database as it was in FILE, one of /backups
#
# Each backup goes to /backups, named after its date and time in UTC, which
# keeps the latest BACKUP_KEEP (7 by default). They connect to the database
# with PGHOST, PGUSER, PGPASSWORD and PGDATABASE.
set -eu

KEEP=${BACKUP_KEEP:-7}
INTERVAL=${BACKUP_INTERVAL:-86400}

# The newest first, by the date in their names.
backups() {
    find /backups -name 'nanosaas-*.dump' | sort -r
}

backup() {
    # Under a name of its own until it's complete: a dump cut halfway never
    # looks like a backup, nor does one taken at once by another run.
    partial=$(mktemp /backups/.partial-XXXXXX)
    if ! pg_dump --format=custom --file="$partial"; then
        rm -f "$partial"
        echo "The backup failed" >&2
        return 1
    fi
    file=/backups/nanosaas-$(date -u +%Y%m%d-%H%M%S).dump
    mv "$partial" "$file"
    backups | tail -n +"$((KEEP + 1))" | xargs -r rm -f
    echo "Backed up the database to $file"
}

# A sleep that a signal ends: the shell runs its traps while it waits.
nap() {
    sleep "$1" &
    wait $!
}

restore() {
    # The whole backup as SQL first: if it can't be read, the database isn't touched.
    sql=$(mktemp)
    trap 'rm -f "$sql"' EXIT
    pg_restore --file="$sql" "/backups/$1"
    # Then, in a single transaction, the schema emptied and the backup restored
    # into it: the tables created since the backup go too. If anything fails,
    # the database stays as it was.
    PGOPTIONS="-c client_min_messages=warning" psql --single-transaction --set=ON_ERROR_STOP=1 \
        --quiet --output=/dev/null --command="DROP SCHEMA public CASCADE; CREATE SCHEMA public;" --file="$sql"
    echo "Restored the database from /backups/$1"
}

case "${1:-}" in
    once)
        backup
        ;;
    restore)
        restore "${2:?Which backup? One of /backups, like $(backups | head -n 1 | xargs -r basename)}"
        ;;
    "")
        # Stopped, it exits right away, instead of after its sleep, which can last a day.
        trap 'exit 0' INT TERM
        # The parts of backups cut by a stop.
        find /backups -name '.partial-*' -mmin +60 -exec rm -f {} +
        # The first one when the latest is BACKUP_INTERVAL old: restarting the
        # service, as each deploy does, doesn't take one more.
        latest=$(backups | head -n 1)
        if [ -n "$latest" ]; then
            age=$(($(date +%s) - $(stat -c %Y "$latest")))
            if [ "$age" -lt "$INTERVAL" ]; then
                nap "$((INTERVAL - age))"
            fi
        fi
        while true; do
            # A failed one is tried again in 5 minutes.
            if backup; then
                nap "$INTERVAL"
            else
                nap 300
            fi
        done
        ;;
    *)
        echo "Usage: sh /backup.sh [once | restore FILE]" >&2
        exit 2
        ;;
esac
