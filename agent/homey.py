"""Homey Pro-koppeling voor het 🏠-blokje op het dashboard.

Praat via Athoms cloud-proxy (https://<HOMEY_ID>.connect.athom.com) met een lokale
API-key (Homey Web App → Instellingen → API-keys). Alleen stdlib, geen extra deps.
Vereist in .env: HOMEY_ID en HOMEY_API_KEY. Bedienen (lampen uit) vereist de scope
"Apparaten: bedienen" op de sleutel.
"""
from __future__ import annotations

import json
import os
import urllib.request


def _cfg() -> tuple[str, str]:
    return os.environ.get("HOMEY_ID", ""), os.environ.get("HOMEY_API_KEY", "")


def geconfigureerd() -> bool:
    return all(_cfg())


def _request(path: str, method: str = "GET", body: dict | None = None):
    hid, key = _cfg()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://{hid}.connect.athom.com/api{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _cap(d: dict, naam: str):
    c = (d.get("capabilitiesObj") or {}).get(naam)
    return None if not c else c.get("value")


def samenvatting() -> dict:
    """Compacte stand van het huis: energie, klimaat, lampen aan, auto, deur, apparaten."""
    zones = {z["id"]: z["name"] for z in _request("/manager/zones/zone").values()}
    devices = list(_request("/manager/devices/device").values())

    def zone(d: dict) -> str:
        return zones.get(d.get("zone"), "")

    out: dict = {
        "zon_w": None, "net_w": None, "auto": None, "klimaat": [], "lampen_aan": [],
        "deur": None, "stofzuiger": None, "tv_aan": None, "aantal": len(devices),
    }
    gezien_lampen: set[tuple[str, str]] = set()
    for d in devices:
        cls, naam = d.get("class"), d.get("name", "")
        if cls == "solarpanel" and out["zon_w"] is None:
            out["zon_w"] = _cap(d, "measure_power")
        elif (cls == "sensor" and out["net_w"] is None
              and _cap(d, "measure_power") is not None and _cap(d, "meter_power") is not None):
            out["net_w"] = _cap(d, "measure_power")  # slimme meter (P1)
        elif "tesla" in naam.lower() or (cls == "other" and _cap(d, "measure_battery") is not None
                                          and _cap(d, "measure_power") is not None):
            out["auto"] = {"naam": naam, "batterij": _cap(d, "measure_battery"),
                           "laadt": (_cap(d, "measure_power") or 0) > 0}
        elif cls == "thermostat" and _cap(d, "measure_temperature") is not None:
            out["klimaat"].append({
                "kamer": zone(d) or naam,
                "temp": round(float(_cap(d, "measure_temperature")), 1),
                "doel": _cap(d, "target_temperature"),
            })
        elif cls == "light" and _cap(d, "onoff") is True:
            sleutel = (naam.strip(), zone(d))
            if sleutel not in gezien_lampen:
                gezien_lampen.add(sleutel)
                out["lampen_aan"].append({"id": d["id"], "naam": naam.strip(), "kamer": zone(d)})
        elif cls == "lock":
            out["deur"] = {"naam": naam, "dicht": _cap(d, "locked")}
        elif cls == "vacuumcleaner":
            out["stofzuiger"] = {"naam": naam, "batterij": _cap(d, "measure_battery")}
        elif cls == "tv":
            out["tv_aan"] = _cap(d, "onoff")
    out["klimaat"].sort(key=lambda k: (0 if "woon" in k["kamer"].lower() else 1, k["kamer"]))
    return out


def zet_aan_uit(device_id: str, aan: bool) -> None:
    _request(f"/manager/devices/device/{device_id}/capability/onoff", "PUT", {"value": bool(aan)})
