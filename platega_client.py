import httpx

PLATEGA_BASE_URL = "https://app.platega.io"


class PlategaClientError(Exception):
    pass


class PlategaClient:
    """Client for the Platega.io payment API."""

    def __init__(self, merchant_id: str, secret: str, base_url: str = PLATEGA_BASE_URL):
        self.merchant_id = merchant_id
        self.secret = secret
        self.base_url = base_url or PLATEGA_BASE_URL

    def _headers(self) -> dict:
        return {
            "X-MerchantId": self.merchant_id,
            "X-Secret": self.secret,
            "Content-Type": "application/json",
        }

    async def create_payment(
        self,
        amount: float,
        description: str,
        return_url: str,
        failed_url: str,
        payload: str = "",
        currency: str = "RUB",
    ) -> dict:
        """Create a payment (v2 endpoint — payer chooses method).

        amount is in rubles (float). Platega accepts it directly as the
        payment amount.
        """
        if not self.merchant_id or not self.secret:
            raise PlategaClientError("Platega not configured (merchant_id/secret missing)")

        body = {
            "paymentDetails": {"amount": amount, "currency": currency},
            "description": description,
            "return": return_url,
            "failedUrl": failed_url,
        }
        if payload:
            body["payload"] = payload

        url = f"{self.base_url}/v2/transaction/process"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=self._headers())
        if resp.status_code >= 400:
            raise PlategaClientError(f"Platega create error {resp.status_code}: {resp.text}")
        return resp.json()

    async def get_payment_status(self, transaction_id: str) -> dict:
        url = f"{self.base_url}/transaction/{transaction_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code == 404:
            return {"status": "NOT_FOUND"}
        if resp.status_code >= 400:
            raise PlategaClientError(f"Platega status error {resp.status_code}: {resp.text}")
        return resp.json()
