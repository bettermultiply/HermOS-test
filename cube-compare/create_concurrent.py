#!/usr/bin/env python3
"""并发创建 CubeSandbox 压测脚本

通过 CubeAPI (E2B-compatible REST) 直接创建/销毁沙箱，无需 e2b SDK。
所有沙箱创建完成后统一并发销毁。

用法:
    python create_concurrent.py -n 20
    python create_concurrent.py -w 8 -n 50
    python create_concurrent.py -n 10 --template my-template-id

环境变量:
    E2B_API_URL      CubeAPI 地址，如 http://127.0.0.1:3000
    E2B_API_KEY      API Key
    CUBE_TEMPLATE_ID 模板 ID（可被 --template 覆盖）
    SSL_CERT_FILE    可选，自签 CA 证书路径
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("Error: pip install requests")

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    sys.exit("Error: pip install rich")

console = Console()


# ── State ─────────────────────────────────────────────────────────────────

class State(Enum):
    PENDING     = "pending"
    CREATING    = "creating"
    CREATED     = "created"
    DESTROYING  = "destroying"
    DONE        = "done"
    FAILED      = "failed"

STATE_STYLE = {
    State.PENDING:    ("dim",    "·"),
    State.CREATING:   ("yellow", "⟳"),
    State.CREATED:    ("cyan",   "✓"),
    State.DESTROYING: ("yellow", "⟳"),
    State.DONE:       ("green",  "✓"),
    State.FAILED:     ("red",    "✗"),
}


@dataclass
class Task:
    idx: int
    state: State = State.PENDING
    sandbox_id: str = ""
    status: str = ""
    error: str = ""
    create_ms: float = 0.0
    _t0: float = field(default=0.0, repr=False)

    @property
    def elapsed_ms(self) -> float:
        if self.create_ms > 0:
            return self.create_ms
        if self._t0 > 0:
            return (time.monotonic() - self._t0) * 1000
        return 0.0


# ── HTTP client ───────────────────────────────────────────────────────────

def make_session(max_connections: int, ssl_cert: str | None) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        max_retries=Retry(total=0),
        pool_connections=max_connections,
        pool_maxsize=max_connections,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if ssl_cert:
        session.verify = ssl_cert
    return session


# ── CubeAPI calls ─────────────────────────────────────────────────────────

def api_create(session: requests.Session, base_url: str, headers: dict,
               template_id: str, timeout: int = 60) -> dict:
    r = session.post(
        f"{base_url}/sandboxes",
        json={"templateID": template_id},
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def api_destroy(session: requests.Session, base_url: str, headers: dict,
                sandbox_id: str, timeout: int = 30) -> None:
    r = session.delete(
        f"{base_url}/sandboxes/{sandbox_id}",
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()


# ── Workers ───────────────────────────────────────────────────────────────

def create_task(task: Task, session: requests.Session, base_url: str,
                headers: dict, template_id: str, timeout: int) -> None:
    task.state = State.CREATING
    task.status = "Creating"
    task._t0 = time.monotonic()
    try:
        t0 = time.monotonic()
        data = api_create(session, base_url, headers, template_id, timeout)
        task.create_ms = (time.monotonic() - t0) * 1000
        task.sandbox_id = data.get("sandboxID", data.get("sandbox_id", ""))
        task.state = State.CREATED
        task.status = "Created"
    except Exception as e:
        task.state = State.FAILED
        task.error = str(e)[:120]
        task.status = type(e).__name__


def destroy_task(task: Task, session: requests.Session, base_url: str,
                 headers: dict) -> None:
    if not task.sandbox_id:
        return
    task.state = State.DESTROYING
    task.status = "Destroying"
    try:
        api_destroy(session, base_url, headers, task.sandbox_id)
        task.state = State.DONE
        task.status = "Destroyed"
    except Exception as e:
        task.state = State.FAILED
        task.error = str(e)[:120]
        task.status = type(e).__name__


# ── Stats ─────────────────────────────────────────────────────────────────

def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] if lo == hi else s[lo] * (hi - k) + s[hi] * (k - lo)

def fmt_ms(ms: float) -> str:
    return "-" if ms <= 0 else f"{round(ms)}ms"

def fmt_dur(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m{sec:02d}s"


# ── Dashboard ─────────────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, tasks: list[Task], workers: int, template: str, phase: str = "create"):
        self.tasks = tasks
        self.workers = workers
        self.template = template
        self.phase = phase
        self.t0 = time.time()

    def render(self) -> Group:
        return Group(self._header(), self._stats(), self._table())

    def _header(self) -> Panel:
        tpl = self.template[:28] + ("…" if len(self.template) > 28 else "")
        elapsed = fmt_dur(time.time() - self.t0)
        phase_str = "[yellow]Creating[/]" if self.phase == "create" else "[cyan]Destroying[/]"
        return Panel(
            f"Phase: {phase_str}  "
            f"Workers: [bold cyan]{self.workers}[/]  "
            f"Total: [bold cyan]{len(self.tasks)}[/]  "
            f"Template: [bold cyan]{tpl}[/]  "
            f"Elapsed: [bold cyan]{elapsed}[/]",
            title="[bold]CubeAPI Concurrent Create[/]",
            border_style="bright_blue",
        )

    def _stats(self) -> Panel:
        by_state: dict[State, int] = {}
        for t in self.tasks:
            by_state[t.state] = by_state.get(t.state, 0) + 1

        terminal = {State.DONE, State.FAILED}
        if self.phase == "create":
            terminal = {State.CREATED, State.FAILED}

        done  = sum(by_state.get(s, 0) for s in terminal)
        total = len(self.tasks)
        ratio = done / total if total else 0
        bar_w = 32
        filled = int(bar_w * ratio)
        bar = f"[green]{'━' * filled}[/][dim]{'━' * (bar_w - filled)}[/]"

        ct = [t.create_ms for t in self.tasks if t.create_ms > 0]
        stats = ""
        if ct:
            stats = (
                f"  │  Create  "
                f"avg [cyan]{fmt_ms(sum(ct)/len(ct))}[/]  "
                f"p50 [cyan]{fmt_ms(percentile(ct, 50))}[/]  "
                f"p95 [cyan]{fmt_ms(percentile(ct, 95))}[/]  "
                f"max [cyan]{fmt_ms(max(ct))}[/]"
            )

        counts = "  ".join(
            f"{s.value} [{STATE_STYLE[s][0]}]{by_state[s]}[/]"
            for s in State if s in by_state
        )
        return Panel(
            f"{bar}  {done}/{total} ({ratio*100:.0f}%)\n{counts}{stats}",
            border_style="blue",
        )

    def _table(self) -> Table:
        t = Table(show_header=True, header_style="bold", box=None,
                  pad_edge=False, show_edge=False)
        t.add_column("#",          width=4,  style="dim")
        t.add_column("State",      width=12)
        t.add_column("Sandbox ID", width=34, style="dim")
        t.add_column("Create",     width=9,  justify="right")
        t.add_column("Status",     width=12)
        t.add_column("Error",      min_width=20, style="red dim")

        for task in self.tasks[-50:]:
            style, icon = STATE_STYLE[task.state]
            create = fmt_ms(task.elapsed_ms) if task._t0 > 0 else "-"
            t.add_row(
                str(task.idx),
                f"[{style}]{icon} {task.state.value}[/]",
                task.sandbox_id or "-",
                create,
                task.status,
                task.error,
            )
        return t


# ── Main ──────────────────────────────────────────────────────────────────

def load_dotenv() -> None:
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    try:
        from dotenv import load_dotenv as _load
        _load(env, override=False)
    except ImportError:
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="CubeAPI 并发创建沙箱")
    parser.add_argument("-w", "--workers",  type=int, default=None,
                        help="并发数 (default: 与 -n 相同)")
    parser.add_argument("-n", "--count",    type=int, default=10,
                        help="创建总数 (default: 10)")
    parser.add_argument("--template",       default=os.environ.get("CUBE_TEMPLATE_ID", ""),
                        help="模板 ID (default: $CUBE_TEMPLATE_ID)")
    parser.add_argument("--timeout",        type=int, default=60,
                        help="单次 API 超时秒数 (default: 60)")
    args = parser.parse_args()

    base_url = os.environ.get("E2B_API_URL", "").rstrip("/")
    api_key  = os.environ.get("E2B_API_KEY", "")
    template = args.template
    workers  = args.workers if args.workers is not None else args.count

    if not base_url:
        sys.exit("Error: E2B_API_URL 未设置")
    if not api_key:
        sys.exit("Error: E2B_API_KEY 未设置")
    if not template:
        sys.exit("Error: 模板 ID 未指定，使用 --template 或设置 CUBE_TEMPLATE_ID")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    ssl_cert = os.environ.get("SSL_CERT_FILE") or None
    session  = make_session(max_connections=workers + 4, ssl_cert=ssl_cert)
    tasks    = [Task(idx=i + 1) for i in range(args.count)]
    dash     = Dashboard(tasks, workers, template, phase="create")

    # ── Phase 1: 并发创建 ──────────────────────────────────────────────
    with Live(dash.render(), refresh_per_second=4, console=console) as live:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(create_task, t, session, base_url, headers,
                            template, args.timeout): t
                for t in tasks
            }
            for _ in as_completed(futs):
                live.update(dash.render())
        live.update(dash.render())

    created = [t for t in tasks if t.state == State.CREATED]
    failed  = [t for t in tasks if t.state == State.FAILED]
    ct      = [t.create_ms for t in created]

    console.print()
    console.print(
        f"[bold]创建完成[/]  成功 [green]{len(created)}[/]  "
        f"失败 [red]{len(failed)}[/]  共 {args.count}"
    )
    if ct:
        console.print(
            f"创建耗时  "
            f"avg [cyan]{fmt_ms(sum(ct)/len(ct))}[/]  "
            f"p50 [cyan]{fmt_ms(percentile(ct, 50))}[/]  "
            f"p95 [cyan]{fmt_ms(percentile(ct, 95))}[/]  "
            f"max [cyan]{fmt_ms(max(ct))}[/]"
        )

    if not created:
        if failed:
            console.print("\n[red]失败详情:[/]")
            for t in failed:
                console.print(f"  #{t.idx}: {t.error}")
        session.close()
        sys.exit(1)

    # ── Phase 2: 全部创建完后并发销毁 ─────────────────────────────────
    console.print("\n[bold]开始销毁所有沙箱...[/]")
    dash.phase = "destroy"

    with Live(dash.render(), refresh_per_second=4, console=console) as live:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(destroy_task, t, session, base_url, headers): t
                for t in created
            }
            for _ in as_completed(futs):
                live.update(dash.render())
        live.update(dash.render())

    destroyed     = [t for t in created if t.state == State.DONE]
    destroy_failed = [t for t in created if t.state == State.FAILED]

    console.print()
    console.print(
        f"[bold]销毁完成[/]  成功 [green]{len(destroyed)}[/]  "
        f"失败 [red]{len(destroy_failed)}[/]"
    )

    all_failed = failed + destroy_failed
    if all_failed:
        console.print("\n[red]失败详情:[/]")
        for t in all_failed:
            console.print(f"  #{t.idx} {t.sandbox_id or ''}: {t.error}")

    session.close()
    sys.exit(0 if not all_failed else 1)


if __name__ == "__main__":
    main()
