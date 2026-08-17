"""OKX v5 REST API 轻量封装。

- 自动从环境变量读取凭证：
    OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE
    OKX_PROXY            例如 http://127.0.0.1:7890（可选）
    OKX_SIMULATED        "1"/"true" 为模拟盘，默认开启（安全优先）
- 默认模拟盘（x-simulated-trading: 1），显式关闭才上实盘。
- 网络错误 / 5xx 自动重试（指数退避）。

用法::

    from okx_client import OkxClient

    client = OkxClient()                    # 读环境变量，默认模拟盘
    client.get_ticker("BTC-USDT")
    client.get_balance()
    client.place_order("BTC-USDT", side="buy", sz="0.001")

等拿到 API Key 后，只需要设置环境变量（或写入 .env 再 source）::

    export OKX_API_KEY=xxx
    export OKX_SECRET_KEY=xxx
    export OKX_PASSPHRASE=xxx
    export OKX_PROXY=http://127.0.0.1:7890   # 可选
    export OKX_SIMULATED=0                   # 想上实盘时才关
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.okx.com"

# 模拟盘是默认值：只有显式设置 OKX_SIMULATED=0/false/no 才上实盘
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


class OkxError(Exception):
    """OKX 业务错误（code != "0"）。"""

    def __init__(self, code: str, msg: str, data: Any = None):
        super().__init__(f"OKX error {code}: {msg}")
        self.code = code
        self.msg = msg
        self.data = data


class OkxClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        passphrase: Optional[str] = None,
        proxy: Optional[str] = None,
        simulated: Optional[bool] = None,
        max_retries: int = 3,
        timeout: float = 10.0,
    ):
        self.api_key = api_key or os.environ.get("OKX_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("OKX_SECRET_KEY", "")
        self.passphrase = passphrase or os.environ.get("OKX_PASSPHRASE", "")
        self.simulated = _env_flag("OKX_SIMULATED", True) if simulated is None else simulated
        self.max_retries = max_retries
        self.timeout = timeout

        proxy = proxy or os.environ.get("OKX_PROXY") or None
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

        if not self.simulated:
            logger.warning("OKX_SIMULATED 已关闭，请求将发往【实盘】")

    # ---------- 底层 ----------

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method: str, request_path: str, body: str) -> dict:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        return headers

    def request(self, method: str, path: str, params: Optional[dict] = None,
                body: Optional[dict] = None, auth: bool = True) -> Any:
        """发请求，返回 data 字段；业务错误抛 OkxError，传输层错误重试。"""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        query = ""
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params)
        request_path = path + query
        body_str = json.dumps(body) if body else ""

        headers = self._headers(method, request_path, body_str) if auth else {}
        url = BASE_URL + request_path

        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method.upper(), url,
                    headers=headers,
                    data=body_str or None,
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                payload = resp.json()
                code = str(payload.get("code", ""))
                if code != "0":
                    raise OkxError(code, payload.get("msg", ""), payload.get("data"))
                return payload.get("data")
            except OkxError:
                raise  # 业务错误不重试
            except (requests.RequestException, ValueError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    wait = 2 ** attempt
                    logger.warning("OKX 请求失败 (%s)，%.1fs 后重试 [%d/%d]",
                                   e, wait, attempt + 1, self.max_retries)
                    time.sleep(wait)
        raise OkxError("-1", f"请求失败，已重试 {self.max_retries} 次: {last_exc}")

    def get(self, path: str, params: Optional[dict] = None, auth: bool = True) -> Any:
        return self.request("GET", path, params=params, auth=auth)

    def post(self, path: str, body: Optional[dict] = None, auth: bool = True) -> Any:
        return self.request("POST", path, body=body, auth=auth)

    # ---------- 常用接口 ----------

    # 行情（公共接口，无需 Key）
    def get_ticker(self, inst_id: str) -> Any:
        return self.get("/api/v5/market/ticker", {"instId": inst_id}, auth=False)

    def get_candles(self, inst_id: str, bar: str = "1H", limit: str = "100") -> Any:
        return self.get("/api/v5/market/candles",
                        {"instId": inst_id, "bar": bar, "limit": limit}, auth=False)

    # 账户
    def get_balance(self, ccy: Optional[str] = None) -> Any:
        return self.get("/api/v5/account/balance", {"ccy": ccy})

    def get_positions(self, inst_type: str = "SPOT") -> Any:
        return self.get("/api/v5/account/positions", {"instType": inst_type})

    # 交易
    def place_order(self, inst_id: str, side: str, sz: str,
                    td_mode: str = "cash", ord_type: str = "market",
                    px: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Any:
        body = {
            "instId": inst_id,
            "tdMode": td_mode,   # 现货用 cash，合约用 cross/isolated
            "side": side,        # buy / sell
            "ordType": ord_type,  # market / limit
            "sz": sz,
            "px": px,
            "clOrdId": cl_ord_id,
        }
        return self.post("/api/v5/trade/order", body)

    def cancel_order(self, inst_id: str, ord_id: Optional[str] = None,
                     cl_ord_id: Optional[str] = None) -> Any:
        return self.post("/api/v5/trade/cancel-order",
                         {"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id})

    def get_order(self, inst_id: str, ord_id: Optional[str] = None,
                  cl_ord_id: Optional[str] = None) -> Any:
        return self.get("/api/v5/trade/order",
                        {"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id})


if __name__ == "__main__":
    # 冒烟测试：公共行情接口不需要 Key，可以直接跑
    logging.basicConfig(level=logging.INFO)
    c = OkxClient()
    ticker = c.get_ticker("BTC-USDT")
    print(json.dumps(ticker, indent=2, ensure_ascii=False))
