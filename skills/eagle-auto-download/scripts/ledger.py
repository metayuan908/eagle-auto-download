"""Additive, library-scoped file identities, project links and durable attempts."""
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ACCEPTED = {'imported', 'reused'}
PROTECTED = ACCEPTED | {'uncertain', 'needs_metadata_review'}


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize(db, path):
    if db.execute("SELECT value FROM settings WHERE key='workflow_schema'").fetchone():
        return
    if db.execute('SELECT count(*) FROM assets').fetchone()[0]:
        backup = path.parent / 'state-backups'
        backup.mkdir(parents=True, exist_ok=True)
        target = backup / (path.name + '-' + uuid.uuid4().hex + '.bak')
        with sqlite3.connect(target) as destination:
            db.backup(destination)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS files (
            library TEXT NOT NULL, sha256 TEXT NOT NULL, eagle_id TEXT NOT NULL,
            verified_at TEXT NOT NULL, PRIMARY KEY(library,sha256,eagle_id));
        CREATE TABLE IF NOT EXISTS project_links (
            asset_id TEXT PRIMARY KEY, project_url TEXT NOT NULL, sha256 TEXT,
            library TEXT, eagle_id TEXT, folder_id TEXT, status TEXT NOT NULL,
            metadata_policy TEXT, replaced_by_asset_id TEXT, verified_at TEXT);
        CREATE TABLE IF NOT EXISTS attempts (
            attempt_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, kind TEXT NOT NULL,
            library TEXT NOT NULL, started_at TEXT NOT NULL, is_retry INTEGER NOT NULL,
            result TEXT NOT NULL, response_ids TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS attempts_asset ON attempts(asset_id,kind,library);
        INSERT INTO settings(key,value) VALUES ('workflow_schema','1');
    ''')
    db.commit()


def library_key(config):
    return str(Path(config['eagle']['expected_library_path']).expanduser().resolve())


def begin_attempt(db, asset_id, library, retry=False):
    if retry and db.execute(
            "SELECT 1 FROM attempts WHERE asset_id=? AND library=? AND kind='import' AND is_retry=1",
            (asset_id, library)).fetchone():
        raise RuntimeError('此素材已经重提一次；保留待对账，不再提交')
    attempt_id = uuid.uuid4().hex
    db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,NULL,NULL)',
               (attempt_id, asset_id, 'import', library, now(), int(retry), 'submitted'))
    db.execute("UPDATE assets SET status='uncertain',error=NULL WHERE id=?", (asset_id,))
    db.commit()  # A lost response or crash must never erase the submission.
    return attempt_id


def finish_attempt(db, attempt_id, result, ids=None, error=None):
    if attempt_id:
        db.execute('UPDATE attempts SET result=?,response_ids=COALESCE(?,response_ids),error=? '
                   'WHERE attempt_id=?',
                   (result, json.dumps(ids) if ids is not None else None, error, attempt_id))
        db.commit()


def saved_ids(db, asset_id, library):
    ids = [r[0] for r in db.execute(
        'SELECT eagle_id FROM project_links WHERE asset_id=? AND library=? AND eagle_id IS NOT NULL',
        (asset_id, library))]
    for encoded, in db.execute(
            'SELECT response_ids FROM attempts WHERE asset_id=? AND library=? '
            "AND kind='import' AND response_ids IS NOT NULL ORDER BY started_at DESC",
            (asset_id, library)):
        ids.extend(json.loads(encoded))
    return list(dict.fromkeys(ids))


def link(db, asset_id, record, library, eagle_id, folder_id, status, policy):
    db.execute('INSERT OR REPLACE INTO project_links VALUES (?,?,?,?,?,?,?,?,NULL,?)',
               (asset_id, record['project_url'], record.get('sha256'), library, eagle_id,
                folder_id, status, policy, now()))
    db.execute('UPDATE assets SET status=?,eagle_id=?,error=? WHERE id=?',
               (status, eagle_id, '共享素材元数据需审核；未覆盖原条目' if status ==
                'needs_metadata_review' else None, asset_id))
    db.execute("UPDATE attempts SET result=? WHERE asset_id=? AND library=? AND kind='import' "
               "AND result IN ('submitted','response_received','uncertain')",
               ('reconciled' if status in ACCEPTED else status, asset_id, library))
    if record.get('media_type') != 'bookmark':
        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?)',
                   (library, record['sha256'], eagle_id, now()))
    db.commit()


def summary(rows, mode):
    """An offline queue summary is not a fresh Eagle audit."""
    from preflight import selected
    counts, projects, represented = Counter(), {}, {}
    videos = {}
    for key, record, status, eagle_id in rows:
        if record.get('media_type') == 'video' and status in ACCEPTED:
            for source in [record['asset_url']] + record.get('sources', []):
                videos[(record['project_url'].rstrip('/'), source.rstrip('/'))] = key
    for key, record, status, eagle_id in rows:
        project = record['project_url'].rstrip('/')
        p = projects.setdefault(project, Counter())
        counts['records'] += 1
        if record.get('media_type') == 'bookmark' and mode == 'files' and not selected(record, mode):
            replacement = videos.get((project, record['asset_url'].rstrip('/')))
            if replacement:
                counts['represented_by_video'] += 1
                p['represented_by_video'] += 1
                represented[key] = replacement
            else:
                counts['awaiting_video'] += 1
                p['awaiting_video'] += 1
            if status in ACCEPTED:
                counts['legacy_bookmarks'] += 1
            continue
        if not selected(record, mode):
            counts['excluded_by_mode'] += 1
            continue
        if status in ACCEPTED:
            label = 'fallback_bookmarks' if record.get('download_fallback') and record.get(
                'media_type') == 'bookmark' else record.get('media_type', 'image')
            counts[label] += 1
            p['accepted'] += 1
            if status == 'reused':
                counts['reused'] += 1
        else:
            counts[status] += 1
            p['remaining'] += 1
    unique = {e for _, r, s, e in rows if s in ACCEPTED and e and
              r.get('media_type') != 'bookmark' and selected(r, mode)}
    counts['accepted_unique_files'] = len(unique)
    return {'counts': dict(counts), 'projects': {
        url: {**dict(p), 'complete': not p['remaining'] and not p['awaiting_video']}
        for url, p in sorted(projects.items())}, 'represented_by_video': represented,
        'complete': all(not p['remaining'] and not p['awaiting_video'] for p in projects.values()),
        'verification': 'local_ledger_not_live_audit'}
