# -*- coding: utf-8 -*-
"""Run the seven-role daily meeting through the Moonshot API.

The runner creates research artifacts and delegates deterministic Paper finalization.
It never approves or submits Live orders.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

from trading_team.collect import DEFAULT_TRADING_CALENDAR, load_trading_days
from trading_team.conclusion_block import (
    backfill_accuracy,
    enrich_plan,
    extract_block,
)
from trading_team.schemas import parse_entry, parse_pct, validate_plan
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.risk_contract import (RESERVED_PROPOSAL_FIELDS, apply_risk_reviews as _apply_risk_reviews,
                                        build_reviewed_plan, validate_risk_document)
from trading_team.paper_pipeline import COHORT_FILE, active_paper_cohort, dual_paper_snapshot, paper_db_paths
from trading_team.multica_adapter import verify_execution_context
from trading_team.run_ledger import RunLedger
from trading_team.market_archive import verify_receipt
from trading_team.state_lock import state_lock

TEAM = Path(__file__).resolve().parent
ROOT = TEAM.parent
GENERATION_SCHEMA = "daily_research.v4"
ROLE_NAMES = {
    "01": "fundamental",
    "02": "sentiment",
    "03": "news",
    "04": "technical",
    "05": "researcher",
    "06": "trader",
    "07": "risk_manager",
}
PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def _sha256(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _response_hash(response: dict) -> str:
    return _sha256(json.dumps(response, ensure_ascii=False, sort_keys=True))


def _validate_endpoint(value: str) -> str:
    try:
        url = urlsplit(value)
        if (not isinstance(value, str) or any(c.isspace() for c in value)
                or url.scheme not in ("https", "http") or not url.hostname
                or url.username is not None or url.password is not None or "?" in value or "#" in value):
            raise ValueError()
        _ = url.port
    except (TypeError, ValueError, AttributeError):
        raise ValueError("model transport endpoint must be an HTTP URL without credentials, query or fragment") from None
    return value.rstrip("/")


def _validate_transport(value: dict, model: str | None = None) -> dict:
    keys = {"schema", "adapter", "endpoint", "model", "parameters"}
    if (not isinstance(value, dict) or set(value) != keys or value["schema"] != "model_transport.v1"
            or any(not isinstance(value[k], str) or not re.fullmatch(r"[\w./:-]{1,256}", value[k])
                   for k in ("adapter", "model")) or (model is not None and value["model"] != model)):
        raise ValueError("generation transport identity missing or invalid")
    if _validate_endpoint(value["endpoint"]) != value["endpoint"]:
        raise ValueError("generation transport endpoint is not canonical")
    params = value["parameters"]
    if (not isinstance(params, dict) or set(params) != {"temperature", "max_tokens", "thinking", "response_format"}
            or type(params["max_tokens"]) is not int or params["max_tokens"] <= 0
            or type(params["temperature"]) not in (int, float) or not math.isfinite(params["temperature"])
            or not 0 <= params["temperature"] <= 2 or params["thinking"] not in ("disabled", "enabled")
            or params["response_format"] != "json_object"):
        raise ValueError("generation transport parameters invalid")
    return json.loads(json.dumps(value, allow_nan=False))


def _verify_role_transports(directory: Path, transport: dict) -> None:
    for number, role in ROLE_NAMES.items():
        meta = json.loads((directory / f"{number}_{role}.run.json").read_text(encoding="utf-8"))
        if (meta.get("model") != transport["model"] or meta.get("transport") != transport
                or not isinstance(meta.get("request"), dict) or meta["request"].get("transport") != transport):
            raise ValueError("generation role/request transport does not match publication")


def _validate_manifest(manifest: dict, day: str, team: Path | None = None) -> None:
    if manifest.get("date") != day:
        raise ValueError(f"manifest 日期与 {day} 不一致")
    if manifest.get("ok") is not True:
        raise ValueError("manifest 未通过数据采集闸门")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("manifest 缺数据源明细")
    failed = sorted(
        name for name, source in sources.items()
        if not isinstance(source, dict) or source.get("ok") is not True
    )
    if failed:
        raise ValueError(f"manifest 存在失败数据源: {', '.join(failed)}")
    if not manifest.get("latest_trade_date"):
        raise ValueError("manifest 缺 latest_trade_date")
    receipt = manifest.get("execution_archive")
    if (not isinstance(receipt, dict) or sources.get("execution_archive", {}).get("ok") is not True
            or receipt.get("date") != manifest["latest_trade_date"]):
        raise ValueError("manifest 缺少匹配的原始行情 archive 凭据")
    if team is not None:
        document = verify_receipt(team, receipt)
        watchlist = json.loads((team / "watchlist.json").read_text(encoding="utf-8"))
        expected = {stock["code"] for stock in watchlist["stocks"]} | {"000300"}
        if not expected <= document["bars"].keys():
            raise ValueError("archive coverage does not include the research universe")
        artifact = team / "context" / day / "execution_archive.json"
        if not artifact.exists() or json.loads(artifact.read_bytes()) != receipt:
            raise ValueError("archive context artifact does not match manifest")


def _require_previous_session(data_asof: str, day: str) -> None:
    previous = max(
        (session for session in load_trading_days(DEFAULT_TRADING_CALENDAR) if session < day),
        default=None,
    )
    if data_asof != previous:
        raise ValueError(
            f"Multica publication requires previous completed session {previous}; "
            f"manifest has {data_asof}"
        )


def generation_inputs(team: Path, day: str) -> dict:
    paths = [team / "watchlist.json"]
    paths.extend(sorted((team / "context" / day).glob("*.json")))
    paths.extend(team / "prompts" / f"{number}_{role}.md"
                 for number, role in ROLE_NAMES.items())
    inputs = {str(path.relative_to(team)): _sha256(path.read_text(encoding="utf-8"))
              for path in paths}
    if active_paper_cohort(team):
        inputs[COHORT_FILE] = _sha256((team.parent / COHORT_FILE).read_text(encoding="utf-8"))
    return inputs


def generation_outputs(directory: Path) -> dict:
    names = ["daily_conclusion.md"]
    for number, role in ROLE_NAMES.items():
        names.extend(f"{number}_{role}{suffix}"
                     for suffix in (".md", ".run.json", ".input.txt"))
    return {name: _sha256((directory / name).read_text(encoding="utf-8")) for name in names}


def _validate_fundamental_period_claims(report: str) -> None:
    for sentence in re.split(r"[。；;\n]", report):
        annual = re.search(r"全年|(?<!半)年报", sentence)
        shorter = re.search(r"H[12]|Q[124]|半年|一季|二季|四季", sentence, re.I)
        nine_month = re.search(r"Q3|三季|前三季", sentence, re.I)
        mixed = (annual and (shorter or nine_month)) or (nine_month and shorter)
        inference = re.search(r"相比|对比|比较|较|延续|反转|加速|回升|走弱|抬升|升至|走低|转弱|转强|连续|改善|恶化", sentence)
        if mixed and inference:
            raise ValueError("fundamental period comparison: mixed reporting periods cannot support a directional claim")


def _validate_unverified_fund_flow(report: str) -> None:
    flow = re.compile(
        r"(?:主力|北向|外资|增量)?资金(?:真实|确实|明显|持续|净)*(?:流入|流出|介入|进场|撤出)"
        r"|资金.{0,24}(?:向|往).{0,24}集中"
    )
    volume_causality = re.compile(
        r"(?:上涨|走强|上行).{0,20}由.{0,20}(?:换手|放量|量比|成交量|成交额).{0,10}(?:推动|驱动|带动|导致)"
        r"|(?:换手|放量|量比|成交量|成交额).{0,16}(?:推动|驱动|带动|导致).{0,8}(?:股价|价格|指数)?.{0,3}(?:上涨|走强|上行)"
    )
    bounded_before = ("可能", "疑似", "或许", "推测", "待核验", "未证实", "不可判", "不可判定",
                      "无法证明", "不能证明", "不代表", "不能推出", "数据缺失")
    bounded_after = ("迹象", "的迹象", *bounded_before)
    causal_bounded = re.compile(r"可能|疑似|或许|推测|待核验|未证实|假设|并非|不是|不代表|不能证明|无法证明|不能推出|不得升级为|未升级为")
    negated_prefix = re.compile(
        r"(?:(?:未|不)(?:将|把).{0,64}升级为|未升级为|(?:不(?:得|应|可|能)?|不可)(?:断言|认定|证明|推断|推出|升级为(?:已证实的)?))[「“]?$"
    )
    for clause in re.split(r"[，,。；;：:\n]|但", report):
        for match in flow.finditer(clause):
            before, after = clause[:match.start()], clause[match.end():]
            negated = ((before.endswith("不构成") and after.startswith("证据"))
                       or negated_prefix.search(before))
            bounded_inside = match.group().startswith(("资金可能", "资金疑似", "资金或许", "资金推测"))
            paired_flow_unknown = re.match(r"^(?:或|/)(?:流入|流出)方向不可判", after)
            if not negated and not (before.endswith(bounded_before) or bounded_inside
                                    or after.startswith(bounded_after) or paired_flow_unknown):
                raise ValueError("unverified fund flow: volume and turnover do not prove signed capital flow")
        for match in volume_causality.finditer(clause):
            if not (causal_bounded.search(clause[max(0, match.start() - 6):match.end()])
                    or negated_prefix.search(clause[:match.start()])):
                raise ValueError("unverified volume causality: volume and turnover do not prove a price driver")


def _validate_researcher_report(report: str) -> None:
    _validate_unverified_fund_flow(report)
    if re.search(r"(?<!并非)(?<!不是)无反向信号", report):
        raise ValueError("researcher counter-signal contradiction: absolute denial needs evidence reconciliation")


def inspect_generation(team: Path, day: str, ledger: RunLedger,
                       expected_model: str | None = None, expected_transport: dict | None = None) -> dict:
    """Verify publication content without acknowledging or repairing its ledger step."""
    manifest = json.loads((team / "context" / day / "manifest.json").read_text(encoding="utf-8"))
    _validate_manifest(manifest, day, team)
    output = team / "outputs" / day
    receipt_path = output / "generation.json"
    if not receipt_path.exists():
        raise ValueError("generation evidence missing; legacy report cannot certify a new run")
    text = receipt_path.read_text(encoding="utf-8")
    receipt = json.loads(text)
    publisher = ledger.latest_step(receipt.get("run_id", ""), "publish_generation")
    if not publisher or publisher["input_hash"] != _sha256(text):
        raise ValueError("generation receipt does not match its ledger record")
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    if (
        receipt.get("schema") != GENERATION_SCHEMA
        or receipt.get("date") != day
        or not receipt.get("model")
        or (expected_model is not None and receipt.get("model") != expected_model)
        or receipt.get("inputs") != generation_inputs(team, day)
        or receipt.get("outputs") != generation_outputs(output)
        or receipt.get("plan_hash") != canonical_plan_hash(plan)
        or plan.get("date") != day
        or plan.get("data_asof") != manifest["latest_trade_date"]
    ):
        raise ValueError("generation input, output, model or plan version changed; regenerate explicitly")
    if not validate_risk_document(plan):
        raise ValueError("generation risk contract missing; regenerate explicitly")
    _validate_fundamental_period_claims((output / "01_fundamental.md").read_text(encoding="utf-8"))
    _validate_unverified_fund_flow((output / "02_sentiment.md").read_text(encoding="utf-8"))
    _validate_researcher_report((output / "05_researcher.md").read_text(encoding="utf-8"))
    transport = _validate_transport(receipt.get("transport"), receipt["model"])
    if expected_transport is not None and transport != expected_transport:
        raise ValueError("generation transport changed; regenerate explicitly")
    _verify_role_transports(output, transport)
    return {"data_asof": manifest["latest_trade_date"], "plan_hash": receipt["plan_hash"],
            "publisher": publisher, "receipt_hash": _sha256(text), "transport": transport}


def load_local_env(path: Path = ROOT / ".env") -> None:
    """Load missing environment variables from the git-ignored local env file."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def direct_network_env() -> dict[str, str]:
    """Return an environment without inherited desktop proxy settings."""
    env = os.environ.copy()
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


