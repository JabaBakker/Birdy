"""Todoist-lijstjes voor de gezins-agent (boodschappen & acties).

De agent roept dit aan via Bash:
    python /app/agent/todoist.py add "kwark" --lijst boodschappen
    python /app/agent/todoist.py add "band plakken" --lijst acties --wanneer "zaterdag"
    python /app/agent/todoist.py list --lijst boodschappen
    python /app/agent/todoist.py done "kwark" --lijst boodschappen
    python /app/agent/todoist.py projects

Vereist in .env: TODOIST_API_TOKEN. Lijstnamen matchen op de projectnaam
(hoofdletter-ongevoelig), dus "boodschappen" vindt het project "Boodschappen".
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys

API = "https://api.todoist.com/api/v1"


def _request(method: str, path: str, **kwargs):
    import requests

    token = os.environ.get("TODOIST_API_TOKEN", "")
    if not token:
        sys.exit(
            "Todoist is nog niet gekoppeld (TODOIST_API_TOKEN ontbreekt in .env). "
            "Meld dit kort in de chat in plaats van het opnieuw te proberen."
        )
    resp = requests.request(
        method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kwargs
    )
    if not resp.ok:
        sys.exit(f"Todoist-API-fout {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.text else None


def _list_all(path: str, params: dict | None = None) -> list[dict]:
    """Lijst-endpoints van de v1-API pagineren via {'results': [...], 'next_cursor': ...}."""
    params = dict(params or {})
    out: list[dict] = []
    while True:
        data = _request("GET", path, params=params)
        if isinstance(data, list):  # voor het geval een endpoint plat antwoordt
            return data
        out.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            return out
        params["cursor"] = cursor


def _projects() -> list[dict]:
    return _list_all("/projects")


def _project(name: str) -> dict:
    wanted = name.strip().lower()
    projects = _projects()
    exact = [p for p in projects if p["name"].lower() == wanted]
    partial = [p for p in projects if wanted in p["name"].lower()]
    match = (exact or partial)
    if not match:
        names = ", ".join(p["name"] for p in projects) or "(geen projecten)"
        sys.exit(f"Geen Todoist-project gevonden voor '{name}'. Beschikbaar: {names}")
    return match[0]


def cmd_projects() -> None:
    for p in _projects():
        print(f"- {p['name']}")


def cmd_add(content: str, lijst: str, wanneer: str | None) -> None:
    project = _project(lijst)
    body: dict = {"content": content, "project_id": project["id"]}
    if wanneer:
        body["due_string"] = wanneer
        body["due_lang"] = "nl"
    task = _request("POST", "/tasks", json=body)
    due = task.get("due") or {}
    extra = f" ({due.get('string')})" if due.get("string") else ""
    print(f"Toegevoegd aan {project['name']}: {task['content']}{extra}")


def cmd_list(lijst: str | None) -> None:
    if lijst:
        project = _project(lijst)
        tasks = _list_all("/tasks", {"project_id": project["id"]})
        header = project["name"]
    else:
        tasks = _list_all("/tasks")
        header = "alle lijsten"
    if not tasks:
        print(f"{header}: leeg ✅")
        return
    print(f"{header}:")
    for t in tasks:
        due = (t.get("due") or {}).get("string", "")
        print(f"- {t['content']}" + (f"  · {due}" if due else ""))
        if t.get("description"):
            print(f"    ↳ notitie: {t['description']}")


def cmd_done(query: str, lijst: str | None) -> None:
    params = {"project_id": _project(lijst)["id"]} if lijst else None
    tasks = _list_all("/tasks", params)
    wanted = query.strip().lower()
    matches = [t for t in tasks if wanted in t["content"].lower()]
    if not matches:
        sys.exit(f"Geen open taak gevonden die op '{query}' lijkt.")
    if len(matches) > 1:
        opts = "; ".join(t["content"] for t in matches[:5])
        sys.exit(f"Meerdere taken lijken op '{query}': {opts}. Wees specifieker.")
    _request("POST", f"/tasks/{matches[0]['id']}/close")
    print(f"Afgevinkt: {matches[0]['content']}")


def cmd_notitie(query: str, lijst: str | None, tekst: str) -> None:
    params = {"project_id": _project(lijst)["id"]} if lijst else None
    tasks = _list_all("/tasks", params)
    wanted = query.strip().lower()
    matches = [t for t in tasks if wanted in t["content"].lower()]
    if not matches:
        sys.exit(f"Geen open taak gevonden die op '{query}' lijkt.")
    if len(matches) > 1:
        opts = "; ".join(t["content"] for t in matches[:5])
        sys.exit(f"Meerdere taken lijken op '{query}': {opts}. Wees specifieker.")
    _request("POST", f"/tasks/{matches[0]['id']}", json={"description": tekst})
    print(f"Notitie opgeslagen bij: {matches[0]['content']}")


# ── hulpfuncties voor het dashboard (geen CLI) ──


def lijst(naam: str) -> list[dict]:
    try:
        project = _project(naam)
        tasks = _list_all("/tasks", {"project_id": project["id"]})
        out = [{
            "id": str(t["id"]),
            "tekst": t["content"],
            "due": ((t.get("due") or {}).get("date") or "")[:10],
            "notitie": (t.get("description") or "")[:400],
        } for t in tasks]
        out.sort(key=lambda t: (t["due"] == "", t["due"]))  # deadlines eerst, oplopend
        return out[:50]  # de Vandaag-tab toont de top; de verdiepende pagina alles
    except BaseException:
        return []


def afvinken(task_id: str) -> bool:
    try:
        _request("POST", f"/tasks/{task_id}/close")
        return True
    except BaseException:
        return False


def afgevinkt(naam: str) -> list[dict]:
    """Onlangs afgevinkte taken (7 dagen) van een project, voor de herstel-lijst."""
    try:
        project = _project(naam)
        nu = datetime.now(timezone.utc)
        data = _request("GET", "/tasks/completed/by_completion_date", params={
            "project_id": project["id"],
            "since": (nu - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": nu.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        items = data.get("items") or data.get("results") or []
        return [{"id": str(t["id"]), "tekst": t["content"]} for t in items][:6]
    except BaseException:
        return []


def heropen(task_id: str) -> bool:
    try:
        _request("POST", f"/tasks/{task_id}/reopen")
        return True
    except BaseException:
        return False


def deadline(task_id: str, datum: str) -> bool:
    try:
        _request("POST", f"/tasks/{task_id}", json={"due_date": datum})
        return True
    except BaseException:
        return False


def toevoegen(lijst: str, tekst: str, datum: str = "") -> dict | None:
    try:
        project = _project(lijst)
        body = {"content": tekst, "project_id": project["id"]}
        if datum:
            body["due_date"] = datum
        t = _request("POST", "/tasks", json=body)
        return {"id": str(t["id"]), "tekst": t["content"],
                "due": ((t.get("due") or {}).get("date") or "")[:10]}
    except BaseException:
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("add")
    pa.add_argument("content")
    pa.add_argument("--lijst", required=True, help="bijv. boodschappen of acties")
    pa.add_argument("--wanneer", help='bijv. "morgen" of "zaterdag" (Nederlands mag)')
    pl = sub.add_parser("list")
    pl.add_argument("--lijst")
    pd = sub.add_parser("done")
    pd.add_argument("query")
    pd.add_argument("--lijst")
    pn = sub.add_parser("notitie")
    pn.add_argument("query")
    pn.add_argument("--lijst")
    pn.add_argument("--tekst", required=True, help="de notitie (vervangt de bestaande)")
    sub.add_parser("projects")
    args = p.parse_args()

    if args.cmd == "add":
        cmd_add(args.content, args.lijst, args.wanneer)
    elif args.cmd == "list":
        cmd_list(args.lijst)
    elif args.cmd == "done":
        cmd_done(args.query, args.lijst)
    elif args.cmd == "notitie":
        cmd_notitie(args.query, args.lijst, args.tekst)
    else:
        cmd_projects()


if __name__ == "__main__":
    main()
