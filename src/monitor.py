from __future__ import annotations
import argparse, hashlib, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests, yaml

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "monitor.db"
REPORT = ROOT / "reports" / "latest.md"
API = "https://api.github.com"

def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def db_connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS repo_state(
        repo TEXT PRIMARY KEY, sha TEXT, release_tag TEXT, checked_at TEXT, meta_hash TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, kind TEXT, old_value TEXT,
        new_value TEXT, url TEXT, detected_at TEXT, UNIQUE(repo,kind,new_value))""")
    con.commit()
    return con

def gh(path: str):
    headers = {"Accept":"application/vnd.github+json",
               "X-GitHub-Api-Version":"2022-11-28",
               "User-Agent":"aec-prompt-monitor/0.1"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    r = requests.get(API + path, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()

def snapshot(repo: str):
    meta = gh(f"/repos/{repo}")
    branch = meta.get("default_branch") or "main"
    commit = gh(f"/repos/{repo}/commits/{branch}")
    try:
        release = gh(f"/repos/{repo}/releases/latest")
        tag, release_url = release.get("tag_name"), release.get("html_url")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            tag, release_url = None, None
        else:
            raise
    m = {"description":meta.get("description"),
         "license":(meta.get("license") or {}).get("spdx_id"),
         "branch":branch, "archived":meta.get("archived")}
    digest = hashlib.sha256(repr(sorted(m.items())).encode()).hexdigest()
    return {"sha":commit["sha"], "commit_url":commit["html_url"],
            "tag":tag, "release_url":release_url, "meta_hash":digest, "meta":m}

def run_once():
    cfg = load_yaml(ROOT / "config" / "sources.yaml")
    repos = cfg.get("github",{}).get("repositories",[])
    con, now, events, errors = db_connect(), datetime.now(timezone.utc).isoformat(), [], []
    for item in repos:
        repo = item["repo"]
        try:
            s = snapshot(repo)
            old = con.execute("SELECT sha,release_tag,meta_hash FROM repo_state WHERE repo=?",(repo,)).fetchone()
            if old is None:
                events.append((repo,"Первичная фиксация","Baseline создан; это не считается обновлением.",f"https://github.com/{repo}"))
            else:
                old_sha, old_tag, old_meta = old
                if old_sha != s["sha"]:
                    con.execute("INSERT OR IGNORE INTO events(repo,kind,old_value,new_value,url,detected_at) VALUES(?,?,?,?,?,?)",
                        (repo,"commit",old_sha,s["sha"],s["commit_url"],now))
                    events.append((repo,"Новый commit",f"{old_sha[:8]} → {s['sha'][:8]}",s["commit_url"]))
                if old_tag != s["tag"] and s["tag"]:
                    con.execute("INSERT OR IGNORE INTO events(repo,kind,old_value,new_value,url,detected_at) VALUES(?,?,?,?,?,?)",
                        (repo,"release",old_tag or "",s["tag"],s["release_url"],now))
                    events.append((repo,"Новый Release",f"{old_tag or 'нет'} → {s['tag']}",s["release_url"]))
                if old_meta != s["meta_hash"]:
                    events.append((repo,"Изменились метаданные",f"Лицензия: {s['meta'].get('license') or 'не указана'}",f"https://github.com/{repo}"))
            con.execute("""INSERT INTO repo_state VALUES(?,?,?,?,?)
                ON CONFLICT(repo) DO UPDATE SET sha=excluded.sha, release_tag=excluded.release_tag,
                checked_at=excluded.checked_at, meta_hash=excluded.meta_hash""",
                (repo,s["sha"],s["tag"],now,s["meta_hash"]))
            con.commit()
        except Exception as e:
            errors.append((repo,str(e)[:250]))
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    lines = ["# Ежедневная сводка AEC Prompt & Agent Monitor","",
             f"Проверено (UTC): {now}",f"Источников в конфигурации: {len(repos)}","","## События",""]
    if events:
        for repo,kind,details,url in events:
            lines += [f"### {repo} — {kind}",details,f"[Открыть источник]({url})",""]
    else:
        lines += ["Новых событий относительно сохранённого состояния не обнаружено.",""]
    lines += ["## Ошибки проверки",""]
    lines += [f"- `{repo}`: {err}" for repo,err in errors] or ["Ошибок не зарегистрировано."]
    lines += ["","## Ограничение версии","",
        "MVP отслеживает commits, Releases и часть метаданных. Смысловой анализ изменённых файлов, веб-поиск и LLM-оценка входят в следующие этапы."]
    REPORT.write_text("\n".join(lines),encoding="utf-8")
    print(f"Report: {REPORT}; events={len(events)}; errors={len(errors)}")

if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    run_once()
