from __future__ import annotations
import argparse, base64, hashlib, json, os, re, sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote
import requests, yaml
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
DB=ROOT/"data"/"monitor.db"
REPORT=ROOT/"reports"/"latest.md"
API="https://api.github.com"
EXTS={".md",".txt",".rst",".yaml",".yml",".json",".prompt",".xml"}
NAMES={"skill.md","agents.md","claude.md","gemini.md"}
MAX_BYTES=500_000

def load_yaml(path: Path)->dict[str,Any]:
    with path.open(encoding="utf-8") as f: return yaml.safe_load(f) or {}

def db_connect():
    DB.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS repo_state(repo TEXT PRIMARY KEY,sha TEXT,release_tag TEXT,checked_at TEXT,meta_hash TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,repo TEXT,kind TEXT,old_value TEXT,new_value TEXT,url TEXT,detected_at TEXT,UNIQUE(repo,kind,new_value))""")
    con.execute("""CREATE TABLE IF NOT EXISTS artifacts(repo TEXT,path TEXT,blob_sha TEXT,content_hash TEXT,category TEXT,seen_at TEXT,url TEXT,excerpt TEXT,PRIMARY KEY(repo,path,blob_sha))""")
    con.execute("""CREATE TABLE IF NOT EXISTS analyses(repo TEXT,path TEXT,blob_sha TEXT,analyzed_at TEXT,model TEXT,score REAL,is_relevant INTEGER,payload_json TEXT,PRIMARY KEY(repo,path,blob_sha))""")
    con.execute("""CREATE TABLE IF NOT EXISTS web_items(source_id TEXT,item_id TEXT,title TEXT,url TEXT,published TEXT,summary TEXT,content_hash TEXT,first_seen TEXT,PRIMARY KEY(source_id,item_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS web_sources_state(source_id TEXT PRIMARY KEY,initialized_at TEXT)""")
    con.commit()
    return con

def gh(path:str):
    h={"Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"aec-intelligence-monitor/0.2"}
    if os.getenv("GITHUB_TOKEN"): h["Authorization"]="Bearer "+os.environ["GITHUB_TOKEN"]
    r=requests.get(API+path,headers=h,timeout=30); r.raise_for_status(); return r.json()

def tracked(path:str)->bool:
    p=Path(path); return p.name.lower() in NAMES or p.suffix.lower() in EXTS

def classify(path:str,content:str)->str:
    p=path.lower()
    if Path(p).name in NAMES or "agent skill" in content[:5000].lower(): return "Agent Skill / инструкции агента"
    if any(x in p for x in ("prompt","template","checklist")) or re.search(r"(?im)^\s*(prompt|system prompt|user prompt)\s*:",content[:12000]): return "Промпт / шаблон / чек-лист"
    if p.endswith((".yaml",".yml",".json")): return "Конфигурация / схема"
    return "Документация / текст"

def fetch_file(repo:str,path:str,ref:str):
    return gh(f"/repos/{repo}/contents/{quote(path,safe='/')}?ref={ref}")

def snapshot(repo:str):
    meta=gh(f"/repos/{repo}"); branch=meta.get("default_branch") or "main"
    commit=gh(f"/repos/{repo}/commits/{branch}")
    try:
        rel=gh(f"/repos/{repo}/releases/latest"); tag,url=rel.get("tag_name"),rel.get("html_url")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code==404: tag,url=None,None
        else: raise
    m={"description":meta.get("description"),"license":(meta.get("license") or {}).get("spdx_id"),"branch":branch,"archived":meta.get("archived")}
    digest=hashlib.sha256(repr(sorted(m.items())).encode()).hexdigest()
    return {"sha":commit["sha"],"commit_url":commit["html_url"],"tag":tag,"release_url":url,"meta_hash":digest,"meta":m}

def inspect_changes(con,repo,old_sha,new_sha,now,license_id,topics):
    data=gh(f"/repos/{repo}/compare/{quote(old_sha,safe='')}...{quote(new_sha,safe='')}")
    found=[]
    for item in data.get("files",[]):
        path,status=item.get("filename",""),item.get("status","modified")
        if not tracked(path): continue
        url=f"https://github.com/{repo}/blob/{new_sha}/{quote(path,safe='/')}"
        excerpt,category="","Файл"
        if status!="removed":
            try:
                raw=fetch_file(repo,path,new_sha)
                if raw.get("size",0)>MAX_BYTES or raw.get("encoding")!="base64":
                    excerpt="[Файл пропущен: размер или формат не поддерживается]"
                else:
                    content=base64.b64decode(raw["content"]).decode("utf-8",errors="replace")
                    digest=hashlib.sha256(content.encode()).hexdigest()
                    category=classify(path,content); excerpt=re.sub(r"\s+"," ",content).strip()[:420]
                    blob_sha=raw.get("sha","")
                    con.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                        (repo,path,blob_sha,digest,category,now,url,excerpt))
                    if os.getenv("OPENAI_API_KEY") and blob_sha:
                        exists=con.execute("SELECT 1 FROM analyses WHERE repo=? AND path=? AND blob_sha=?",(repo,path,blob_sha)).fetchone()
                        if not exists:
                            try:
                                from src.analyzer import analyze_artifact
                                result=analyze_artifact(repo=repo,path=path,category=category,content=content,
                                    source_url=url,license_id=license_id,topics=topics)
                                con.execute("INSERT OR REPLACE INTO analyses VALUES(?,?,?,?,?,?,?,?)",
                                    (repo,path,blob_sha,now,result.get("model","unknown"),result["overall_score"],
                                     int(result["is_relevant"]),json.dumps(result,ensure_ascii=False)))
                            except Exception as err:
                                print(f"LLM analysis failed for {repo}/{path}: {err}")
            except Exception as e: excerpt=f"[Не удалось получить содержимое: {str(e)[:150]}]"
        else: url=f"https://github.com/{repo}/commit/{new_sha}"
        label={"added":"Добавлен","removed":"Удалён","renamed":"Переименован"}.get(status,"Изменён")
        found.append({"path":path,"label":label,"category":category,"url":url,"excerpt":excerpt,"changes":item.get("changes",0)})
    return found,data.get("html_url") or f"https://github.com/{repo}/compare/{old_sha}...{new_sha}"


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)

def _clean_html(value: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(value or "")
        text = " ".join(parser.parts)
    except Exception:
        text = value or ""
    return re.sub(r"\s+", " ", text).strip()[:1200]

def _child_text(node, names: set[str]) -> str:
    for child in list(node):
        if child.tag.split("}")[-1].lower() in names:
            return "".join(child.itertext()).strip()
    return ""

def parse_feed(xml_text: str, feed_url: str) -> list[dict[str, str]]:
    """Parse common RSS 2.0 and Atom feeds without executing feed content."""
    root = ET.fromstring(xml_text)
    root_name = root.tag.split("}")[-1].lower()
    if root_name == "rss":
        channel = next((x for x in list(root) if x.tag.split("}")[-1].lower() == "channel"), root)
        entries = [x for x in list(channel) if x.tag.split("}")[-1].lower() == "item"]
    elif root_name == "feed":
        entries = [x for x in list(root) if x.tag.split("}")[-1].lower() == "entry"]
    else:
        entries = [x for x in root.iter() if x.tag.split("}")[-1].lower() in {"item", "entry"}]
    result = []
    for entry in entries[:100]:
        title = _child_text(entry, {"title"}) or "Без названия"
        link = ""
        item_id = _child_text(entry, {"guid", "id"})
        for child in list(entry):
            if child.tag.split("}")[-1].lower() != "link":
                continue
            href = child.attrib.get("href")
            rel = child.attrib.get("rel", "alternate")
            if href and rel in {"alternate", ""}:
                link = href
                break
            if not href and child.text:
                link = child.text.strip()
                break
        link = link or item_id
        if link:
            from urllib.parse import urljoin
            link = urljoin(feed_url, link)
        item_id = item_id or link
        if not item_id:
            continue
        published = _child_text(entry, {"pubdate", "published", "updated", "date"})
        summary = _child_text(entry, {"description", "summary", "content", "encoded"})
        if published:
            try:
                published = parsedate_to_datetime(published).isoformat()
            except (TypeError, ValueError, OverflowError):
                try:
                    published = datetime.fromisoformat(published.replace("Z", "+00:00")).isoformat()
                except ValueError:
                    pass
        result.append({"item_id": item_id[:1000], "title": _clean_html(title),
                       "url": link, "published": published[:100] if published else "",
                       "summary": _clean_html(summary)})
    return result

def fetch_web_feed(feed: dict[str, Any]) -> list[dict[str, str]]:
    url = feed["url"]
    response = requests.get(url, headers={
        "User-Agent": "aec-intelligence-monitor/0.3 (+RSS reader)",
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"
    }, timeout=30)
    response.raise_for_status()
    if len(response.content) > 5_000_000:
        raise ValueError("RSS/Atom feed exceeds 5 MB limit")
    return parse_feed(response.content.decode(response.encoding or "utf-8", errors="replace"), url)

def monitor_web_sources(con, cfg, now, events, errors):
    web_cfg = cfg.get("web", {})
    if not web_cfg.get("enabled", False):
        return 0
    feeds = web_cfg.get("rss_feeds", [])
    checked = 0
    for feed in feeds:
        source_id = feed.get("id") or feed.get("url")
        name = feed.get("name") or source_id
        try:
            items = fetch_web_feed(feed)
            checked += 1
            initialized = con.execute(
                "SELECT 1 FROM web_sources_state WHERE source_id=?", (source_id,)
            ).fetchone() is not None
            new_count = updated_count = 0
            for item in items:
                fingerprint = hashlib.sha256(
                    json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                old = con.execute(
                    "SELECT title,url,published,summary,content_hash FROM web_items WHERE source_id=? AND item_id=?",
                    (source_id, item["item_id"])
                ).fetchone()
                if old is None:
                    con.execute(
                        "INSERT INTO web_items(source_id,item_id,title,url,published,summary,content_hash,first_seen) VALUES(?,?,?,?,?,?,?,?)",
                        (source_id, item["item_id"], item["title"], item["url"],
                         item["published"], item["summary"], fingerprint, now)
                    )
                    if initialized:
                        new_count += 1
                        kind = "Новая внешняя публикация"
                    else:
                        continue
                elif old[4] != fingerprint:
                    con.execute(
                        "UPDATE web_items SET title=?,url=?,published=?,summary=?,content_hash=? WHERE source_id=? AND item_id=?",
                        (item["title"], item["url"], item["published"], item["summary"],
                         fingerprint, source_id, item["item_id"])
                    )
                    if initialized:
                        updated_count += 1
                        kind = "Обновлена внешняя публикация"
                    else:
                        continue
                else:
                    continue
                details = item["title"]
                if item["published"]:
                    details += f" (дата: {item['published']})"
                if item["summary"]:
                    details += "\\n\\n" + item["summary"][:500]
                events.append({
                    "repo": name, "kind": kind, "details": details,
                    "url": item["url"] or feed["url"], "files": []
                })
            con.execute(
                "INSERT OR IGNORE INTO web_sources_state(source_id,initialized_at) VALUES(?,?)",
                (source_id, now)
            )
            con.commit()
            if not initialized:
                print(f"Web baseline: {name}; indexed={len(items)}")
            else:
                print(f"Web feed: {name}; items={len(items)}; new={new_count}; updated={updated_count}")
        except Exception as e:
            errors.append((f"WEB:{name}", str(e)[:250]))
    return checked

def run_once():
    cfg=load_yaml(ROOT/"config"/"sources.yaml"); repos=cfg.get("github",{}).get("repositories",[])
    con,now,events,errors=db_connect(),datetime.now(timezone.utc).isoformat(),[],[]
    for item in repos:
        repo=item["repo"]
        try:
            s=snapshot(repo)
            old=con.execute("SELECT sha,release_tag,meta_hash FROM repo_state WHERE repo=?",(repo,)).fetchone()
            if old is None:
                events.append({"repo":repo,"kind":"Первичная фиксация","details":"Baseline создан; исторические файлы не считаются новыми находками.","url":f"https://github.com/{repo}","files":[]})
            else:
                old_sha,old_tag,old_meta=old
                if old_sha!=s["sha"]:
                    changed,cmp_url=inspect_changes(con,repo,old_sha,s["sha"],now,s["meta"].get("license"),load_yaml(ROOT/"config"/"topics.yaml").get("topics",[]))
                    con.execute("INSERT OR IGNORE INTO events(repo,kind,old_value,new_value,url,detected_at) VALUES(?,?,?,?,?,?)",(repo,"commit",old_sha,s["sha"],cmp_url,now))
                    events.append({"repo":repo,"kind":"Новый commit","details":f"{old_sha[:8]} -> {s['sha'][:8]}","url":cmp_url,"files":changed})
                if old_tag!=s["tag"] and s["tag"]:
                    con.execute("INSERT OR IGNORE INTO events(repo,kind,old_value,new_value,url,detected_at) VALUES(?,?,?,?,?,?)",(repo,"release",old_tag or "",s["tag"],s["release_url"],now))
                    events.append({"repo":repo,"kind":"Новый Release","details":f"{old_tag or 'нет'} -> {s['tag']}","url":s["release_url"],"files":[]})
                if old_meta!=s["meta_hash"]:
                    events.append({"repo":repo,"kind":"Изменились метаданные","details":f"Лицензия: {s['meta'].get('license') or 'не указана'}","url":f"https://github.com/{repo}","files":[]})
            con.execute("""INSERT INTO repo_state VALUES(?,?,?,?,?) ON CONFLICT(repo) DO UPDATE SET sha=excluded.sha,release_tag=excluded.release_tag,checked_at=excluded.checked_at,meta_hash=excluded.meta_hash""",(repo,s["sha"],s["tag"],now,s["meta_hash"]))
            con.commit()
        except Exception as e: errors.append((repo,str(e)[:250]))
    web_checked = monitor_web_sources(con, cfg, now, events, errors)
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    lines=["# Ежедневная сводка AEC Intelligence Monitor","",f"Проверено (UTC): {now}",f"GitHub-репозиториев: {len(repos)}",f"Внешних RSS/Atom-лент проверено: {web_checked}","","## События",""]
    if not events: lines += ["Новых событий относительно сохранённого состояния не обнаружено.",""]
    for ev in events:
        lines += [f"### {ev['repo']} — {ev['kind']}",ev["details"],f"[Открыть источник]({ev['url']})",""]
        for f in ev["files"]:
            lines += [f"- **{f['label']} файл**: {f['path']} — {f['category']}; изменений строк: {f['changes']}",f"  [Открыть файл]({f['url']})"]
            if f["excerpt"]: lines.append("  Фрагмент: "+f["excerpt"])
        if ev["files"]: lines.append("")
    lines += ["## LLM-анализ",""]
    if os.getenv("OPENAI_API_KEY"):
        rows=con.execute("SELECT repo,path,score,payload_json FROM analyses ORDER BY analyzed_at DESC LIMIT 30").fetchall()
        if rows:
            for repo_name,path_name,score,payload in rows:
                item=json.loads(payload)
                lines.append(f"- **{score:.2f}/5 — {repo_name}/{path_name}**: {item.get('summary','')}")
                lines.append(f"  Применение: {'; '.join(item.get('practical_use_cases',[])[:3]) or 'не указано'}")
                lines.append(f"  Лицензия: {item.get('license_status')}; СП/ГОСТ: {item.get('russian_standards_status')}; действие: {item.get('recommended_action')}")
        else:
            lines.append("Пока нет завершённых LLM-анализов.")
    else:
        lines.append("LLM отключён: добавьте OPENAI_API_KEY и OPENAI_MODEL в Secrets GitHub Actions.")
    lines += ["## Ошибки проверки",""]+[f"- {r}: {e}" for r,e in errors] or ["Ошибок не зарегистрировано."]
    lines += ["","## Ограничения","Отслеживаются изменения текстовых и конфигурационных файлов в GitHub commits и новые публикации из настроенных RSS/Atom-лент. Первый опрос каждой ленты создаёт baseline без выдачи старых публикаций как новых. Извлечённые фрагменты не являются экспертной оценкой."]
    REPORT.write_text("\n".join(lines),encoding="utf-8")
    print(f"Report: {REPORT}; events={len(events)}; errors={len(errors)}")
    if errors:
        raise SystemExit(f"{len(errors)} source(s) failed; see report for details")

if __name__=="__main__":
    argparse.ArgumentParser().parse_args()
    run_once()