class KimiClient:
    def __init__(self, api_key: str, base_url: str, model: str = "kimi-k3"):
        if not api_key:
            raise ValueError("MOONSHOT_API_KEY 未配置")
        self.api_key = api_key
        self.base_url = _validate_endpoint(base_url)
        self.model = model
        self.session = requests.Session()
        self.session.trust_env = False
        self.last_meta: dict = {}

    @property
    def transport_identity(self) -> dict:
        return _validate_transport({"schema": "model_transport.v1", "adapter": "openai_chat_completions.v1",
            "endpoint": self.base_url, "model": self.model,
            "parameters": {"response_format": "json_object", "thinking": "disabled",
                           "temperature": 0.6, "max_tokens": 8192}})

    def complete(self, system: str, user: str, max_tokens: int = 8192) -> dict:
        self.last_meta = {}
        identity = self.transport_identity
        identity["parameters"]["max_tokens"] = max_tokens
        identity = _validate_transport(identity)
        params = identity["parameters"]
        payload = {
            "model": identity["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": params["response_format"]},
            "thinking": {"type": params["thinking"]},
            "temperature": params["temperature"],
            "max_tokens": params["max_tokens"],
        }
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.post(
                    f"{identity['endpoint']}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=(20, 900),
                    allow_redirects=False,
                )
                if response.status_code != 200:
                    raise requests.HTTPError(
                        f"Model API HTTP {response.status_code}",
                        response=response,
                    )
                response.raise_for_status()
                body = response.json()
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise RuntimeError("Kimi 输出达到 max_tokens，拒绝保存截断报告")
                self.last_meta = {
                    "transport": identity,
                    "reported_model": body.get("model") if isinstance(body.get("model"), str) else None,
                    "request_id": body.get("id"),
                    "usage": body.get("usage") or {},
                    "finish_reason": choice.get("finish_reason"),
                }
                return json.loads(choice["message"]["content"])
            except (requests.RequestException, KeyError, json.JSONDecodeError,
                    RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"Kimi API 连续失败: {last_error}") from last_error


