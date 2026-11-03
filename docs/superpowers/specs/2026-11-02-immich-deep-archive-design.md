# Immich Deep Archive backup

A container on the same machine as Immich copies original photos and videos to S3 Glacier Deep Archive once a day. It also keeps two small objects in S3 Standard: a manifest of what is in the archive, and the newest Immich database dump. The goal is a cheap copy that still exists after the Immich disk is lost. A restore of the photos can take a day or two.

## Decisions

- The tool is a Docker sidecar. It reads the library from disk. It does not call the Immich API and it is not an Immich WebAssembly plugin.
- Photo bytes go to Glacier Deep Archive. There is no Iceberg table.
- Object keys mirror the Immich relative path, so a restore copies the tree back into place.
- The bucket listing is enough to know a file was stored and where. The manifest and the database dump are still kept because both are cheap and a restore needs the dump.
- The schedule is daily at 03:00 in the container timezone. The process also runs once when the container starts.
- Files that disappear from Immich stay in the archive. The tool never deletes S3 objects.

## What is copied

From `UPLOAD_LOCATION`, mounted read-only at `/data`:

| Local directory | S3 key prefix | Storage class |
| --- | --- | --- |
| `/data/library` | `<prefix>/originals/library/` | `DEEP_ARCHIVE` |
| `/data/upload` | `<prefix>/originals/upload/` | `DEEP_ARCHIVE` |
| `/data/profile` | `<prefix>/originals/profile/` | `DEEP_ARCHIVE` |
| newest `*.sql.gz` in `/data/backups` | `<prefix>/db/immich-latest.sql.gz` | `STANDARD` |
| generated each run | `<prefix>/manifest.csv` | `STANDARD` |

`<prefix>` defaults to `immich`.

These directories are never walked: `thumbs`, `encoded-video`, and anything else under `/data`. External libraries live outside `UPLOAD_LOCATION` and are out of scope.

If Immich is configured with separate host paths for backups or profile, those paths are mounted over `/data/backups` and `/data/profile`. The defaults match a stock Compose layout where everything is under `UPLOAD_LOCATION`.

## Layout on S3

```text
s3://<bucket>/<prefix>/originals/library/<path inside library>
s3://<bucket>/<prefix>/originals/upload/<path inside upload>
s3://<bucket>/<prefix>/originals/profile/<path inside profile>
s3://<bucket>/<prefix>/db/immich-latest.sql.gz
s3://<bucket>/<prefix>/manifest.csv
```

The relative path uses forward slashes and has no leading slash. The three roots are part of the relative path, so `library/user/2024/IMG.jpg` and `upload/user/2024/IMG.jpg` are different objects.

A changed file at the same path is uploaded again and replaces that object. Replacing a Deep Archive object early still incurs the 180-day minimum charge on the previous object. Immich paths usually include the asset id, so a normal add does not replace anything. A storage-template move writes a new key and leaves the old object in place. The bytes are then stored twice. That is accepted.

Bucket versioning should be off. Each run overwrites the dump and the manifest. With versioning on, every previous copy would be retained. If the bucket already has versioning, a lifecycle rule must expire noncurrent versions of those two keys. The tool calls `s3:ListBucket`, `s3:HeadObject`, `s3:PutObject`, and `s3:AbortMultipartUpload`. Its IAM policy does not include `s3:DeleteObject` or `s3:GetObject`. Downloading objects for a restore is a separate operator action.

## Schedule

One long-running process.

1. Run the backup once at startup.
2. Sleep until the next time `CRON` matches in the timezone given by `TZ` (default `UTC`).
3. Run again. If a run is still holding the lock when a tick fires, skip that tick. When a run finishes, wait for the next future cron time. Do not run again immediately, and do not catch up on ticks that passed during the run.

`CRON` defaults to `0 3 * * *`, which is 03:00 every day.

A skipped tick exits 0. Overlapping runs are the lock's job, including a manual run started while the scheduled run is active.

## Checkpoint

A sqlite file at `/state/state.sqlite` records successful work so the next run can skip it. The archive is the source of truth. The sqlite file is disposable.

