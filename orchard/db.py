"""SQLite persistence. Every connection uses foreign keys; migrations are additive."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS project(id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL, question TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS concepts(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS terms(id TEXT PRIMARY KEY, concept_id TEXT NOT NULL REFERENCES concepts(id), original TEXT NOT NULL, normalized TEXT NOT NULL, line INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY, parent_id TEXT REFERENCES queries(id), document_id TEXT REFERENCES documents(id), label TEXT NOT NULL, query TEXT NOT NULL, provider TEXT NOT NULL, term_ids TEXT NOT NULL, rationale TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS criteria(id TEXT PRIMARY KEY, inclusion TEXT NOT NULL, exclusion TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, query_id TEXT REFERENCES queries(id), query TEXT NOT NULL, provider TEXT NOT NULL, fingerprint TEXT NOT NULL, parameters TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, finished TEXT, cursor TEXT, retrieved INTEGER NOT NULL DEFAULT 0, total INTEGER, error TEXT, refresh_of TEXT REFERENCES runs(id));
CREATE INDEX IF NOT EXISTS runs_fingerprint ON runs(fingerprint);
CREATE TABLE IF NOT EXISTS papers(id TEXT PRIMARY KEY, title TEXT, abstract TEXT, authors TEXT, year TEXT, doi TEXT, publication_type TEXT, created TEXT NOT NULL, merged_into TEXT REFERENCES papers(id), revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS identifiers(kind TEXT NOT NULL, value TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers(id), PRIMARY KEY(kind,value));
CREATE TABLE IF NOT EXISTS source_records(id TEXT PRIMARY KEY, provider TEXT NOT NULL, source_id TEXT NOT NULL, paper_id TEXT NOT NULL REFERENCES papers(id), raw TEXT NOT NULL, sha256 TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(provider,source_id,sha256));
CREATE TABLE IF NOT EXISTS discoveries(run_id TEXT NOT NULL REFERENCES runs(id), source_record_id TEXT NOT NULL REFERENCES source_records(id), created TEXT NOT NULL, PRIMARY KEY(run_id,source_record_id));
CREATE INDEX IF NOT EXISTS source_paper ON source_records(paper_id);
CREATE TABLE IF NOT EXISTS duplicates(id TEXT PRIMARY KEY, a TEXT NOT NULL REFERENCES papers(id), b TEXT NOT NULL REFERENCES papers(id), reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL, UNIQUE(a,b));
CREATE TABLE IF NOT EXISTS relationships(id TEXT PRIMARY KEY, a TEXT NOT NULL REFERENCES papers(id), b TEXT NOT NULL REFERENCES papers(id), kind TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id), stage TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT, notes TEXT, reviewer TEXT NOT NULL, criteria_id TEXT NOT NULL REFERENCES criteria(id), created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id), kind TEXT NOT NULL, url TEXT NOT NULL, repository TEXT, evidence TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'found_unverified', access_note TEXT, created TEXT NOT NULL, verified TEXT, UNIQUE(paper_id,kind,url,evidence));
CREATE TABLE IF NOT EXISTS fulltexts(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id), content TEXT NOT NULL, sha256 TEXT NOT NULL, origin TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assessments(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id), fields TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, entity_id TEXT, detail TEXT NOT NULL, created TEXT NOT NULL);
INSERT OR IGNORE INTO schema_version VALUES(2);
CREATE TABLE IF NOT EXISTS eyelink_scans(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id), status TEXT NOT NULL, result TEXT, pdf_url TEXT, pdf_path TEXT, pdf_sha256 TEXT, steps TEXT NOT NULL DEFAULT '[]', repositories INTEGER NOT NULL DEFAULT 0, eyelink_files INTEGER NOT NULL DEFAULT 0, error TEXT, created TEXT NOT NULL, finished TEXT);
CREATE INDEX IF NOT EXISTS eyelink_scans_paper ON eyelink_scans(paper_id);
CREATE TABLE IF NOT EXISTS repository_checks(id TEXT PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES eyelink_scans(id), paper_id TEXT NOT NULL REFERENCES papers(id), repository TEXT NOT NULL, identifier TEXT NOT NULL, url TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL, files_listed INTEGER NOT NULL DEFAULT 0, eyelink_files INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repository_files(id TEXT PRIMARY KEY, check_id TEXT NOT NULL REFERENCES repository_checks(id), path TEXT NOT NULL, size INTEGER, url TEXT, kind TEXT NOT NULL, header TEXT NOT NULL, note TEXT, created TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'Audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'Audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_decisions BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT,'Screening history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_queries BEFORE UPDATE ON queries BEGIN SELECT RAISE(ABORT,'Queries are versioned'); END;
'''


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)
            c.execute('INSERT OR IGNORE INTO project VALUES(1,?,?,?)', ('Orchard', None, now()))

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None


def audit(c, event, entity=None, detail=None):
    c.execute('INSERT INTO audit(event,entity_id,detail,created) VALUES(?,?,?,?)', (event, entity, dump(detail or {}), now()))