def _bundle(paths: list[Path]) -> str:
    chunks = []
    for path in paths:
        if path.exists():
            chunks.append(f"\n===== {path.name} =====\n{path.read_text(encoding='utf-8')}")
    if not chunks:
        raise FileNotFoundError("角色输入文件为空")
    return "".join(chunks)


def _fundamental_period_index(paths: list[Path]) -> str:
    index = {}
    for path in paths:
        reports = json.loads(path.read_text(encoding="utf-8")).get("financials", {}).get("reports", [])
        dates = sorted({date.fromisoformat(row["report_date"]).isoformat() for row in reports}, reverse=True)
        missing = {}
        for report_date in dates:
            current = date.fromisoformat(report_date)
            previous = current.replace(year=current.year - 1).isoformat()
            if previous not in dates:
                missing[report_date] = previous
        index[path.stem.removeprefix("fundamental_")] = {
            "archived_report_dates": dates,
            "missing_prior_year_same_period": missing,
        }
    return "\n===== 报告期覆盖索引 =====\n" + json.dumps(index, ensure_ascii=False, indent=2)


def _latest_fundamental_reports(paths: list[Path]) -> str:
    chunks = []
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        financials = document.get("financials")
        if isinstance(financials, dict) and financials.get("reports"):
            latest = max(financials["reports"], key=lambda row: date.fromisoformat(row["report_date"]))
            document["financials"] = {**financials, "reports": [latest]}
        chunks.append(f"\n===== {path.name} =====\n{json.dumps(document, ensure_ascii=False)}")
    return "".join(chunks)


def _market_gate(context_dir: Path) -> dict:
    """Return the current strategy's deterministic CSI300 MA60 market gate."""
    technical = json.loads(
        (context_dir / "technical_000300.json").read_text(encoding="utf-8")
    )
    close = float(technical["close"])
    ma60 = float(technical["ma60"])
    return {"market_ok": close > ma60, "close": close, "ma60": ma60}


def _normalize_proposals(proposals: object, expected_codes: set[str]) -> list[dict]:
    if not isinstance(proposals, list):
        raise ValueError("交易员 proposals 必须是数组")
    normalized = []
    seen = set()
    for raw in proposals:
        if not isinstance(raw, dict):
            raise ValueError("proposals 中存在非对象条目")
        if RESERVED_PROPOSAL_FIELDS & raw.keys():
            raise ValueError("risk contract: trader cannot author risk or approval fields")
        plan = dict(raw)
        plan["code"] = str(plan.get("code", ""))
        plan["position_pct"] = parse_pct(plan.get("position_pct"))
        plan["entry"] = parse_entry(plan.get("entry"))
        problems = validate_plan(plan, strict=True)
        if problems:
            raise ValueError(f"{plan.get('code')}: {'; '.join(problems)}")
        if plan["code"] in seen:
            raise ValueError(f"重复提案: {plan['code']}")
        seen.add(plan["code"])
        normalized.append(plan)
    if seen != expected_codes:
        raise ValueError(
            f"提案未完整覆盖观察池: missing={sorted(expected_codes - seen)}, "
            f"extra={sorted(seen - expected_codes)}"
        )
    return normalized