```sql
CREATE TABLE files (
  relative_path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime_ns INTEGER,
  sha256 TEXT NOT NULL,
  s3_key TEXT NOT NULL,
  uploaded_at TEXT NOT NULL
);

CREATE TABLE dump_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  source_name TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  uploaded_at TEXT NOT NULL
);
```

`mtime_ns` is null when the row was rebuilt from the bucket and the local mtime has not been checked yet. `uploaded_at` is UTC, ISO-8601. `sha256` is lowercase hex of the full file. Sqlite writes happen on one thread.

If the sqlite file is missing, the same run rebuilds it before deciding what to upload:

1. List every key under `<prefix>/originals/`.
2. HEAD each object and read its size and `sha256` user metadata.
3. Insert a row with `mtime_ns` null. Skip objects that have no `sha256` metadata. The file walk will upload those and write the metadata.

A null `mtime_ns` does not match the local file, so the walk hashes the file, HEADs the object, and skips the PUT when the checksum matches.

If the sqlite file is present but unreadable, the run exits 1 and prints that `/state/state.sqlite` must be deleted. It does not delete the file itself and it does not delete archive objects.

## One run

Take an exclusive lock on the state directory. If the lock is already held, log that the run was skipped and exit 0.

Refuse to start, before walking any files, when credentials are missing, the bucket is missing, or S3 returns access denied. Exit 1. Mark nothing done.

Walk regular files under `library`, `upload`, and `profile`. Do not follow symlinks. Reject any path that escapes its root. Skip a file modified less than 60 seconds before the run started, and skip a file whose mtime is in the future. The next run can pick it up. This avoids copying a file that is still being written.

The skip key for an unchanged file is size plus mtime. A replacement that keeps both the size and the mtime is not detected. Removing the sqlite row makes the next run hash the file and compare it to S3.

For each remaining file:

1. If the sqlite row has the same size and the same mtime, skip it. Do not hash and do not call S3.
2. Otherwise hash the file with one read.
3. HEAD the object. If it exists and its `sha256` user metadata equals that hash, update the sqlite row and skip the PUT.
4. Otherwise read the file again and upload it. Storage class is `DEEP_ARCHIVE`. User metadata `sha256` is the full-file hash from this read. A single PUT is used under 100 MB. At or above 100 MB the upload is multipart. S3 checksum verification is required on every attempt, so a corrupted transfer fails instead of being stored. The comparison key is still the full-file hash in user metadata.

An unchanged file is not read. A file whose checksum is already in S3 is read once. A file that is actually uploaded is read twice. The steady state is the first of those. The first backup of a new library is the third.
5. On success, upsert the sqlite row.
6. On failure, retry that file up to 3 times in this run, then move on. Abort a multipart upload that fails. Other files continue.

Default concurrency is 2 uploads.

After the file walk, copy the database dump, then write the manifest. Both of those uploads use storage class `STANDARD` and overwrite a single key. Readers see the previous object or the new one, not a partial object.

Exit 1 if any original failed, the dump is missing, the dump upload failed, or the manifest upload failed. Photos that already uploaded stay uploaded. The next run retries the failures and rewrites the two Standard objects.

Log one summary line: scanned, uploaded, skipped, failed, whether the dump was copied, and duration.

`--dry-run` takes the same lock, hashes and HEADs as needed, and prints the same decisions. It performs no PUT and writes no sqlite. If the sqlite file is missing, dry-run does not rebuild it. Every local file that is not skipped for being too new is reported as a would-be upload.

## Database dump

Choose the newest `*.sql.gz` in `/data/backups` by mtime. If two files share that mtime, choose the greater filename.

Skip the upload when `dump_state` already records that filename, size, and mtime. Otherwise PUT the file to `<prefix>/db/immich-latest.sql.gz` with user metadata `source-name` set to the filename, then replace the `dump_state` row.

Immich writes this dump on its own schedule. This tool only copies the newest file it finds. If the directory contains no `*.sql.gz`, the photo pass still runs, and the process then exits 1.

## Manifest

Build `manifest.csv` from every row in `files`, ordered by `relative_path`. A row stays after the local file disappears, because the manifest lists the archive, not the current disk.

Columns:

```text
relative_path,size,sha256,s3_key,uploaded_at
```

Write the CSV with standard quoting so a path that contains a comma or a quote is one field. PUT it to `<prefix>/manifest.csv`. An empty `files` table still uploads a header-only CSV.

