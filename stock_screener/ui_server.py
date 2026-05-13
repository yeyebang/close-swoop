#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local web UI for the stock screener.

The UI intentionally uses the Python standard library for serving so the
project remains easy to run on a machine that already runs the CLI scanner.
"""

from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd

from stock_screener.main import BASE_DIR, CONFIG_FILE, load_config
from stock_screener.paper import paper_report
from stock_screener.research import resolve_data_dir


ROOT_DIR = BASE_DIR.parent
WEB_DIR = BASE_DIR / "web"
REPORT_DIR = BASE_DIR / "reports"
MODEL_DIR = BASE_DIR / "models"
PAPER_LEDGER = BASE_DIR / "paper" / "paper_trades.csv"
SERVER_STATE: dict[str, Any] = {
    "scan_running": False,
    "scan_started_at": None,
    "scan_finished_at": None,
    "scan_error": None,
    "scan_returncode": None,
    "scan_phase": "idle",
    "scan_log": [],
    "backtest_running": False,
    "backtest_started_at": None,
    "backtest_finished_at": None,
    "backtest_error": None,
    "backtest_returncode": None,
    "backtest_phase": "idle",
    "backtest_log": [],
}


def run(host: str = "127.0.0.1", port: int = 3002) -> None:
    server = ThreadingHTTPServer((host, port), UiHandler)
    print(f"Stock Screener UI: http://{host}:{port}")
    server.serve_forever()


class UiHandler(BaseHTTPRequestHandler):
    server_version = "StockScreenerUI/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
                self._send_file(WEB_DIR / "index.html")
            elif path.startswith("/api/"):
                self._handle_api_get(path, query)
            else:
                self._send_file(WEB_DIR / path.lstrip("/"))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def do_HEAD(self) -> None:
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "请求体不是合法 JSON"}, status=400)
            return

        try:
            if parsed.path == "/api/ollama/analyze":
                self._send_json(handle_ollama_analyze(payload))
            elif parsed.path == "/api/scan/run":
                self._send_json(start_scan())
            elif parsed.path == "/api/backtest/run":
                self._send_json(start_backtest(payload))
            else:
                self._send_json({"error": "未知接口"}, status=404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/summary":
            self._send_json(get_summary())
        elif path == "/api/results":
            self._send_json(get_results(query))
        elif path == "/api/scans":
            self._send_json({"files": list_scan_files()})
        elif path == "/api/history":
            code = first(query, "code", "")
            self._send_json(get_history(code))
        elif path == "/api/ollama/models":
            self._send_json(get_ollama_models())
        elif path == "/api/scan/status":
            self._send_json(SERVER_STATE)
        elif path == "/api/backtest/status":
            self._send_json(backtest_state())
        elif path == "/api/backtest/report":
            self._send_json(get_backtest_report(query))
        elif path == "/api/minute-backtest":
            self._send_json(get_minute_backtest())
        else:
            self._send_json({"error": "未知接口"}, status=404)

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(raw)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "文件不存在"}, status=404)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(raw)


def first(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    return values[0] if values else default


def _enrich_with_paper_returns(df: pd.DataFrame) -> pd.DataFrame:
    """将 paper 复盘结果（次日开盘收益）join 到候选股列表。"""
    try:
        ledger = pd.read_csv(PAPER_LEDGER, dtype={"code": str})
        settled = ledger[ledger["status"] == "settled"][["code", "signal_date", "next_return_pct"]].copy()
        if settled.empty:
            return df

        # 标准化 paper ledger 的 code
        settled["code"] = settled["code"].apply(normalize_code)

        # 从候选股的 信号时间/行情采集时间 提取日期
        date_col = next((c for c in ["信号时间", "行情采集时间"] if c in df.columns), None)
        if date_col is None:
            return df
        df = df.copy()
        df["_sig_date"] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        df["_code6"] = df["代码"].apply(normalize_code) if "代码" in df.columns else df.get("code", "").apply(normalize_code)

        # join
        settled = settled.rename(columns={"next_return_pct": "next_return_pct"})
        settled["_key"] = settled["signal_date"].astype(str) + "_" + settled["code"].astype(str)
        df["_key"] = df["_sig_date"].astype(str) + "_" + df["_code6"].astype(str)
        key_map = settled.set_index("_key")["next_return_pct"].to_dict()
        df["next_return_pct"] = df["_key"].map(key_map)
        df.drop(columns=["_sig_date", "_code6", "_key"], inplace=True)
    except Exception:
        pass
    return df


def get_summary() -> dict[str, Any]:
    latest_all = latest_file("results_all_*.csv")
    latest_top = latest_file("results_*.csv", exclude_prefix="results_all_")
    latest_df = read_csv(latest_top or latest_all, limit=20) if (latest_top or latest_all) else pd.DataFrame()
    all_df = read_csv(latest_all, limit=100000) if latest_all else pd.DataFrame()
    config = load_json(CONFIG_FILE)
    metrics = load_json(REPORT_DIR / "minute_backtest_metrics.json")
    backtest_mode = "minute"
    if not metrics:
        metrics = load_json(REPORT_DIR / "backtest_metrics.json")
        backtest_mode = "daily" if metrics else ""
    model_metrics = load_json(MODEL_DIR / "metrics.json")

    paper = paper_report()
    source = pick_first_value(all_df, ["数据源"])
    captured_at = pick_first_value(all_df, ["行情采集时间"])
    score_col = "final_score" if "final_score" in all_df.columns else "score"

    return {
        "latestTopFile": file_info(latest_top),
        "latestAllFile": file_info(latest_all),
        "candidateCount": int(len(all_df)) if not all_df.empty else 0,
        "topCount": int(len(latest_df)) if not latest_df.empty else 0,
        "maxScore": number_or_none(all_df[score_col].max()) if score_col in all_df.columns else None,
        "dataSource": source,
        "marketCapturedAt": captured_at,
        "config": config,
        "backtest": metrics,
        "backtestMode": backtest_mode,
        "model": model_metrics,
        "paper": paper,
        "scoreBuckets": score_buckets(all_df, score_col),
        "scanState": SERVER_STATE,
    }


def get_results(query: dict[str, list[str]]) -> dict[str, Any]:
    scope = first(query, "scope", "all")
    limit = int(first(query, "limit", "500"))
    explicit_file = first(query, "file", "")
    if explicit_file:
        path = safe_result_path(explicit_file)
    elif scope == "top":
        path = latest_file("results_*.csv", exclude_prefix="results_all_")
    elif scope == "latest-candidates":
        path = REPORT_DIR / "latest_candidates.csv"
    elif scope == "paper":
        path = PAPER_LEDGER
    elif scope == "backtest":
        mode = first(query, "mode", "daily")
        path = REPORT_DIR / ("minute_backtest_picks.csv" if mode == "minute" else "backtest_picks.csv")
    else:
        path = latest_file("results_all_*.csv")

    if not path or not path.exists():
        return {"file": None, "rows": [], "columns": []}

    df = read_csv(path, limit=None)

    # 候选股列表：注入 paper 复盘结果（次日开盘收益）
    if scope in ("all", "top") and PAPER_LEDGER.exists() and not df.empty:
        df = _enrich_with_paper_returns(df)

    df = sort_result_frame(df, scope)
    if limit:
        df = df.head(limit)
    rows = dataframe_records(df)
    return {"file": file_info(path), "rows": rows, "columns": list(df.columns)}


def get_history(code: str) -> dict[str, Any]:
    normalized = normalize_code(code)
    if not normalized:
        return {"code": "", "rows": []}
    data_dir = resolve_data_dir(load_config())
    path = data_dir / f"history_{normalized}.csv"
    if not path.exists():
        return {"code": normalized, "rows": [], "error": "未找到历史数据"}
    df = pd.read_csv(path).tail(80)
    return {"code": normalized, "rows": dataframe_records(df), "file": file_info(path)}


def get_ollama_models() -> dict[str, Any]:
    try:
        data = ollama_request("GET", "/api/tags", timeout=5)
        models = [item.get("name") for item in data.get("models", []) if item.get("name")]
        configured = load_config().get("ollama_model")
        if configured and configured not in models:
            models.insert(0, configured)
        return {"available": True, "models": models, "defaultModel": configured or (models[0] if models else "")}
    except Exception as exc:
        configured = load_config().get("ollama_model", "")
        return {"available": False, "models": [configured] if configured else [], "defaultModel": configured, "error": str(exc)}


def handle_ollama_analyze(payload: dict[str, Any]) -> dict[str, Any]:
    model = str(payload.get("model") or load_config().get("ollama_model") or "").strip()
    if not model:
        raise ValueError("请选择 Ollama 模型")
    limit = max(1, min(int(payload.get("limit") or 20), 80))
    source = str(payload.get("source") or "latest").strip()
    rows = candidate_rows_for_ollama(source, limit)
    if not rows:
        raise ValueError("没有可分析的候选数据，请先运行扫描或生成候选")

    prompt = build_ollama_prompt(rows)
    started = time.time()
    data = ollama_request(
        "POST",
        "/api/generate",
        payload={"model": model, "stream": False, "prompt": prompt},
        timeout=180,
    )
    return {
        "model": model,
        "source": source,
        "count": len(rows),
        "elapsedSeconds": round(time.time() - started, 2),
        "response": str(data.get("response", "")).strip(),
    }


def start_scan() -> dict[str, Any]:
    if SERVER_STATE["scan_running"]:
        return {"started": False, "message": "扫描已在运行", "state": SERVER_STATE}

    def worker() -> None:
        SERVER_STATE.update({
            "scan_running": True,
            "scan_started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_finished_at": None,
            "scan_error": None,
            "scan_returncode": None,
            "scan_phase": "starting",
            "scan_log": [],
        })
        try:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT_DIR / "run.py"), "scan"],
                cwd=str(ROOT_DIR),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                append_scan_log(line.rstrip())
            proc.wait(timeout=600)
            SERVER_STATE["scan_returncode"] = proc.returncode
            if proc.returncode != 0:
                SERVER_STATE["scan_error"] = "\n".join(SERVER_STATE["scan_log"][-30:])
                SERVER_STATE["scan_phase"] = "failed"
            else:
                SERVER_STATE["scan_phase"] = "completed"
        except Exception as exc:
            SERVER_STATE["scan_error"] = str(exc)
            SERVER_STATE["scan_phase"] = "failed"
        finally:
            SERVER_STATE["scan_running"] = False
            SERVER_STATE["scan_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "message": "扫描已开始", "state": SERVER_STATE}


def start_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    if SERVER_STATE["backtest_running"]:
        return {"started": False, "message": "回测已在运行", "state": backtest_state()}

    mode = str(payload.get("mode") or "daily").strip()
    if mode not in {"daily", "minute"}:
        mode = "daily"
    command = "minute-backtest" if mode == "minute" else "backtest"

    def worker() -> None:
        SERVER_STATE.update({
            "backtest_running": True,
            "backtest_started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backtest_finished_at": None,
            "backtest_error": None,
            "backtest_returncode": None,
            "backtest_phase": "running",
            "backtest_log": [],
        })
        try:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT_DIR / "run.py"), command],
                cwd=str(ROOT_DIR),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                append_backtest_log(line.rstrip())
            proc.wait(timeout=900)
            SERVER_STATE["backtest_returncode"] = proc.returncode
            if proc.returncode != 0:
                SERVER_STATE["backtest_error"] = "\n".join(SERVER_STATE["backtest_log"][-30:])
                SERVER_STATE["backtest_phase"] = "failed"
            else:
                SERVER_STATE["backtest_phase"] = "completed"
        except Exception as exc:
            SERVER_STATE["backtest_error"] = str(exc)
            SERVER_STATE["backtest_phase"] = "failed"
        finally:
            SERVER_STATE["backtest_running"] = False
            SERVER_STATE["backtest_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "message": "回测已开始", "state": backtest_state()}


def backtest_state() -> dict[str, Any]:
    return {key: SERVER_STATE[key] for key in [
        "backtest_running",
        "backtest_started_at",
        "backtest_finished_at",
        "backtest_error",
        "backtest_returncode",
        "backtest_phase",
        "backtest_log",
    ]}


def append_scan_log(line: str) -> None:
    if not line:
        return
    logs = SERVER_STATE.setdefault("scan_log", [])
    logs.append(line)
    del logs[:-120]
    phase_map = [
        ("正在获取全市场实时数据", "获取实时行情"),
        ("获取到", "整理市场数据"),
        ("排除ST", "执行基础过滤"),
        ("实时预筛", "预筛候选池"),
        ("已处理", "计算特征评分"),
        ("开始二次增强", "二次增强验证"),
        ("二次增强完成", "排序保存结果"),
        ("扫描完成", "扫描完成"),
        ("虚拟盘记录新增", "更新虚拟盘"),
    ]
    for marker, phase in phase_map:
        if marker in line:
            SERVER_STATE["scan_phase"] = phase
            break


def append_backtest_log(line: str) -> None:
    if not line:
        return
    logs = SERVER_STATE.setdefault("backtest_log", [])
    logs.append(line)
    del logs[:-120]
    if "无法回测" in line or "ERROR" in line:
        SERVER_STATE["backtest_phase"] = "failed"
    elif "候选明细" in line or "指标报告" in line:
        SERVER_STATE["backtest_phase"] = "completed"


def candidate_rows_for_ollama(source: str, limit: int) -> list[dict[str, Any]]:
    if source == "latest-candidates":
        path = REPORT_DIR / "latest_candidates.csv"
    else:
        path = latest_file("results_*.csv", exclude_prefix="results_all_") or latest_file("results_all_*.csv")
    if not path or not path.exists():
        return []
    df = read_csv(path, limit=limit)
    return dataframe_records(df.head(limit))


def build_ollama_prompt(rows: list[dict[str, Any]]) -> str:
    data = json.dumps(rows, ensure_ascii=False, indent=2)
    return f"""你是一个严格的A股短线量化风控助手。

