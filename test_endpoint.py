#!/usr/bin/env python3
"""Probe an OpenAI-compatible endpoint.

Lists every model exposed by ``GET /models`` and then sends each one a simple
chat completion so you can see which models actually respond.

Examples:
    python test_endpoint.py -e http://zidan:20129/v1 -k sk-123
    OPENAI_BASE_URL=http://zidan:20129/v1 OPENAI_API_KEY=sk-123 python test_endpoint.py
    python test_endpoint.py -e http://zidan:20129/v1 -k sk-123 --model gpt-4o --model llama3
"""

import argparse
import csv
import http.client
import json
from dotenv import load_dotenv, find_dotenv
import os
import socket
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

load_dotenv(find_dotenv())  # this line loads environment variables from .env file

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:8000")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-123")
DEFAULT_PROMPT = "Reply with exactly one word: OK."
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKENS = 16


class Colours:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    RESET = "\033[0m"

    @staticmethod
    def enabled():
        return sys.stdout.isatty()


def paint(text, code):
    return f"{code}{text}{Colours.RESET}" if Colours.enabled() else text


def short(text, limit=80):
    text = " ".join(str(text).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def build_headers(api_key):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _connect(host, port, timeout):
    """Connect to the first reachable address, preferring IPv4.

    urllib's default resolver tries every resolved address sequentially with the
    full timeout each, so a hostname that resolves to unreachable IPv6 addresses
    first (e.g. ``zidan`` -> several dead ``fdea:``/``2a02:`` addrs) hangs for
    ``timeout`` seconds per address.  Here we race the addresses with a
    per-address deadline and prefer IPv4, so a dead IPv6 entry can never stall us.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectionError(f"could not resolve {host}: {exc}")

    # Prefer IPv4 (AF_INET) over IPv6 (AF_INET6) to avoid dead IPv6 links.
    infos.sort(key=lambda info: 0 if info[0] == socket.AF_INET else 1)

    deadline = time.monotonic() + timeout
    last_err = None
    for family, socktype, proto, _canon, sockaddr in infos:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(remaining)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_err = exc
            sock.close()
    raise ConnectionError(
        f"could not reach {host}:{port}: {last_err or 'no usable address'}"
    )


def request_json(url, method="GET", headers=None, payload=None, timeout=DEFAULT_TIMEOUT):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ConnectionError(f"unsupported scheme in {url}: {parsed.scheme!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    conn = _connect(host, port, timeout)
    try:
        if parsed.scheme == "https":
            import ssl

            ctx = ssl.create_default_context()
            conn = ctx.wrap_socket(conn, server_hostname=host)
        http_conn = http.client.HTTPConnection(host, port, timeout=timeout)
        http_conn.sock = conn
        http_conn.request(method, path, body=data, headers=headers or {})
        resp = http_conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body
    except http.client.HTTPException as exc:
        raise ConnectionError(f"could not reach {url}: {exc}")


def fetch_models(endpoint, api_key, timeout):
    status, body = request_json(
        f"{endpoint}/models", headers=build_headers(api_key), timeout=timeout
    )
    if status != 200:
        raise RuntimeError(f"GET {endpoint}/models -> HTTP {status}\n{body[:1000]}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"GET {endpoint}/models returned invalid JSON: {exc}\n{body[:500]}"
        )
    rows = data.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("GET /models response has no 'data' array")
    models = []
    for row in rows:
        if isinstance(row, str):
            models.append({"id": row, "owned_by": ""})
        elif isinstance(row, dict) and row.get("id"):
            models.append({"id": row["id"], "owned_by": row.get("owned_by", "")})
    return models


def probe_model(endpoint, api_key, model_id, prompt, timeout, max_tokens):
    url = f"{endpoint}/chat/completions"
    headers = build_headers(api_key)
    messages = [{"role": "user", "content": prompt}]
    variants = [
        {"model": model_id, "messages": messages, "max_tokens": max_tokens, "temperature": 0},
        {"model": model_id, "messages": messages, "max_completion_tokens": max_tokens},
        {"model": model_id, "messages": messages, "temperature": 0},
        {"model": model_id, "messages": messages},
    ]
    started = time.monotonic()
    status, body = None, ""
    for payload in variants:
        status, body = request_json(
            url, method="POST", headers=headers, payload=payload, timeout=timeout
        )
        if status == 200:
            break
        if status != 400:
            break
    elapsed = time.monotonic() - started

    if status == 200:
        try:
            parsed = json.loads(body)
            content = parsed["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            content = body[:300]
        return {
            "model": model_id,
            "ok": True,
            "status": 200,
            "elapsed": elapsed,
            "content": content,
        }
    return {
        "model": model_id,
        "ok": False,
        "status": status,
        "elapsed": elapsed,
        "error": body[:500],
    }


# ---------------------------------------------------------------------------
# Report generation (CSV / Markdown / PDF)
# ---------------------------------------------------------------------------

REPORT_BASENAME = "test_endpoint"


def _report_paths(out_dir):
    out_dir = Path(out_dir)
    return {
        "csv": out_dir / f"{REPORT_BASENAME}.csv",
        "md": out_dir / f"{REPORT_BASENAME}.md",
        "pdf": out_dir / f"{REPORT_BASENAME}.pdf",
    }


def _status_text(result):
    if result["ok"]:
        return "OK"
    if result.get("status") is None:
        return "ERROR"
    return f"HTTP {result['status']}"


def _error_text(result):
    if result["ok"]:
        return ""
    return (result.get("error") or "").replace("\n", " ").strip()


def _content_text(result):
    if result["ok"]:
        return (result.get("content") or "").replace("\n", " ").strip()
    return ""


def write_csv(results, out_dir):
    path = _report_paths(out_dir)["csv"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["model", "status", "http_status", "elapsed_s", "content", "error"])
        for r in results:
            writer.writerow(
                [
                    r["model"],
                    _status_text(r),
                    r.get("status") if r.get("status") is not None else "",
                    f"{r['elapsed']:.2f}",
                    _content_text(r),
                    _error_text(r),
                ]
            )
    return path


def write_markdown(results, endpoint, out_dir):
    path = _report_paths(out_dir)["md"]
    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    failed = [r for r in results if not r["ok"]]

    lines = []
    lines.append("# Endpoint Model Test Report")
    lines.append("")
    lines.append(f"- **Endpoint:** `{endpoint}`")
    lines.append(f"- **Models tested:** {total}")
    lines.append(f"- **Passed:** {passed}")
    lines.append(f"- **Failed:** {total - passed}")
    lines.append(f"- **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Result | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Passed | {passed} |")
    lines.append(f"| Failed | {total - passed} |")
    lines.append("")

    lines.append("## Per-Model Results")
    lines.append("")
    lines.append("| Model | Status | HTTP | Elapsed (s) | Content | Error |")
    lines.append("|-------|--------|------|-------------|---------|-------|")
    for r in results:
        content = _content_text(r)
        if len(content) > 60:
            content = content[:60] + "…"
        error = _error_text(r)
        if len(error) > 60:
            error = error[:60] + "…"
        lines.append(
            f"| {r['model']} | {_status_text(r)} | "
            f"{r.get('status') if r.get('status') is not None else ''} | "
            f"{r['elapsed']:.2f} | {content} | {error} |"
        )
    lines.append("")

    if failed:
        lines.append("## Failed Models")
        lines.append("")
        for r in failed:
            lines.append(f"- **{r['model']}** (HTTP {r.get('status')}): {_error_text(r)}")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_pdf(md_path, out_dir, styles_css=None):
    """Render the markdown report to PDF via pandoc + weasyprint.

    Mirrors the repo's ``genpdf.sh`` pipeline so the output matches the
    existing report styling. Falls back to a plain (unstyled) PDF if the
    stylesheet is unavailable.
    """
    path = _report_paths(out_dir)["pdf"]
    html_path = path.with_suffix(".html")

    cmd = [
        "pandoc",
        str(md_path),
        "--from", "markdown+raw_html",
        "--standalone",
        "--metadata", "title=Endpoint Model Test Report",
        "--metadata", "charset=utf-8",
        "-t", "html5",
        "-o", str(html_path),
    ]
    if styles_css and Path(styles_css).is_file():
        cmd += ["--css", str(styles_css)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"pandoc failed: {exc}")

    wp_cmd = ["weasyprint", str(html_path), str(path)]
    if styles_css and Path(styles_css).is_file():
        wp_cmd[1:1] = ["--stylesheet", str(styles_css)]
    try:
        subprocess.run(wp_cmd, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(f"weasyprint failed: {exc}")

    return path


def write_reports(results, endpoint, out_dir, styles_css=None):
    """Write CSV, Markdown and PDF reports; returns a dict of output paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(results, out_dir)
    md_path = write_markdown(results, endpoint, out_dir)
    pdf_path = write_pdf(md_path, out_dir, styles_css)
    return {"csv": csv_path, "md": md_path, "pdf": pdf_path}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="test_endpoint",
        description=(
            "List models on an OpenAI-compatible endpoint and verify each one "
            "responds to a simple prompt."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-e",
        "--endpoint",
        default=os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_ENDPOINT"),
        help="Base URL of the API, including any /v1 suffix",
    )
    parser.add_argument(
        "-k",
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY"),
        help="API key (omit for servers without authentication)",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt used to test each model")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of models to test in parallel; 1 = sequential",
    )
    parser.add_argument("--list-only", action="store_true", help="Only list models, do not run any test")
    parser.add_argument(
        "--model",
        action="append",
        dest="wanted",
        metavar="ID",
        help="Test only this model ID (repeatable)",
    )
    parser.add_argument(
        "--report-dir",
        default=os.environ.get("TEST_ENDPOINT_REPORT_DIR", "reports/endpoint"),
        help="Directory where test_endpoint.csv/.md/.pdf are written",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip writing CSV/Markdown/PDF reports",
    )
    parser.add_argument(
        "--styles-css",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles.css"),
        help="Stylesheet used for the PDF report (default: repo styles.css)",
    )
    args = parser.parse_args(argv)

    if not args.endpoint:
        parser.error("no endpoint given (pass --endpoint or set OPENAI_BASE_URL)")
    endpoint = args.endpoint.rstrip("/")

    print(f"Endpoint: {endpoint}")
    print(f"API key : {'set' if args.api_key else 'not set (anonymous)'}")
    print()

    try:
        models = fetch_models(endpoint, args.api_key, args.timeout)
    except (RuntimeError, ConnectionError, ValueError) as exc:
        print(paint(f"ERROR: {exc}", Colours.FAIL))
        return 1

    if not models:
        print(paint("ERROR: endpoint reports no models", Colours.FAIL))
        return 1

    print(paint("Models Count: {m}".format(m=len(models)), Colours.OK))
    print()

    if args.wanted:
        wanted = set(args.wanted)
        found = {m["id"] for m in models}
        missing = sorted(wanted - found)
        models = [m for m in models if m["id"] in wanted]
        if missing:
            print(paint(f"WARNING: requested models not found: {', '.join(missing)}", Colours.WARN))

    width = max(len(m["id"]) for m in models)
    print(f"Found {len(models)} model(s):")
    for m in models:
        owner = f" ({m['owned_by']})" if m.get("owned_by") else ""
        print(f"  {m['id']:<{width}}{owner}")
    print()

    if args.list_only:
        return 0

    workers = max(1, args.workers)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                probe_model, endpoint, args.api_key, m["id"],
                args.prompt, args.timeout, args.max_tokens,
            ): m["id"]
            for m in models
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "model": futures[future],
                    "ok": False,
                    "status": None,
                    "elapsed": 0.0,
                    "error": str(exc),
                }
            results.append(result)
            tag = paint("OK ", Colours.OK) if result["ok"] else paint("FAIL", Colours.FAIL)
            if result["ok"]:
                print(f"  {tag} {result['model']:<{width}} {result['elapsed']:.1f}s -> {short(result['content'])}")
            else:
                print(f"  {tag} {result['model']:<{width}} HTTP {result.get('status')} {short(result.get('error') or 'no response')}")

    passed = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    print()
    print(f"Passed: {passed}/{len(results)}")
    if failed:
        print(paint(f"Failed: {len(failed)} model(s)", Colours.FAIL))
        for r in failed:
            reason = short((r.get("error") or "").replace("\n", " "), 160)
            print(f"  {r['model']}: HTTP {r.get('status')} {reason}")

    if not args.no_report:
        try:
            paths = write_reports(
                results, endpoint, args.report_dir, styles_css=args.styles_css
            )
            print()
            print(paint("Reports written:", Colours.OK))
            for kind, path in paths.items():
                print(f"  {kind.upper():<4} {path}")
        except (RuntimeError, OSError) as exc:
            print(paint(f"WARNING: could not write reports: {exc}", Colours.WARN))

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
