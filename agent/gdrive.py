"""Google Drive-documentenhub voor de gezins-agent (map "Birdy 2.0").

De agent roept dit aan via Bash:
    python /app/agent/gdrive.py tree
    python /app/agent/gdrive.py list "00 Inbox"
    python /app/agent/gdrive.py mkdir "10 Gezin/Evi/School"
    python /app/agent/gdrive.py upload lokaal.pdf --to "00 Inbox" [--naam "nieuw.pdf"]
    python /app/agent/gdrive.py move "00 Inbox/x.pdf" --to "10 Gezin/Evi/School" [--naam "..."]
    python /app/agent/gdrive.py download "10 Gezin/Evi/School/x.pdf" --naar /tmp/x.pdf
    python /app/agent/gdrive.py search "kamp"
    python /app/agent/gdrive.py link "10 Gezin/Evi/School/x.pdf"
    python /app/agent/gdrive.py read "20 Huishouden/Huishoudhandboek"
    python /app/agent/gdrive.py write-doc "20 Huishouden/Huishoudhandboek" --van /tmp/handboek.txt

Paden zijn relatief aan de hoofdmap (DRIVE_ROOT_FOLDER_ID in .env). Vereist de
OAuth-koppeling (GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN, zie scripts/google_consent.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from agent.google_auth import DRIVE_SCOPE, google_credentials
except ImportError:  # aangeroepen als los script: python /app/agent/gdrive.py
    from google_auth import DRIVE_SCOPE, google_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIMES = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


def _service():
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("google-api-python-client is niet geïnstalleerd")
    return build("drive", "v3", credentials=google_credentials([DRIVE_SCOPE]), cache_discovery=False)


def _root() -> str:
    root = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
    if not root:
        sys.exit("DRIVE_ROOT_FOLDER_ID ontbreekt in .env")
    return root


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _children(svc, parent: str, only_folders: bool = False, name: str | None = None) -> list[dict]:
    q = f"'{parent}' in parents and trashed=false"
    if only_folders:
        q += f" and mimeType='{FOLDER_MIME}'"
    if name is not None:
        q += f" and name='{_q(name)}'"
    out, token = [], None
    while True:
        resp = svc.files().list(
            q=q, fields="nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime)",
            orderBy="folder,name", pageSize=100, pageToken=token,
        ).execute()
        out.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return out


def _resolve(svc, path: str, must_exist: bool = True) -> dict | None:
    """Pad relatief aan de hoofdmap → bestand/map-dict, of None."""
    node = {"id": _root(), "name": "", "mimeType": FOLDER_MIME}
    for seg in [s for s in path.replace("\\", "/").split("/") if s.strip()]:
        matches = _children(svc, node["id"], name=seg.strip())
        if not matches:
            if must_exist:
                sys.exit(f"Niet gevonden in Drive: '{path}' (segment '{seg}')")
            return None
        node = matches[0]
    return node


def _ensure_folder(svc, path: str) -> str:
    parent = _root()
    for seg in [s for s in path.replace("\\", "/").split("/") if s.strip()]:
        matches = _children(svc, parent, only_folders=True, name=seg.strip())
        if matches:
            parent = matches[0]["id"]
        else:
            made = svc.files().create(
                body={"name": seg.strip(), "mimeType": FOLDER_MIME, "parents": [parent]},
                fields="id",
            ).execute()
            parent = made["id"]
    return parent


def _fmt(f: dict) -> str:
    kind = "📁" if f["mimeType"] == FOLDER_MIME else "📄"
    return f"{kind} {f['name']}"


def cmd_tree(depth: int) -> None:
    svc = _service()

    def walk(folder_id: str, prefix: str, level: int) -> None:
        for f in _children(svc, folder_id):
            print(f"{prefix}{_fmt(f)}")
            if f["mimeType"] == FOLDER_MIME and level < depth:
                walk(f["id"], prefix + "  ", level + 1)

    print("(hoofdmap Birdy 2.0)")
    walk(_root(), "  ", 1)


def cmd_list(path: str) -> None:
    svc = _service()
    node = _resolve(svc, path) if path else {"id": _root(), "mimeType": FOLDER_MIME}
    if node["mimeType"] != FOLDER_MIME:
        print(_fmt(node))
        return
    files = _children(svc, node["id"])
    if not files:
        print("(leeg)")
    for f in files:
        print(f"{_fmt(f)}  · gewijzigd {f.get('modifiedTime', '')[:10]}")


def cmd_mkdir(path: str) -> None:
    svc = _service()
    _ensure_folder(svc, path)
    print(f"Map aanwezig: {path}")


def cmd_upload(local: str, to: str, naam: str | None) -> None:
    from googleapiclient.http import MediaFileUpload

    src = Path(local)
    if not src.exists():
        sys.exit(f"Lokaal bestand niet gevonden: {local}")
    svc = _service()
    folder = _ensure_folder(svc, to)
    media = MediaFileUpload(str(src), resumable=False)
    made = svc.files().create(
        body={"name": naam or src.name, "parents": [folder]},
        media_body=media, fields="id, name, webViewLink",
    ).execute()
    print(f"Geüpload: {to}/{made['name']}\nLink: {made.get('webViewLink', '')}")


def cmd_move(path: str, to: str, naam: str | None) -> None:
    svc = _service()
    node = _resolve(svc, path)
    folder = _ensure_folder(svc, to)
    old_parents = ",".join(
        svc.files().get(fileId=node["id"], fields="parents").execute().get("parents", [])
    )
    body = {"name": naam} if naam else None
    moved = svc.files().update(
        fileId=node["id"], addParents=folder, removeParents=old_parents,
        body=body, fields="id, name, webViewLink",
    ).execute()
    print(f"Verplaatst naar: {to}/{moved['name']}\nLink: {moved.get('webViewLink', '')}")


def cmd_download(path: str, naar: str) -> None:
    svc = _service()
    node = _resolve(svc, path)
    dest = Path(naar)
    export = GOOGLE_DOC_MIMES.get(node["mimeType"])
    if export:
        mime, ext = export
        if not dest.suffix:
            dest = dest.with_suffix(ext)
        data = svc.files().export(fileId=node["id"], mimeType=mime).execute()
    else:
        data = svc.files().get_media(fileId=node["id"]).execute()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"Gedownload naar: {dest}")


def _docs_service():
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=google_credentials([DRIVE_SCOPE]), cache_discovery=False)


def cmd_read(path: str) -> None:
    """Tekstinhoud van een bestand printen (Google Docs als platte tekst)."""
    svc = _service()
    node = _resolve(svc, path)
    mime = node["mimeType"]
    if mime.startswith("application/vnd.google-apps."):
        data = svc.files().export(fileId=node["id"], mimeType="text/plain").execute()
    elif mime.startswith("text/") or mime in ("application/json", "text/markdown"):
        data = svc.files().get_media(fileId=node["id"]).execute()
    else:
        sys.exit(f"'{path}' is geen tekstbestand ({mime}) — gebruik download + de Read-tool.")
    print(data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data)


def cmd_write_doc(path: str, van: str) -> None:
    """Google Doc aanmaken of de volledige inhoud vervangen met de tekst uit een lokaal bestand."""
    src = Path(van)
    if not src.exists():
        sys.exit(f"Lokaal bestand niet gevonden: {van}")
    text = src.read_text()

    svc = _service()
    node = _resolve(svc, path, must_exist=False)
    if node is None:
        folder_path, _, name = path.replace("\\", "/").rpartition("/")
        parent = _ensure_folder(svc, folder_path) if folder_path else _root()
        node = svc.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.document",
                  "parents": [parent]},
            fields="id, mimeType, name",
        ).execute()
    if node["mimeType"] != "application/vnd.google-apps.document":
        sys.exit(f"'{path}' bestaat al maar is geen Google Doc ({node['mimeType']}).")

    docs = _docs_service()
    doc = docs.documents().get(documentId=node["id"], fields="body(content(endIndex))").execute()
    end = doc["body"]["content"][-1]["endIndex"] - 1  # laatste newline mag niet weg
    requests = []
    if end > 1:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end}}})
    if text.strip():
        requests.append({"insertText": {"location": {"index": 1}, "text": text.rstrip("\n")}})
    if requests:
        docs.documents().batchUpdate(documentId=node["id"], body={"requests": requests}).execute()
    info = svc.files().get(fileId=node["id"], fields="webViewLink").execute()
    print(f"Bijgewerkt: {path}\nLink: {info.get('webViewLink', '')}")


def cmd_link(path: str) -> None:
    svc = _service()
    node = _resolve(svc, path)
    info = svc.files().get(fileId=node["id"], fields="webViewLink").execute()
    print(info.get("webViewLink", "(geen link)"))


def _path_of(svc, file_id: str, cache: dict) -> str | None:
    """Pad t.o.v. de hoofdmap, of None als het bestand er niet onder valt."""
    root = _root()
    segs: list[str] = []
    current = file_id
    for _ in range(12):
        if current == root:
            return "/".join(reversed(segs))
        if current not in cache:
            cache[current] = svc.files().get(fileId=current, fields="name, parents").execute()
        info = cache[current]
        parents = info.get("parents", [])
        if not parents:
            return None
        segs.append(info.get("name", "?"))
        current = parents[0]
    return None


def cmd_search(text: str) -> None:
    svc = _service()
    resp = svc.files().list(
        q=f"(name contains '{_q(text)}' or fullText contains '{_q(text)}') and trashed=false",
        fields="files(id, name, mimeType, webViewLink)", pageSize=30,
    ).execute()
    cache: dict = {}
    hits = 0
    for f in resp.get("files", []):
        parent = (svc.files().get(fileId=f["id"], fields="parents").execute().get("parents") or [None])[0]
        path = _path_of(svc, parent, cache) if parent else None
        if path is None:
            continue  # buiten de Birdy-map
        loc = f"{path}/" if path else ""
        print(f"{_fmt(f)}  · in {loc or '(hoofdmap)'}  · {f.get('webViewLink', '')}")
        hits += 1
    if not hits:
        print(f"Niets gevonden voor '{text}' in de Birdy-map.")


def poll_inbox(workspace) -> list[tuple[str, str]]:
    """Nieuwe bestanden in '00 Inbox' downloaden naar de lokale inbox.

    Geeft [(drive-naam, lokaal relatief pad)] terug; bijhouden welke ids al gezien
    zijn gebeurt in workspace/memory/drive_inbox_seen.json. Sync — draai in een thread.
    """
    workspace = Path(workspace)
    seen_file = workspace / "memory" / "drive_inbox_seen.json"
    try:
        seen = set(json.loads(seen_file.read_text()))
    except (OSError, json.JSONDecodeError):
        seen = set()

    svc = _service()
    inbox = _resolve(svc, "00 Inbox", must_exist=False)
    if not inbox:
        return []
    new: list[tuple[str, str]] = []
    for f in _children(svc, inbox["id"]):
        if f["mimeType"] == FOLDER_MIME or f["id"] in seen:
            continue
        sub = "photos" if str(f["mimeType"]).startswith("image/") else "docs"
        dest = workspace / "inbox" / sub / f["name"]
        try:
            export = GOOGLE_DOC_MIMES.get(f["mimeType"])
            if export:
                mime, ext = export
                dest = dest.with_suffix(ext)
                data = svc.files().export(fileId=f["id"], mimeType=mime).execute()
            else:
                data = svc.files().get_media(fileId=f["id"]).execute()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except Exception:
            continue  # volgende poll opnieuw proberen
        seen.add(f["id"])
        new.append((f["name"], f"inbox/{sub}/{dest.name}"))

    if new:
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        seen_file.write_text(json.dumps(sorted(seen)))
    return new


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pt = sub.add_parser("tree")
    pt.add_argument("--diepte", type=int, default=2)
    pl = sub.add_parser("list")
    pl.add_argument("pad", nargs="?", default="")
    pm = sub.add_parser("mkdir")
    pm.add_argument("pad")
    pu = sub.add_parser("upload")
    pu.add_argument("lokaal")
    pu.add_argument("--to", required=True, help="doelmap in Drive")
    pu.add_argument("--naam")
    pv = sub.add_parser("move")
    pv.add_argument("pad")
    pv.add_argument("--to", required=True, help="doelmap in Drive")
    pv.add_argument("--naam", help="nieuwe bestandsnaam")
    pd = sub.add_parser("download")
    pd.add_argument("pad")
    pd.add_argument("--naar", required=True, help="lokaal doelpad")
    ps = sub.add_parser("search")
    ps.add_argument("tekst")
    pk = sub.add_parser("link")
    pk.add_argument("pad")
    pr = sub.add_parser("read")
    pr.add_argument("pad")
    pw = sub.add_parser("write-doc")
    pw.add_argument("pad")
    pw.add_argument("--van", required=True, help="lokaal tekstbestand met de nieuwe inhoud")
    args = p.parse_args()

    if args.cmd == "tree":
        cmd_tree(args.diepte)
    elif args.cmd == "list":
        cmd_list(args.pad)
    elif args.cmd == "mkdir":
        cmd_mkdir(args.pad)
    elif args.cmd == "upload":
        cmd_upload(args.lokaal, args.to, args.naam)
    elif args.cmd == "move":
        cmd_move(args.pad, args.to, args.naam)
    elif args.cmd == "download":
        cmd_download(args.pad, args.naar)
    elif args.cmd == "search":
        cmd_search(args.tekst)
    elif args.cmd == "link":
        cmd_link(args.pad)
    elif args.cmd == "read":
        cmd_read(args.pad)
    elif args.cmd == "write-doc":
        cmd_write_doc(args.pad, args.van)


if __name__ == "__main__":
    main()
