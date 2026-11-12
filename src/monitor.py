from __future__ import annotations
import argparse, base64, hashlib, json, os, re, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
import requests, yaml

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
    REPORT.parent.mkdir(parents=True,exist_ok=True)
    lines=["# Ежедневная сводка AEC Intelligence Monitor","",f"Проверено (UTC): {now}",f"Источников: {len(repos)}","","## События",""]
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
    lines += ["","## Ограничения","Сейчас отслеживаются изменения текстовых и конфигурационных файлов в GitHub commits. Извлечённые фрагменты не являются экспертной оценкой. Web/RSS и LLM-анализ будут добавлены отдельно."]
    REPORT.write_text("\n".join(lines),encoding="utf-8")
    print(f"Report: {REPORT}; events={len(events)}; errors={len(errors)}")

if __name__=="__main__":
    argparse.ArgumentParser().parse_args()
    run_once()
