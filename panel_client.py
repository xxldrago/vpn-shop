from typing import Optional
import httpx


class PanelClientError(Exception):
    pass


class PanelClient:
    """Client for the Amnezia Web Panel JSON API using bearer tokens."""

    def __init__(self, panel_url: str, token: str):
        self.panel_url = panel_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.panel_url}{path}"
        headers = {**self._headers(), **(kwargs.pop("headers", {}) or {})}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise PanelClientError(f"Panel error {resp.status_code}: {resp.text}")
        return resp.json()

    async def _request_raw(self, method: str, path: str, **kwargs):
        url = f"{self.panel_url}{path}"
        headers = {**self._headers(), **(kwargs.pop("headers", {}) or {})}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise PanelClientError(f"Panel error {resp.status_code}: {resp.text}")
        return resp.json()

    # ---- Auth proxy (login) ----

    async def login(self, username: str, password: str, captcha: Optional[str] = None) -> dict:
        body = {"username": username, "password": password}
        if captcha:
            body["captcha"] = captcha
        url = f"{self.panel_url}/api/auth/login"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            raise PanelClientError(f"Login error {resp.status_code}: {resp.text}")
        return resp.json()

    # ---- Servers ----

    async def list_servers(self):
        """Get full server list (with protocols) by pulling the JSON backup."""
        data = await self._request_raw("GET", "/api/settings/backup/download")
        return data.get("servers", [])

    async def get_servers_with_protocols(self, installed_only: bool = True):
        servers = await self.list_servers()
        result = []
        for idx, server in enumerate(servers):
            protocols = []
            for pkey, pconf in (server.get("protocols") or {}).items():
                if installed_only and not pconf.get("installed"):
                    continue
                protocols.append({"key": pkey, "display_name": pconf.get("display_name", pkey)})
            result.append({"server_id": idx, "server": server, "protocols": protocols})
        return result

    # ---- Users ----

    async def list_users(self, search: str = "", page: int = 1, size: int = 100):
        params = {"search": search, "page": page, "size": size}
        url = f"{self.panel_url}/api/users"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, headers=self._headers())
        if resp.status_code >= 400:
            raise PanelClientError(f"List users error {resp.status_code}: {resp.text}")
        return resp.json()

    async def find_user_by_username(self, username: str):
        data = await self.list_users(search=username, size=200)
        users = data.get("users", [])
        for user in users:
            if user.get("username") == username:
                return user
        return None

    async def get_user(self, uid: str):
        data = await self.list_users(search="", size=2000)
        users = data.get("users", data if isinstance(data, list) else [])
        for user in users:
            if user.get("id") == uid:
                return user
        return None

    async def create_panel_user(self, username: str, password: str, email: str = "", role: str = "user"):
        body = {"username": username, "password": password, "role": role}
        if email:
            body["email"] = email
        return await self._request("POST", "/api/users/add", json=body)

    async def update_panel_user(self, uid: str, **fields):
        return await self._request("POST", f"/api/users/{uid}/update", json=fields)

    async def attach_connection_to_user(self, uid: str, server_id: int, client_id: str, protocol: str, name: str = ""):
        body = {
            "server_id": server_id,
            "client_id": client_id,
            "protocol": protocol,
            "name": name,
        }
        return await self._request("POST", f"/api/users/{uid}/connections/add", json=body)

    # ---- Connections ----

    async def add_connection(self, server_id: int, protocol: str, name: str = "", user_id: str = ""):
        """Create a connection for a specific server/protocol.

        When user_id is provided the panel links the generated client to that
        panel user automatically. Returns the full response incl. config.
        """
        body = {"protocol": protocol, "name": name}
        if user_id:
            body["user_id"] = user_id
        return await self._request("POST", f"/api/servers/{server_id}/connections/add", json=body)

    async def toggle_connection(self, server_id: int, protocol: str, client_id: str, disable: bool = True):
        body = {"protocol": protocol, "client_id": client_id, "enable": not disable}
        path = f"/api/servers/{server_id}/connections/toggle"
        url = f"{self.panel_url}{path}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=self._headers())
        return resp.status_code, resp.text

    async def get_user_connections(self, uid: str):
        url = f"{self.panel_url}/api/users/{uid}/connections"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise PanelClientError(f"Get user connections error {resp.status_code}: {resp.text}")
        return resp.json()

    async def get_connection_config(self, server_id: int, protocol: str, client_id: str):
        url = f"{self.panel_url}/api/servers/{server_id}/connections/config"
        body = {"protocol": protocol, "client_id": client_id}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PanelClientError(f"Get config error {resp.status_code}: {resp.text}")
        return resp.json()