The manifest contains only rows whose upload succeeded. A file that failed this run is absent until a later run succeeds.

## Failure behavior

| Situation | Result |
| --- | --- |
| Missing credentials, missing bucket, or access denied | Exit 1 before any upload. Nothing is marked done. |
| File fails 3 times | Leave it unmarked. Continue the run. Retry tomorrow. |
| Multipart upload fails | Abort it. |
| Sqlite file missing | Rebuild from the bucket listing, then continue. |
| Sqlite file unreadable | Exit 1. Ask the operator to delete `/state/state.sqlite`. Do not delete S3 objects. |
| Manifest or dump upload fails after photos landed | Exit 1. Photos stay. Next run rewrites both Standard objects. |
| No `sql.gz` in the backups directory | Finish the photo pass, then exit 1. |
| Lock already held | Log and exit 0. |
| Local file removed | Leave the S3 object and the sqlite row. |

## Restore

The first version does not ship a restore command. The README gives the manual steps:

1. Download `<prefix>/manifest.csv` and `<prefix>/db/immich-latest.sql.gz`. Both are Standard and are readable immediately.
2. Start a bulk restore of `<prefix>/originals/`. Bulk is the cheap tier and typically finishes within 48 hours. A standard restore typically finishes within 12 hours and costs more.
3. After the restore completes, copy `originals/library`, `originals/upload`, and `originals/profile` back under `UPLOAD_LOCATION`.
4. Restore Postgres from the dump using Immich's restore procedure.

A fresh Immich can also scan the restored files as a library. Without the dump it will not have the old albums, named people, favorites, or shares. The dump is what brings those back. Thumbnails and transcoded videos are left behind on purpose. Immich rebuilds them.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | required | AWS credential |
| `AWS_SECRET_ACCESS_KEY` | required | AWS credential |
| `AWS_REGION` | required | Bucket region |
| `S3_BUCKET` | required | Destination bucket |
| `S3_PREFIX` | `immich` | Key prefix inside the bucket |
| `DATA_DIR` | `/data` | Mount that contains `library`, `upload`, `profile`, and `backups` |
| `STATE_DIR` | `/state` | Read-write directory for sqlite and the lock |
| `TZ` | `UTC` | Timezone for the schedule |
| `CRON` | `0 3 * * *` | Cadence |
| `CONCURRENCY` | `2` | Parallel uploads |
| `MULTIPART_THRESHOLD_BYTES` | `104857600` | Multipart starts at this size |

`LIBRARY_DIR`, `UPLOAD_DIR`, `PROFILE_DIR`, and `BACKUP_DIR` override the four directories when they are not all under `DATA_DIR`.

The example Compose service mounts `${UPLOAD_LOCATION}` at `/data` read-only and a named volume at `/state`. `restart: unless-stopped`.

## Repository

Python package `immich-archive`. The Docker image runs the long-lived process above. Tests use pytest and moto. They do not call AWS.

Tests cover:

- An unchanged size and mtime skips hashing and skips S3.
- A size or mtime change rehashes. A HEAD whose stored checksum matches skips the PUT and updates sqlite.
- A failed PUT is absent from the manifest. A successful one is present.
- Keys stay under `originals/library`, `originals/upload`, and `originals/profile`.
- `thumbs` and `encoded-video` are never visited.
- A path that escapes the root is rejected.
- Dry-run performs no PUT and writes no sqlite.
- Originals are requested as `DEEP_ARCHIVE`. The manifest and the dump are requested as `STANDARD`.
- A second run while the lock is held exits 0 without uploading.
- A missing dump exits 1 after the photo pass.
- A missing sqlite file is rebuilt from listed keys and HEAD metadata.

## Out of scope

- Apache Iceberg, or any second ledger besides sqlite and the manifest.
- An Immich WebAssembly plugin, or a worker that downloads files through the Immich API.
- Thumbnails, transcoded videos, and external libraries.
- Deleting or lifecycle-expiring archive objects when Immich deletes a photo.
- A restore subcommand. The README documents the manual steps.
- Downloading Deep Archive objects to verify them. Integrity comes from S3 checksum verification on the way up and from the stored SHA-256.
