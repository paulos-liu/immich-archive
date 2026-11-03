# Immich Deep Archive

A sidecar for [Immich](https://immich.app/) that keeps a cheap copy of the photos in S3 Glacier Deep Archive. It is the copy you still have after the Immich disk is gone. Bringing the photos back takes about 12 hours (standard restore) or about 48 hours (bulk restore).

The tool reads the library from disk. It does not call the Immich API, and it is not an Immich plugin. Photo bytes are S3 objects. Object keys mirror the Immich path, so a restore copies the tree back into place.

## What is copied

From the Immich upload directory, mounted read-only at `/data`:

| Local directory | S3 key | Storage class |
| --- | --- | --- |
| `/data/library` | `<prefix>/originals/library/...` | `DEEP_ARCHIVE` |
| `/data/upload` | `<prefix>/originals/upload/...` | `DEEP_ARCHIVE` |
| `/data/profile` | `<prefix>/originals/profile/...` | `DEEP_ARCHIVE` |
| newest `*.sql.gz` in `/data/backups` | `<prefix>/db/immich-latest.sql.gz` | `STANDARD` |
| generated each run | `<prefix>/manifest.csv` | `STANDARD` |

`<prefix>` defaults to `immich`. `thumbs`, `encoded-video`, and every other directory under `/data` are left alone. External libraries are out of scope.

Files that disappear from Immich stay in the archive. This tool never deletes an S3 object. Replacing a Deep Archive object, or deleting one by hand, still incurs the 180-day minimum charge on the previous bytes. A storage-template move writes a new key and leaves the old object in place.

Turn bucket versioning off. Each run overwrites the dump and the manifest. With versioning on, every previous copy is retained. If the bucket already has versioning, expire noncurrent versions of those two keys.

## Schedule

`immich-archive serve` does three things:

1. Run a backup once at startup.
2. Listen on port 8080 for the admin dashboard.
3. Sleep until the next time `CRON` matches in `TZ`, then run again.

The default cron is `0 3 * * *`, which is 03:00 every day in the container timezone (`UTC` unless `TZ` is set). A tick that passes while a run is still going is not replayed. If a run already holds the lock, that tick is skipped and the process exits 0 for that attempt. The next wake is the next future cron match.

`immich-archive run` performs one backup and exits. `immich-archive run --dry-run` takes the same lock, hashes and checks S3 as needed, and does not upload or write the checkpoint. There is no restore command.

## Admin dashboard

Set `ADMIN_PASSWORD`. Until that variable is set, the dashboard responds that it is off and does not show archive paths or counts. After it is set, sign in to see the last run, the next run, archive file count and bytes, and the dump name and age. Search reads the checkpoint. It does not walk the disk. Cron and concurrency can be changed; the new cron is what the scheduler waits for. Bucket, region, and credentials are shown read-only. The secret key is not shown. **Run now** and **Dry run** start a real run and are refused while a backup already holds the lock.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | required | AWS credential |
| `AWS_SECRET_ACCESS_KEY` | required | AWS credential |
| `AWS_REGION` | required | Bucket region |
| `S3_BUCKET` | required | Destination bucket |
| `S3_PREFIX` | `immich` | Key prefix inside the bucket |
| `DATA_DIR` | `/data` | Mount that contains `library`, `upload`, `profile`, and `backups` |
| `STATE_DIR` | `/state` | Writable directory for the sqlite checkpoint and the lock |
| `TZ` | `UTC` | Timezone for the schedule |
| `CRON` | `0 3 * * *` | Cadence |
| `CONCURRENCY` | `2` | Parallel uploads |
| `MULTIPART_THRESHOLD_BYTES` | `104857600` | Multipart starts at this size |
| `ADMIN_PASSWORD` | unset | Dashboard password. The dashboard stays off until this is set |
| `HOST` | `0.0.0.0` | Dashboard bind address |
| `PORT` | `8080` | Dashboard port |
| `AWS_ENDPOINT_URL` | unset | Optional S3-compatible endpoint. Leave unset for AWS |

`LIBRARY_DIR`, `UPLOAD_DIR`, `PROFILE_DIR`, and `BACKUP_DIR` override the four directories when they are not all under `DATA_DIR`.

A missing checkpoint is rebuilt from the bucket listing and `HEAD` metadata, then the same run continues. Objects with no `sha256` user metadata are uploaded again so the checksum gets written. If the sqlite file is present but unreadable, the run exits 1 and tells you to delete `/state/state.sqlite`. It does not delete that file, and it does not delete archive objects.

## IAM

The backup calls `ListBucket`, `HeadObject`, `PutObject`, and `AbortMultipartUpload`. It never calls `GetObject` or `DeleteObject`.

AWS authorizes `HeadObject` with `s3:GetObject`. The example policy includes `s3:GetObject` so the checksum check works, and it does not include `s3:DeleteObject`. Restoring objects is a separate operator action and needs its own permission.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload"
      ],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/*"
    }
  ]
}
```

## Run with Compose

`compose.example.yml` mounts `${UPLOAD_LOCATION}` at `/data` read-only and a named volume at `/state`. The service restarts unless it is stopped. If Immich keeps backups or the profile directory outside `UPLOAD_LOCATION`, mount those paths over `/data/backups` and `/data/profile`.

```sh
docker compose -f compose.example.yml up -d --build
```

## Restore

There is no restore command. Do this by hand:

1. Download `<prefix>/manifest.csv` and `<prefix>/db/immich-latest.sql.gz`. Both are Standard and can be downloaded immediately. The manifest lists every archived original: `relative_path`, `size`, `sha256`, `s3_key`, and `uploaded_at`.
2. Start a bulk restore of `<prefix>/originals/`. Bulk is the cheap tier and typically finishes within 48 hours. A standard restore typically finishes within 12 hours and costs more.
3. After the restore completes, copy `originals/library`, `originals/upload`, and `originals/profile` back under `UPLOAD_LOCATION`.
4. Restore Postgres from the dump using Immich's restore procedure.

A fresh Immich can scan the restored files as a library. Without the dump it will not have the old albums, named people, favorites, or shares. Thumbnails and transcoded videos are left behind on purpose. Immich rebuilds them.

## Checkpoint

`/state/state.sqlite` records successful uploads so the next run can skip them. The bucket is the source of truth. The sqlite file can be deleted; the next run rebuilds it. The skip key for an unchanged file is size plus mtime. A replacement that keeps both is not detected until that sqlite row is removed.

## Development

Python 3.11 or newer. Tests use pytest and moto. They do not call AWS.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