请只基于下面候选股的结构化指标做解释，不要编造新闻、公告、传闻或不存在的数据。
输出必须使用 Markdown，且至少包含一个 Markdown 表格，方便 Web UI 渲染。

输出结构：
1. 先用一句话说明这不是投资建议。
2. 给出候选股总览表，列包括：代码、名称、综合评分、强项、弱项、尾盘风险、需要验证的数据。
3. 再列出最高风险标的和相对稳健标的。
4. 最后给出风控结论，不要承诺涨停或收益。

候选股数据：
{data}
"""


def ollama_request(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    host = str(load_config().get("ollama_host", "http://localhost:11434")).rstrip("/")
    raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{host}{path}",
        data=raw,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama 连接失败: {exc}") from exc


def list_scan_files() -> list[dict[str, Any]]:
    files = sorted(BASE_DIR.glob("results*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [file_info(path) for path in files]


def latest_file(pattern: str, exclude_prefix: str | None = None) -> Path | None:
    files = list(BASE_DIR.glob(pattern))
    if exclude_prefix:
        files = [path for path in files if not path.name.startswith(exclude_prefix)]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def safe_result_path(name: str) -> Path:
    candidate = (BASE_DIR / Path(name).name).resolve()
    if candidate.parent != BASE_DIR or not candidate.name.startswith("results"):
        raise ValueError("非法结果文件")
    return candidate


def read_csv(path: Path | None, limit: int | None = None) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    dtype = {"代码": str, "code": str, "股票代码": str}
    df = pd.read_csv(path, dtype=dtype)
    for col in ["代码", "code", "股票代码"]:
        if col in df.columns:
            df[col] = df[col].map(normalize_code)
    if limit:
        df = df.head(limit)
    return df


def dataframe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    cleaned = df.where(pd.notna(df), None)
    records = cleaned.to_dict(orient="records")
    return [{key: clean_value(value) for key, value in row.items()} for row in records]


def clean_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)
    return value


def normalize_code(code: Any) -> str:
    text = "".join(ch for ch in str(code).strip() if ch.isdigit())
    return text.zfill(6)[-6:] if text else ""


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def file_info(path: Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "modifiedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "size": stat.st_size,
    }


def pick_first_value(df: pd.DataFrame, cols: list[str]) -> Any:
    for col in cols:
        if col in df.columns and not df.empty:
            value = df[col].dropna()
            if not value.empty:
                return clean_value(value.iloc[0])
    return None


def number_or_none(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def get_minute_backtest() -> dict[str, Any]:
    metrics_path = REPORT_DIR / "minute_backtest_metrics.json"
    metrics = load_json(metrics_path)
    picks_path = REPORT_DIR / "minute_backtest_picks.csv"
    if picks_path.exists():
        df = pd.read_csv(picks_path, dtype={"代码": str, "code": str})
        rows = dataframe_records(df.tail(500))
    else:
        rows = []
    return {"metrics": metrics, "rows": rows}


def get_backtest_report(query: dict[str, list[str]]) -> dict[str, Any]:
    mode = first(query, "mode", "minute")
    if mode == "daily":
        metrics_path = REPORT_DIR / "backtest_metrics.json"
        picks_path = REPORT_DIR / "backtest_picks.csv"
    else:
        mode = "minute"
        metrics_path = REPORT_DIR / "minute_backtest_metrics.json"
        picks_path = REPORT_DIR / "minute_backtest_picks.csv"

    metrics = load_json(metrics_path)
    if picks_path.exists():
        df = sort_result_frame(read_csv(picks_path, limit=None), "backtest").head(500)
        rows = dataframe_records(df)
        columns = list(df.columns)
    else:
        rows = []
        columns = []

    return {
        "mode": mode,
        "metrics": metrics,
        "rows": rows,
        "columns": columns,
        "file": file_info(picks_path) if picks_path.exists() else None,
        "metricsFile": file_info(metrics_path) if metrics_path.exists() else None,
    }


def sort_result_frame(df: pd.DataFrame, scope: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if scope == "paper":
        for col in ["scan_time", "扫描时间", "signal_date", "日期"]:
            if col in out.columns:
                out["_sort_time"] = pd.to_datetime(out[col], errors="coerce")
                return out.sort_values("_sort_time", ascending=False).drop(columns=["_sort_time"])
    if scope == "backtest":
        for col in ["date", "日期"]:
            if col in out.columns:
                out["_sort_time"] = pd.to_datetime(out[col], errors="coerce")
                return out.sort_values("_sort_time", ascending=False).drop(columns=["_sort_time"])
    return out


def score_buckets(df: pd.DataFrame, score_col: str) -> list[dict[str, Any]]:
    buckets = [
        ("高分(>80)", 80, float("inf"), "#22c55e"),
        ("中上(60-80)", 60, 80, "#10b981"),
        ("中等(40-60)", 40, 60, "#3b82f6"),
        ("中下(20-40)", 20, 40, "#f59e0b"),
        ("低分(<20)", float("-inf"), 20, "#ef4444"),
    ]
    if df.empty or score_col not in df.columns:
        return [{"label": label, "value": 0, "color": color} for label, _, _, color in buckets]
    scores = pd.to_numeric(df[score_col], errors="coerce")
    data = []
    for label, low, high, color in buckets:
        if high == float("inf"):
            count = int((scores > low).sum())
        elif low == float("-inf"):
            count = int((scores < high).sum())
        else:
            count = int(((scores >= low) & (scores <= high)).sum())
        data.append({"label": label, "value": count, "color": color})
    return data


if __name__ == "__main__":
    run()
