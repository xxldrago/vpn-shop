"""Mock Amnezia Web Panel for end-to-end testing of the shop integration."""
import json
import uuid
from datetime import datetime

import base64
import struct
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

DATA = {"servers": [
    {"name": "nl-01", "host": "1.2.3.4", "protocols": {
        "awg": {"installed": True, "display_name": "AmneziaWG", "port": "55424"},
        "xray": {"installed": True, "display_name": "Xray", "port": "443"},
    }},
    {"name": "de-01", "host": "5.6.7.8", "protocols": {
        "awg2": {"installed": True, "display_name": "AmneziaWG 2.0", "port": "55425"},
    }},
]}
USERS = []
CONNECTIONS = []


@app.get("/api/settings/backup/download")
async def backup():
    return JSONResponse({"servers": DATA["servers"], "users": USERS, "user_connections": CONNECTIONS})


@app.get("/api/users")
async def list_users(search: str = "", page: int = 1, size: int = 100):
    filtered = [u for u in USERS if not search or search.lower() in u["username"].lower()]
    result = []
    for u in filtered:
        result.append({
            "id": u["id"], "username": u["username"], "role": u["role"],
            "email": u.get("email"), "telegramId": u.get("telegramId"), "enabled": True,
        })
    return {"users": result, "total": len(result)}


@app.post("/api/users/add")
async def add_user(request: Request):
    body = await request.json()
    u = {
        "id": str(uuid.uuid4()),
        "username": body["username"],
        "email": body.get("email", ""),
        "role": body.get("role", "user"),
        "password_hash": "x",
        "telegramId": body.get("telegramId"),
    }
    USERS.append(u)
    return JSONResponse({"status": "success", "user_id": u["id"]})


@app.post("/api/users/{uid}/update")
async def update_user(uid: str, request: Request):
    body = await request.json()
    for u in USERS:
        if u["id"] == uid:
            for k, v in body.items():
                u[k] = v
            return {"status": "success"}
    return JSONResponse({"status": "error"}, status_code=404)


@app.post("/api/servers/{sid}/connections/add")
async def add_connection(sid: int, request: Request):
    body = await request.json()
    server = DATA["servers"][sid]
    cid = str(uuid.uuid4())
    config = (
        "[Interface]\n"
        "PrivateKey = TEST\n"
        f"Address = 10.8.0.{len(CONNECTIONS) + 2}/32\n"
        "\n[Peer]\n"
        f"PublicKey = PEERKEY\n"
        f"Endpoint = {server['host']}:{server['protocols'][body['protocol']]['port']}\n"
        "AllowedIPs = 0.0.0.0/0\n"
    )
    conn = {
        "id": str(uuid.uuid4()),
        "user_id": body.get("user_id"),
        "server_id": sid,
        "protocol": body["protocol"],
        "client_id": cid,
        "name": body.get("name", "Connection"),
    }
    CONNECTIONS.append(conn)

    # Emulate the Amnezia config_payloads() output: split the config into short
    # QR frames the way the real panel does.
    def qr_chunks(text):
        payload = text.encode("utf-8")
        n = (len(payload) + 143) // 144
        size = (len(payload) + n - 1) // n
        out = []
        for i in range(n):
            part = payload[i * size:(i + 1) * size]
            frame = struct.pack(">hBBI", 1984, n, i, len(part)) + part
            out.append(base64.urlsafe_b64encode(frame).decode("utf-8").rstrip("="))
        return out

    return JSONResponse({
        "client_id": cid,
        "config": config,
        "vpn_link": f"vpn://test-{cid[:8]}",
        "vpn_name": body.get("name", "Connection"),
        "vpn_qr_chunks": qr_chunks(config),
    })


@app.get("/api/users/{uid}/connections")
async def user_connections(uid: str):
    return {"connections": [c for c in CONNECTIONS if c.get("user_id") == uid]}