class DailyMeetingRunner:
    def __init__(self, client: KimiClient, team: Path = TEAM,
                 ledger: RunLedger | None = None):
        self.client = client
        self.team = team
        self.root = team.parent
        self.ledger = ledger or RunLedger(team / ".runs" / "ledger.db")
        self._active_transport: dict | None = None

    def _transport(self) -> dict:
        identity = _validate_transport(getattr(self.client, "transport_identity", None), self.client.model)
        if self._active_transport is not None and identity != self._active_transport:
            raise ValueError("generation transport changed during research")
        return identity

    def _status(self, day: str, **fields) -> None:
        path = self.team / ".runs" / f"{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = {}
        current.update(fields, updated_at=datetime.now().isoformat(timespec="seconds"))
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    def _collect(self, day: str) -> None:
        env = direct_network_env()
        for module in ("trading_team.collect", "trading_team.collect_global"):
            subprocess.run(
                [sys.executable, "-m", module, "--date", day],
                cwd=self.root,
                env=env,
                check=True,
            )

    def _role(self, number: str, day: str, inputs: str, extra: str = "") -> dict:
        charter = (self.team / "prompts" / f"{number}_{ROLE_NAMES[number]}.md")
        system = charter.read_text(encoding="utf-8")
        mode = "非交易日复盘版" if date.fromisoformat(day).weekday() >= 5 else "正常日报"
        user = f"""报告日期：{day}
运行模式：{mode}。非交易日不得伪造当日成交或行情，计划仅面向下一交易日。
你只能依据下方归档输入，不得联网，不得补造缺失数字。
请完成角色报告，并只返回一个合法 JSON 对象：
{{"report_markdown": "完整 Markdown 报告"{extra}}}
report_markdown 必须遵守角色章程，并以章程要求的 ```json 结构化结论块结束。

<archived_inputs>{inputs}</archived_inputs>
"""
        return self.client.complete(system, user)

    def _role_fingerprint(self, number: str, day: str, inputs: str,
                          extra: str = "") -> dict:
        charter = self.team / "prompts" / f"{number}_{ROLE_NAMES[number]}.md"
        prompt_hash = _sha256(GENERATION_SCHEMA, charter.read_text(encoding="utf-8"), extra)
        return {
            "date": day,
            "role": ROLE_NAMES[number],
            "model": self.client.model,
            "transport": self._transport(),
            "input_hash": _sha256(inputs),
            "prompt_hash": prompt_hash,
        }

    def _cached_role(self, stage: Path, number: str, day: str,
                     fingerprint: dict) -> dict | None:
        path = stage / f"{number}_{ROLE_NAMES[number]}.md"
        meta_path = stage / f"{number}_{ROLE_NAMES[number]}.run.json"
        if not path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if any(meta.get(key) != value for key, value in fingerprint.items()):
            return None
        if not isinstance(meta.get("request"), dict) or meta["request"].get("transport") != fingerprint["transport"]:
            return None
        report = path.read_text(encoding="utf-8")
        response = meta.get("response") or {}
        if (meta.get("output_hash") != _sha256(report)
                or meta.get("response_hash") != _response_hash(response)):
            return None
        input_path = stage / f"{number}_{ROLE_NAMES[number]}.input.txt"
        if not input_path.exists() or _sha256(input_path.read_text(encoding="utf-8")) != fingerprint["input_hash"]:
            return None
        block = extract_block(report, ROLE_NAMES[number])
        if block and block.get("date") == day:
            result = dict(response)
            result["report_markdown"] = report
            return result
        return None

    def _run_role(self, run_id: str, stage: Path, number: str, day: str,
                  inputs: str, extra: str = "", *, validate_result=None) -> dict:
        fingerprint = self._role_fingerprint(number, day, inputs, extra)
        step_id = self.ledger.start_step(
            run_id,
            f"role_{number}",
            role=ROLE_NAMES[number],
            input_hash=fingerprint["input_hash"],
            prompt_hash=fingerprint["prompt_hash"],
            model=self.client.model,
            metadata={"transport": fingerprint["transport"]},
        )
        result = None
        request = {}
        rejected_path = None
        try:
            cached = self._cached_role(stage, number, day, fingerprint)
            if cached is not None:
                if validate_result is not None:
                    validate_result(cached)
                self.ledger.finish_step(
                    step_id,
                    status="skipped",
                    output_hash=_sha256(cached["report_markdown"]),
                    artifact_path=f"trading_team/outputs/{day}/{number}_{ROLE_NAMES[number]}.md",
                    metadata={"cache_hit": True, "transport": fingerprint["transport"]},
                )
                return cached

            result = self._role(number, day, inputs, extra)
            request = dict(getattr(self.client, "last_meta", {}) or {})
            if self._transport() != fingerprint["transport"] or request.get("transport") != fingerprint["transport"]:
                raise ValueError("generation actual request transport does not match role")
            report = result.get("report_markdown")
            if not isinstance(report, str) or not report.strip():
                raise ValueError(f"角色 {number} 缺 report_markdown")
            block = extract_block(report, ROLE_NAMES[number])
            if block is None:
                raise ValueError(f"角色 {number} 结构化结论块缺失或非法")
            if block.get("date") != day:
                raise ValueError(f"角色 {number} 结论日期与 {day} 不一致")
            if validate_result is not None:
                validate_result(result)
            report_path = stage / f"{number}_{ROLE_NAMES[number]}.md"
            report_path.write_text(report, encoding="utf-8")
            response = {key: value for key, value in result.items() if key != "report_markdown"}
            (stage / f"{number}_{ROLE_NAMES[number]}.input.txt").write_text(inputs, encoding="utf-8")
            run_meta = {
                **fingerprint,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "request": request,
                "output_hash": _sha256(report),
                "response_hash": _response_hash(response),
                "response": response,
            }
            (stage / f"{number}_{ROLE_NAMES[number]}.run.json").write_text(
                json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.ledger.finish_step(
                step_id,
                output_hash=_sha256(report),
                artifact_path=f"trading_team/outputs/{day}/{report_path.name}",
                metadata=run_meta["request"],
            )
            return result
        except Exception as exc:
            # Retain decoded model replies even when the first format check rejects them.
            if result is not None:
                rejected_path = self.team / ".runs" / "rejected" / run_id / f"{step_id}.json"
                rejected_path.parent.mkdir(parents=True, exist_ok=True)
                rejected_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                rejected_path.with_suffix(".input.txt").write_text(inputs, encoding="utf-8")
                rejected_path.with_suffix(".run.json").write_text(json.dumps({
                    **fingerprint, "request": request, "response_hash": _response_hash(result),
                    "error": f"{type(exc).__name__}: {exc}",
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            self.ledger.finish_step(
                step_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                artifact_path=str(rejected_path.relative_to(self.root)) if rejected_path else None,
                metadata={"transport": fingerprint["transport"], "request": request},
            )
            raise

    def _run_system_step(self, run_id: str, name: str, callback):
        # These callbacks read mutable state. Finalize and outbox own versioned deduplication.
        step_id = self.ledger.start_step(run_id, name)
        try:
            result = callback()
            self.ledger.finish_step(
                step_id,
                output_hash=_sha256(json.dumps(
                    result, ensure_ascii=False, sort_keys=True, default=str
                )),
                metadata={"result": result},
            )
            return result
        except Exception as exc:
            self.ledger.finish_step(
                step_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    def _generation_inputs(self, day: str) -> dict:
        return generation_inputs(self.team, day)

    def _generation_outputs(self, directory: Path) -> dict:
        return generation_outputs(directory)

    def _archive(self, directory: Path, day: str, run_id: str, name: str) -> None:
        if directory.exists():
            destination = self.team / ".runs" / "archive" / day / run_id / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            directory.rename(destination)

    def _publish_generation(self, run_id: str, day: str, stage: Path,
                            inputs: dict, plan: dict) -> None:
        with state_lock(self.team):
            self._publish_generation_locked(run_id, day, stage, inputs, plan)

    def _publish_generation_locked(self, run_id: str, day: str, stage: Path,
                                   inputs: dict, plan: dict) -> None:
        if not validate_risk_document(plan):
            raise ValueError("generation risk contract missing")
        if inputs != self._generation_inputs(day):
            raise ValueError("generation inputs changed during model execution")
        transport = self._transport()
        _verify_role_transports(stage, transport)
        receipt = {
            "schema": GENERATION_SCHEMA, "date": day, "run_id": run_id,
            "model": self.client.model, "transport": transport, "inputs": inputs,
            "outputs": self._generation_outputs(stage),
            "plan_hash": canonical_plan_hash(plan),
        }
        text = json.dumps(receipt, ensure_ascii=False, indent=2)
        (stage / "generation.json").write_text(text, encoding="utf-8")
        receipt_hash = _sha256(text)
        step_id = self.ledger.start_step(run_id, "publish_generation", input_hash=receipt_hash)
        try:
            output = self.team / "outputs" / day
            output.parent.mkdir(parents=True, exist_ok=True)
            self._archive(output, day, run_id, "outputs")
            stage.rename(output)
            self.ledger.finish_step(
                step_id, output_hash=receipt_hash,
                artifact_path=str((output / "generation.json").relative_to(self.root)),
            )
        except Exception as exc:
            self.ledger.finish_step(step_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            raise

    def _validate_generation(self, day: str) -> dict:
        evidence = inspect_generation(self.team, day, self.ledger, self.client.model, self._transport())
        publisher = evidence["publisher"]
        if publisher["status"] != "succeeded":
            # Recovery after atomic directory rename but before the ledger acknowledgement.
            self.ledger.finish_step(
                publisher["id"], output_hash=evidence["receipt_hash"],
                artifact_path=str((self.team / "outputs" / day / "generation.json").relative_to(self.root)),
                metadata={"recovered_after_publish": True},
            )
        return {key: evidence[key] for key in ("data_asof", "plan_hash")}

    def _publication_handoff(self, run_id: str, day: str) -> dict:
        evidence = self._run_system_step(run_id, "publication_handoff", lambda: self._validate_generation(day))
        from trading_team.build_index import build_index
        try:
            index = self._run_system_step(run_id, "build_index", lambda: {"days": len(build_index(self.team))})
        except Exception:
            index = {"status": "failed"}
        return {"scope": "publication_only", "business_complete": False,
                "plan_hash": evidence["plan_hash"], "reviews": {"status": "not_checked"},
                "finalize": {"status": "not_requested"}, "notification": {"status": "disabled"},
                "index": index}

    def _reuse_generation(self, run_id: str, day: str, push: bool, publish_only: bool = False,
                          external_context: dict | None = None) -> dict:
        if external_context:
            publication = inspect_generation(self.team, day, self.ledger, self.client.model, self._transport())
            _require_previous_session(publication["data_asof"], day)
            publisher = publication["publisher"]
            original = self.ledger.get_run(publisher["run_id"])
            attempt = next((item for item in original["attempts"]
                            if item["attempt"] == publisher["workflow_attempt"]), None) if original else None
            identity = attempt["external_context"] if attempt else {}
            if (identity.get("provider") != "multica"
                    or identity.get("verification") != "remote_verified"
                    or identity.get("role") != "Research Lead"
                    or not identity.get("issue_id") or not identity.get("task_id")):
                raise ValueError("existing generation lacks a verified Multica publisher")
        evidence = self._run_system_step(
            run_id, "existing_artifacts", lambda: self._validate_generation(day)
        )
        postprocess = (self._publication_handoff(run_id, day) if publish_only
                       else self._postprocess(run_id, day, push))
        if not publish_only:
            decision = json.loads(
                (self.team / "decisions" / day / "decision.json").read_text(encoding="utf-8")
            )
            if decision.get("plan_hash") != evidence["plan_hash"]:
                raise ValueError("materialized decision does not match generation plan_hash")
        result = {"date": day, "status": "succeeded", "run_id": run_id,
                  "transport": self._active_transport,
                  "reason": "publication verified; downstream not requested" if publish_only else "日报生成凭据已校验，收尾已重检",
                  **evidence, **postprocess}
        self.ledger.finish_run(run_id, status="succeeded", metadata=result, **evidence)
        self._status(day, **result, step="completed", error=None,
                     completed_at=datetime.now().isoformat(timespec="seconds"))
        return result

    def _verify_materialization(self, day: str) -> dict:
        plan = json.loads((self.team / "outputs" / day / "plan.json").read_text(encoding="utf-8"))
        expected = canonical_plan_hash(plan)
        for name in ("decision.json", "paper_decision.json"):
            decision = json.loads((self.team / "decisions" / day / name).read_text(encoding="utf-8"))
            if decision.get("date") != day or decision.get("plan_hash") != expected:
                raise ValueError(f"{name} date/plan_hash does not match current plan")
        return {"plan_hash": expected}

    def _postprocess(self, run_id: str, day: str, push: bool) -> dict:
        with state_lock(self.team):
            return self._postprocess_locked(run_id, day, push)

    def _postprocess_locked(self, run_id: str, day: str, push: bool) -> dict:
        from trading_team.build_index import build_index
        from trading_team.daily_finalize import finalize_available_tracked
        from trading_team.paper_pipeline import record_current_valuations

        def finalize_scope():
            results = finalize_available_tracked(
                team=self.team,
                trigger_source=f"daily_research:{run_id}",
                ledger=self.ledger,
                focus_day=day,
            )
            failures = [item for item in results if item.get("status") == "failed"]
            if failures:
                failed_days = ", ".join(item["date"] for item in failures)
                raise RuntimeError(f"daily_finalize failed: {failed_days}")
            return results

        enrich = self._run_system_step(
            run_id, "enrich", lambda: enrich_plan(day, self.team)
        )
        backfill = self._run_system_step(
            run_id, "backfill", lambda: backfill_accuracy(day, self.team)
        )
        finalized = self._run_system_step(
            run_id,
            "finalize_available",
            finalize_scope,
        )
        materialization = self._run_system_step(
            run_id, "verify_materialization", lambda: self._verify_materialization(day)
        )
        paper_valuation = self._run_system_step(
            run_id,
            "paper_valuation",
            lambda: record_current_valuations(
                paper_db_paths(self.team),
                team=self.team,
                as_of_day=day,
            ),
        )
        index = self._run_system_step(
            run_id,
            "build_index",
            lambda: {"days": len(build_index(self.team))},
        )
        notification = {"status": "disabled"}
        if push:
            def send_once():
                from trading_team.push_feishu import push as send_feishu
                inherited = {key: os.environ[key] for key in PROXY_ENV_KEYS if key in os.environ}
                for key in PROXY_ENV_KEYS:
                    os.environ.pop(key, None)
                try:
                    return send_feishu(day, team=self.team)
                finally:
                    os.environ.update(inherited)

            notification = self._run_system_step(run_id, "push", send_once)
        return {
            "enrich": enrich,
            "backfill": backfill,
            "finalize": finalized,
            "materialization": materialization,
            "paper_valuation": paper_valuation,
            "index": index,
            "notification": notification,
        }

    def run(self, day: str, collect: bool = True, force: bool = False,
            push: bool = True, trigger_source: str = "manual",
            multica_issue_id: str | None = None, publish_only: bool = False) -> dict:
        from pipeline.policy import require
        require("llm_meeting")
        date.fromisoformat(day)
        lock_path = self.team / ".runs" / "locks" / f"daily_research-{day}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"daily_research {day} already running") from exc
            external_context = {}
            multica_identity_present = any(os.environ.get(key) for key in (
                "MULTICA_TASK_ID", "MULTICA_AGENT_ID", "MULTICA_WORKSPACE_ID"))
            if multica_issue_id or multica_identity_present or trigger_source.startswith("multica"):
                try:
                    external_context = verify_execution_context(day, multica_issue_id)
                    if external_context.get("role") != "Research Lead":
                        raise ValueError("Multica daily publication requires the Research Lead agent")
                    trigger_source = f"multica:{external_context['issue_id']}:{external_context['task_id']}"
                except Exception as exc:
                    run_id = self.ledger.start_run(
                        "daily_research", day,
                        trigger_source="multica_rejected" if external_context else "multica_unverified",
                        external_context=external_context, resume=False,
                    )
                    step_id = self.ledger.start_step(run_id, "verify_multica_context")
                    error = f"{type(exc).__name__}: {exc}"
                    self.ledger.finish_step(step_id, status="failed", error=error)
                    self.ledger.finish_run(run_id, status="failed", error=error)
                    raise
            if external_context and (not publish_only or push or force):
                run_id = self.ledger.start_run(
                    "daily_research", day, trigger_source=trigger_source,
                    external_context=external_context, resume=False,
                )
                step_id = self.ledger.start_step(run_id, "publication_scope_gate")
                message = "Multica publication-only scope requires publish_only=True, push=False, force=False"
                error = f"ValueError: {message}"
                self.ledger.finish_step(step_id, status="failed", error=error)
                self.ledger.finish_run(run_id, status="failed", error=error)
                raise ValueError(message)
            if publish_only and push:
                raise ValueError("publish_only requires push=False")
            if external_context:
                try:
                    if day not in load_trading_days(DEFAULT_TRADING_CALENDAR):
                        raise ValueError(f"Multica publication requires an A-share trading session: {day}")
                except (OSError, ValueError, EOFError) as exc:
                    run_id = self.ledger.start_run(
                        "daily_research", day, trigger_source=trigger_source,
                        external_context=external_context, resume=False,
                    )
                    step_id = self.ledger.start_step(run_id, "trading_session_gate")
                    error = f"{type(exc).__name__}: {exc}"
                    self.ledger.finish_step(step_id, status="failed", error=error)
                    self.ledger.finish_run(run_id, status="failed", error=error)
                    raise
            return self._run_locked(day, collect, force, push, trigger_source, external_context, publish_only)

    def _run_locked(self, day: str, collect: bool, force: bool,
                    push: bool, trigger_source: str, external_context: dict, publish_only: bool = False) -> dict:
        run_id = self.ledger.start_run(
            "daily_research",
            day,
            trigger_source=trigger_source,
            model=self.client.model,
            metadata={"collect": collect, "push": push, "force": force, "publish_only": publish_only},
            external_context=external_context,
            resume=not force,
        )
        output_dir = self.team / "outputs" / day
        try:
            self._active_transport = None
            self._active_transport = self._transport()
            self._status(
                day, status="running", model=self.client.model, transport=self._active_transport, error=None,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            if (output_dir / "daily_conclusion.md").exists() and not force:
                return self._reuse_generation(run_id, day, push, publish_only, external_context)

            stage = self.team / ".runs" / "staging" / day
            if force:
                self._archive(stage, day, run_id, "staging")
            stage.mkdir(parents=True, exist_ok=True)
            for meta_path in stage.glob("0[1-7]_*.run.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    raise ValueError("staged transport evidence invalid; archive with explicit force") from None
                if not isinstance(meta, dict) or meta.get("transport") != self._active_transport:
                    raise ValueError("staged transport changed; archive with explicit force")
            if collect:
                self._status(day, step="collect")
                step_id = self.ledger.start_step(run_id, "collect")
                try:
                    self._collect(day)
                    self.ledger.finish_step(step_id)
                except Exception as exc:
                    self.ledger.finish_step(
                        step_id, status="failed", error=f"{type(exc).__name__}: {exc}"
                    )
                    raise
            context = self.team / "context" / day
            manifest = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
            _validate_manifest(manifest, day, self.team)
            if external_context:
                _require_previous_session(manifest["latest_trade_date"], day)
            generation_inputs = self._generation_inputs(day)
            gate = _market_gate(context)
            market_ok = gate["market_ok"]
            watchlist = json.loads((self.team / "watchlist.json").read_text(encoding="utf-8"))
            codes = {str(item["code"]) for item in watchlist["stocks"]}

            fundamental_files = sorted(context.glob("fundamental_*.json"))
            role_inputs = {
                "01": [context / "manifest.json", context / "snapshot.json"],
                "02": [context / "manifest.json", context / "sentiment.json", context / "global_macro.json"],
                "03": [context / "manifest.json", context / "news.json", context / "global_macro.json"],
                "04": [context / "manifest.json", *sorted(context.glob("technical_*.json"))],
            }
            reports = {}
            for number in ("01", "02", "03", "04"):
                self._status(day, step=f"role_{number}")
                if number == "01":
                    inputs = (_bundle(role_inputs[number])
                              + _latest_fundamental_reports(fundamental_files)
                              + _fundamental_period_index(fundamental_files))
                else:
                    inputs = _bundle(role_inputs[number])
                validator = {"01": _validate_fundamental_period_claims,
                             "02": _validate_unverified_fund_flow}.get(number)
                result = self._run_role(
                    run_id, stage, number, day, inputs,
                    validate_result=(lambda response: validator(response["report_markdown"]))
                    if validator else None,
                )
                reports[number] = result["report_markdown"]

            self._status(day, step="role_05")
            result = self._run_role(
                run_id, stage, "05", day, _bundle(sorted(stage.glob("0[1-4]_*.md"))),
                validate_result=lambda response: _validate_researcher_report(response["report_markdown"]),
            )
            reports["05"] = result["report_markdown"]

            self._status(day, step="role_06")
            trader_extra = f', "market_ok": {str(market_ok).lower()}, "proposals": ["完整覆盖观察池的结构化候选计划"]'
            expected = ", ".join(sorted(codes))
            trader_account_state = dual_paper_snapshot(paper_db_paths(self.team), team=self.team, as_of_day=day)
            # Observation time is not an account fact; it must not invalidate recovery caches.
            trader_account_state.pop("generated_at", None)
            trader_inputs = (
                f"\n===== 可交易代码白名单 =====\n{expected}\n"
                "基准指数 000300 仅用于市场闸门，严禁放入 proposals。\n"
                f"确定性市场闸门：沪深300 close={gate['close']:.2f}，"
                f"MA60={gate['ma60']:.2f}，market_ok={str(market_ok).lower()}。"
                "闸门恢复必须以 close > MA60 为准，不得改写为 MA20 或其他阈值。\n"
                + "\n===== Paper 账户、批次与跨日保护版本 =====\n"
                + json.dumps(trader_account_state, ensure_ascii=False, indent=2)
                + "\n不得猜测持仓或保护单版本；回执投影不等于原计划授权认证。\n"
                + _bundle(sorted(stage.glob("0[1-5]_*.md")))
            )
            result = self._run_role(
                run_id, stage, "06", day, trader_inputs, trader_extra
            )
            if result.get("market_ok") is not market_ok:
                raise ValueError("交易员返回的 market_ok 与确定性市场闸门不一致")
            proposals = _normalize_proposals(result.get("proposals"), codes)
            trader_proposals = [dict(plan) for plan in proposals]
            reports["06"] = result["report_markdown"]

            self._status(day, step="role_07")
            risk_inputs = _bundle([
                stage / "02_sentiment.md", stage / "04_technical.md",
                stage / "06_trader.md", context / "global_macro.json",
                self.team / "loops" / "accuracy.json",
                self.team / "loops" / "approvals.json",
            ])
            paper_state = dual_paper_snapshot(paper_db_paths(self.team), team=self.team, as_of_day=day)
            paper_state.pop("generated_at", None)
            risk_inputs = (
                f"\n===== 确定性市场闸门 =====\n沪深300 close={gate['close']:.2f}，"
                f"MA60={gate['ma60']:.2f}，market_ok={str(market_ok).lower()}。"
                "闸门恢复必须以 close > MA60 为准，不得改写为 MA20 或其他阈值。\n"
                "\n===== Paper 账户、持仓与订单事实 =====\n"
                + json.dumps(paper_state, ensure_ascii=False, indent=2)
                + "\n风险审查必须考虑当前现金、可靠收盘价市值、未实现盈亏、行业集中度、"
                  "T+1 冻结股、订单状态和已有成交；"
                  "不得把 unfilled/blocked 当作持仓。\n"
                + "\n===== 规范化交易员候选（逐笔审核事实源） =====\n"
                + json.dumps(trader_proposals, ensure_ascii=False, indent=2)
                + risk_inputs
            )
            risk_extra = ', "daily_conclusion_markdown": "含 ## 一句话总览 的完整日报总结"'
            def validate_risk_result(response):
                _apply_risk_reviews(
                    trader_proposals, extract_block(response["report_markdown"], "risk_manager")["reviews"],
                    market_ok=market_ok)
                conclusion = response.get("daily_conclusion_markdown")
                if not isinstance(conclusion, str) or "## 一句话总览" not in conclusion:
                    raise ValueError("risk contract: 风险经理未返回合规 daily_conclusion_markdown")

            result = self._run_role(
                run_id, stage, "07", day, risk_inputs, risk_extra,
                validate_result=validate_risk_result,
            )
            risk_report = result["report_markdown"]
            risk_block = extract_block(risk_report, "risk_manager")
            plan = build_reviewed_plan(day, manifest.get("latest_trade_date"), market_ok,
                                       trader_proposals, risk_block["reviews"])
            proposals = plan["proposals"]
            conclusion = result["daily_conclusion_markdown"]
            (stage / "daily_conclusion.md").write_text(conclusion, encoding="utf-8")

            (stage / "plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            self._publish_generation(run_id, day, stage, generation_inputs, plan)

            postprocess = (self._publication_handoff(run_id, day) if publish_only
                           else self._postprocess(run_id, day, push))
            result_summary = {
                "date": day,
                "run_id": run_id,
                "status": "succeeded",
                "model": self.client.model,
                "transport": self._active_transport,
                "data_asof": manifest.get("latest_trade_date"),
                "market_ok": market_ok,
                "proposals": len(proposals),
                **postprocess,
            }
            self._status(
                day, **result_summary, step="completed", error=None,
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            decision_path = self.team / "decisions" / day / "decision.json"
            plan_hash = postprocess.get("plan_hash") if publish_only else None
            if not publish_only and decision_path.exists():
                plan_hash = json.loads(decision_path.read_text(encoding="utf-8")).get("plan_hash")
            self.ledger.finish_run(
                run_id,
                status="succeeded",
                data_asof=manifest.get("latest_trade_date"),
                plan_hash=plan_hash,
                metadata=result_summary,
            )
            return result_summary
        except Exception as exc:
            self._status(day, status="failed", error=f"{type(exc).__name__}: {exc}")
            self.ledger.finish_run(
                run_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                metadata={"transport": self._active_transport},
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--team", type=Path,
                        help="Isolated trading_team directory; requires --no-collect and --no-push, uses inherited credentials only")
    parser.add_argument("--no-collect", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--publish-only", action="store_true",
                        help="Publish verified research for independent review; requires --no-push, skips decisions and settlement")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--multica-issue-id", help="Current issue UUID or key from the Multica task context")
    parser.add_argument(
        "--trigger-source",
        default="manual",
        help="Audit source such as manual, scheduler, or a Multica issue id",
    )
    parser.add_argument("--model", default=os.environ.get("MOONSHOT_MODEL", "kimi-k3"))
    args = parser.parse_args()

    if args.publish_only and not args.no_push:
        parser.error("--publish-only requires --no-push")
    team = TEAM
    if args.team is not None:
        if not args.no_collect or not args.no_push:
            parser.error("--team requires --no-collect and --no-push")
        team = args.team.expanduser().resolve()
        if team.is_relative_to(ROOT.resolve()) or ROOT.resolve().is_relative_to(team.parent):
            parser.error("--team must be outside the production project")
        if team.name != "trading_team" or not team.is_dir():
            parser.error("--team must be an existing trading_team directory")
        for scope in (team, team.parent / "paper"):
            if scope.is_symlink() or any(path.is_symlink() for path in scope.rglob("*")):
                parser.error("isolated team and paper state must not contain symlinks")
    else:
        load_local_env()
    client = KimiClient(
        os.environ.get("MOONSHOT_API_KEY", ""),
        os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1"),
        args.model,
    )
    result = DailyMeetingRunner(client, team=team).run(
        args.date,
        collect=not args.no_collect,
        force=args.force,
        push=not args.no_push,
        trigger_source=args.trigger_source,
        multica_issue_id=args.multica_issue_id,
        publish_only=args.publish_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
