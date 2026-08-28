"""Loopback-only browser presentation for verified Technocore messages."""

from __future__ import annotations

import html
import json
import math
import secrets
import time
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from technocore_orchestrator.errors import ConfigurationError, PreflightError, WorkflowError

MAX_CURSOR = (1 << 63) - 1
MAX_RESPONSE_BYTES = 1 << 20

SnapshotReader = Callable[[int], dict[str, Any]]


class ConversationViewerServer(HTTPServer):
    """Single-user HTTP server restricted to a random loopback URL."""

    allow_reuse_address = False

    def __init__(
        self,
        run_id: str,
        snapshot_reader: SnapshotReader,
        *,
        port: int = 0,
        startup_timeout_seconds: float = 0.0,
    ) -> None:
        if isinstance(port, bool) or not 0 <= port <= 65_535:
            raise ConfigurationError("conversation viewer port must be from 0 through 65535")
        if (
            isinstance(startup_timeout_seconds, bool)
            or not isinstance(startup_timeout_seconds, (int, float))
            or not math.isfinite(startup_timeout_seconds)
            or not 0 <= startup_timeout_seconds <= 3600
        ):
            raise ConfigurationError(
                "conversation viewer startup timeout must be from 0 through 3600 seconds"
            )
        self.run_id = run_id
        self.snapshot_reader = snapshot_reader
        self.capability = secrets.token_urlsafe(24)
        self.nonce = secrets.token_urlsafe(18)
        self.should_stop = False
        self.run_observed = False
        self.startup_deadline = (
            time.monotonic() + startup_timeout_seconds if startup_timeout_seconds else None
        )
        super().__init__(("127.0.0.1", port), ConversationViewerHandler, bind_and_activate=True)
        self.timeout = min(1.0, max(0.05, startup_timeout_seconds))

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/{self.capability}/"

    def serve_until_closed(self) -> None:
        while not self.should_stop:
            self.handle_request()
            if (
                not self.run_observed
                and self.startup_deadline is not None
                and time.monotonic() >= self.startup_deadline
            ):
                self.should_stop = True

    def observe_snapshot(self, payload: dict[str, Any]) -> None:
        if payload.get("state") != "waiting_for_run":
            self.run_observed = True


class ConversationViewerHandler(BaseHTTPRequestHandler):
    """Serve one generated page and its same-origin read-only timeline API."""

    server_version = "TechnocoreConversationViewer/1"
    sys_version = ""

    def do_GET(self) -> None:
        server = cast(ConversationViewerServer, self.server)
        if not self._has_expected_host(server):
            self._write_problem(HTTPStatus.MISDIRECTED_REQUEST, "Unexpected local host header.")
            return
        parsed = urlsplit(self.path)
        root = f"/{server.capability}/"
        if parsed.path == root and not parsed.query and not parsed.fragment:
            document = render_conversation_page(server.run_id, server.nonce).encode("utf-8")
            self._write(
                HTTPStatus.OK,
                document,
                content_type="text/html; charset=utf-8",
                content_security_policy=_content_security_policy(server.nonce),
            )
            return
        if parsed.path == root + "api/timeline" and not parsed.fragment:
            self._timeline(server, parsed.query)
            return
        self._write_problem(HTTPStatus.NOT_FOUND, "Viewer resource not found.")

    def do_POST(self) -> None:
        server = cast(ConversationViewerServer, self.server)
        parsed = urlsplit(self.path)
        expected = f"/{server.capability}/api/close"
        if not self._has_expected_host(server) or parsed.path != expected or parsed.query:
            self._write_problem(HTTPStatus.NOT_FOUND, "Viewer resource not found.")
            return
        if self.headers.get("Origin") != server.origin:
            self._write_problem(HTTPStatus.FORBIDDEN, "Viewer close request has an invalid origin.")
            return
        if self.headers.get("Content-Length", "0") != "0":
            self._write_problem(HTTPStatus.BAD_REQUEST, "Viewer close request must be empty.")
            return
        self._write_json(HTTPStatus.OK, {"closed": True})
        server.should_stop = True

    def do_HEAD(self) -> None:
        self._write_problem(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is not supported.")

    def do_OPTIONS(self) -> None:
        self._write_problem(HTTPStatus.METHOD_NOT_ALLOWED, "Cross-origin access is disabled.")

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def _timeline(self, server: ConversationViewerServer, query: str) -> None:
        try:
            fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
            raw_values = fields.get("after")
            if set(fields) != {"after"} or raw_values is None or len(raw_values) != 1:
                raise ValueError
            raw_cursor = raw_values[0]
            if not raw_cursor.isascii() or not raw_cursor.isdecimal():
                raise ValueError
            cursor = int(raw_cursor)
            if not 0 <= cursor <= MAX_CURSOR:
                raise ValueError
        except ValueError:
            self._write_problem(HTTPStatus.BAD_REQUEST, "Timeline cursor is invalid.")
            return
        try:
            payload = server.snapshot_reader(cursor)
            server.observe_snapshot(payload)
            self._write_json(HTTPStatus.OK, payload)
        except WorkflowError as exc:
            self._write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"category": exc.category.value, "message": exc.message}},
            )
        except Exception:
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": {"category": "internal", "message": "Viewer request failed."}},
            )

    def _has_expected_host(self, server: ConversationViewerServer) -> bool:
        return self.headers.get("Host") == f"127.0.0.1:{server.server_port}"

    def _write_problem(self, status: HTTPStatus, message: str) -> None:
        self._write_json(status, {"error": {"category": "http", "message": message}})

    def _write_json(self, status: HTTPStatus, payload: object) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            self._write_problem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Viewer response exceeded its size limit.",
            )
            return
        self._write(status, encoded, content_type="application/json; charset=utf-8")

    def _write(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        content_security_policy: str = "default-src 'none'; frame-ancestors 'none'",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", content_security_policy)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)


def open_and_serve_conversation_viewer(
    run_id: str,
    snapshot_reader: SnapshotReader,
    *,
    port: int = 0,
    open_browser: bool = True,
    startup_timeout_seconds: float = 0.0,
) -> str:
    """Open and serve one private local conversation page until it is closed."""

    with ConversationViewerServer(
        run_id,
        snapshot_reader,
        port=port,
        startup_timeout_seconds=startup_timeout_seconds,
    ) as server:
        url = server.url
        print(f"Technocore Agent Orchestrator UI: {url}", flush=True)
        if open_browser and not webbrowser.open(url, new=2):
            raise PreflightError("unable to open the default browser for the conversation UI")
        server.serve_until_closed()
        return url


def render_conversation_page(run_id: str, nonce: str) -> str:
    """Render a standalone page that inserts timeline values with textContent only."""

    safe_run_id = html.escape(run_id, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Technocore Agent Orchestrator · {safe_run_id}</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; color: #eef2ff; background: #080b14; }}
    body::before {{ content: ""; position: fixed; inset: 0; pointer-events: none;
      background: radial-gradient(circle at 16% 8%, #4f46e533, transparent 34%),
        radial-gradient(circle at 82% 0%, #06b6d422, transparent 30%); }}
    .shell {{ position: relative; width: min(1080px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 48px; }}
    header {{ display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 24px; }}
    .eyebrow {{ margin: 0 0 8px; color: #67e8f9; font-size: 12px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }}
    h1 {{ margin: 0; font-size: clamp(28px, 5vw, 48px); letter-spacing: -.045em; line-height: 1; }}
    .run {{ margin: 12px 0 0; color: #9aa4bd; font: 13px ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .actions {{ display: flex; align-items: center; gap: 12px; }}
    .status {{ display: inline-flex; align-items: center; gap: 8px; min-width: 132px; padding: 10px 13px;
      border: 1px solid #29324b; border-radius: 999px; background: #101625cc; color: #c7d2fe; font-size: 13px; }}
    .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #fbbf24; box-shadow: 0 0 16px #fbbf24aa; }}
    .status.live .dot {{ background: #34d399; box-shadow: 0 0 16px #34d399aa; animation: pulse 1.6s infinite; }}
    .status.done .dot {{ background: #818cf8; box-shadow: none; }}
    button {{ border: 1px solid #35405d; border-radius: 10px; padding: 10px 14px; color: #dbeafe;
      background: #151c2d; font: inherit; cursor: pointer; }}
    button:hover {{ border-color: #6673a3; background: #1b253b; }}
    .privacy {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; overflow: hidden;
      margin-bottom: 16px; border: 1px solid #222b41; border-radius: 14px; background: #222b41; }}
    .privacy div {{ padding: 15px 17px; background: #0e1422; }}
    .privacy strong {{ display: block; margin-bottom: 4px; font-size: 13px; }}
    .privacy span {{ color: #7f8aa3; font-size: 12px; }}
    .panel {{ min-height: 520px; overflow: hidden; border: 1px solid #222b41; border-radius: 18px;
      background: #0d121ecc; box-shadow: 0 28px 90px #0008; backdrop-filter: blur(16px); }}
    .panel-head {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 20px;
      border-bottom: 1px solid #20283b; color: #8f9ab3; font-size: 12px; }}
    #timeline {{ display: flex; flex-direction: column; gap: 14px; min-height: 430px; max-height: 66vh;
      overflow-y: auto; padding: 20px; scrollbar-color: #38435f transparent; }}
    .empty {{ display: grid; place-items: center; flex: 1; min-height: 380px; color: #758099; text-align: center; }}
    .message {{ display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 12px; max-width: 86%; }}
    .message.implementer {{ align-self: flex-end; grid-template-columns: minmax(0, 1fr) 42px; }}
    .message.implementer .avatar {{ grid-column: 2; }}
    .message.implementer .bubble {{ grid-column: 1; grid-row: 1; background: #151b2b; }}
    .avatar {{ display: grid; place-items: center; width: 42px; height: 42px; border: 1px solid #3e4a6a;
      border-radius: 13px; background: linear-gradient(145deg, #232e4d, #11182a); color: #e6e9f2; }}
    .avatar svg {{ width: 23px; height: 23px; }}
    .implementer .avatar {{ color: #d97757; }}
    .bubble {{ min-width: 0; padding: 13px 15px; border: 1px solid #252f46; border-radius: 5px 16px 16px 16px; background: #111827; }}
    .implementer .bubble {{ border-radius: 16px 5px 16px 16px; }}
    .meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 7px; color: #8290ad; font-size: 11px; }}
    .meta strong {{ color: #e0e7ff; font-size: 12px; }}
    .kind {{ padding: 3px 7px; border-radius: 999px; background: #222b42; color: #a5b4fc; }}
    .text {{ margin: 0; color: #d8def0; font-size: 14px; line-height: 1.58; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .system-event {{ align-self: center; display: flex; flex-wrap: wrap; justify-content: center; align-items: center;
      gap: 7px; max-width: min(88%, 720px); padding: 7px 12px; border: 1px solid #26304a; border-radius: 999px;
      background: #111827cc; color: #8f9ab3; font-size: 11px; text-align: center; }}
    .system-event strong {{ color: #c7d2fe; font-weight: 700; }}
    .system-event .system-text {{ color: #aab4ca; }}
    .brand-symbols {{ position: absolute; width: 0; height: 0; overflow: hidden; }}
    .error {{ color: #fca5a5; }}
    .closed {{ position: fixed; inset: 0; display: none; place-items: center; background: #080b14f2; z-index: 3; }}
    .closed.visible {{ display: grid; }}
    .closed div {{ text-align: center; }}
    @keyframes pulse {{ 50% {{ opacity: .45; transform: scale(.8); }} }}
    @media (max-width: 720px) {{ header {{ flex-direction: column; }} .privacy {{ grid-template-columns: 1fr; }}
      .message {{ max-width: 100%; }} .actions {{ width: 100%; justify-content: space-between; }} }}
    @media (prefers-reduced-motion: reduce) {{ .dot {{ animation: none !important; }} }}
  </style>
</head>
<body>
  <svg class="brand-symbols" aria-hidden="true" focusable="false">
    <symbol id="codexLogo" viewBox="0 0 41 41">
      <path fill="none" stroke="currentColor" stroke-width="2" d="M37.5324 16.8707C37.9808 15.5241 38.1363 14.0974 37.9886 12.6859C37.8409 11.2744 37.3934 9.91076 36.676 8.68622C35.6126 6.83404 33.9882 5.3676 32.0373 4.4985C30.0864 3.62941 27.9098 3.40259 25.8215 3.85078C24.8796 2.7893 23.7219 1.94125 22.4257 1.36341C21.1295 0.785575 19.7249 0.491269 18.3058 0.500197C16.1708 0.495044 14.0893 1.16803 12.3614 2.42214C10.6335 3.67624 9.34853 5.44666 8.6917 7.47815C7.30085 7.76286 5.98686 8.3414 4.8377 9.17505C3.68854 10.0087 2.73073 11.0782 2.02839 12.312C0.956464 14.1591 0.498905 16.2988 0.721698 18.4228C0.944492 20.5467 1.83612 22.5449 3.268 24.1293C2.81966 25.4759 2.66413 26.9026 2.81182 28.3141C2.95951 29.7256 3.40701 31.0892 4.12437 32.3138C5.18791 34.1659 6.8123 35.6322 8.76321 36.5013C10.7141 37.3704 12.8907 37.5973 14.9789 37.1492C15.9208 38.2107 17.0786 39.0587 18.3747 39.6366C19.6709 40.2144 21.0755 40.5087 22.4946 40.4998C24.6307 40.5054 26.7133 39.8321 28.4418 38.5772C30.1704 37.3223 31.4556 35.5506 32.1119 33.5179C33.5027 33.2332 34.8167 32.6547 35.9659 31.821C37.115 30.9874 38.0728 29.9178 38.7752 28.684C39.8458 26.8371 40.3023 24.6979 40.0789 22.5748C39.8556 20.4517 38.9639 18.4544 37.5324 16.8707ZM22.4978 37.8849C20.7443 37.8874 19.0459 37.2733 17.6994 36.1501C17.7601 36.117 17.8666 36.0586 17.936 36.0161L25.9004 31.4156C26.1003 31.3019 26.2663 31.137 26.3813 30.9378C26.4964 30.7386 26.5563 30.5124 26.5549 30.2825V19.0542L29.9213 20.998C29.9389 21.0068 29.9541 21.0198 29.9656 21.0359C29.977 21.052 29.9842 21.0707 29.9867 21.0902V30.3889C29.9842 32.375 29.1946 34.2791 27.7909 35.6841C26.3872 37.0892 24.4838 37.8806 22.4978 37.8849ZM6.39227 31.0064C5.51397 29.4888 5.19742 27.7107 5.49804 25.9832C5.55718 26.0187 5.66048 26.0818 5.73461 26.1244L13.699 30.7248C13.8975 30.8408 14.1233 30.902 14.3532 30.902C14.583 30.902 14.8088 30.8408 15.0073 30.7248L24.731 25.1103V28.9979C24.7321 29.0177 24.7283 29.0376 24.7199 29.0556C24.7115 29.0736 24.6988 29.0893 24.6829 29.1012L16.6317 33.7497C14.9096 34.7416 12.8643 35.0097 10.9447 34.4954C9.02506 33.9811 7.38785 32.7263 6.39227 31.0064ZM4.29707 13.6194C5.17156 12.0998 6.55279 10.9364 8.19885 10.3327C8.19885 10.4013 8.19491 10.5228 8.19491 10.6071V19.808C8.19351 20.0378 8.25334 20.2638 8.36823 20.4629C8.48312 20.6619 8.64893 20.8267 8.84863 20.9404L18.5723 26.5542L15.206 28.4979C15.1894 28.5089 15.1703 28.5155 15.1505 28.5173C15.1307 28.5191 15.1107 28.516 15.0924 28.5082L7.04046 23.8557C5.32135 22.8601 4.06716 21.2235 3.55289 19.3046C3.03862 17.3858 3.30624 15.3413 4.29707 13.6194ZM31.955 20.0556L22.2312 14.4411L25.5976 12.4981C25.6142 12.4872 25.6333 12.4805 25.6531 12.4787C25.6729 12.4769 25.6928 12.4801 25.7111 12.4879L33.7631 17.1364C34.9967 17.849 36.0017 18.8982 36.6606 20.1613C37.3194 21.4244 37.6047 22.849 37.4832 24.2684C37.3617 25.6878 36.8382 27.0432 35.9743 28.1759C35.1103 29.3086 33.9415 30.1717 32.6047 30.6641C32.6047 30.5947 32.6047 30.4733 32.6047 30.3889V21.188C32.6066 20.9586 32.5474 20.7328 32.4332 20.5338C32.319 20.3348 32.154 20.1698 31.955 20.0556ZM35.3055 15.0128C35.2464 14.9765 35.1431 14.9142 35.069 14.8717L27.1045 10.2712C26.906 10.1554 26.6803 10.0943 26.4504 10.0943C26.2206 10.0943 25.9948 10.1554 25.7963 10.2712L16.0726 15.8858V11.9982C16.0715 11.9783 16.0753 11.9585 16.0837 11.9405C16.0921 11.9225 16.1048 11.9068 16.1207 11.8949L24.1719 7.25025C25.4053 6.53903 26.8158 6.19376 28.2383 6.25482C29.6608 6.31589 31.0364 6.78077 32.2044 7.59508C33.3723 8.40939 34.2842 9.53945 34.8334 10.8531C35.3826 12.1667 35.5464 13.6095 35.3055 15.0128ZM14.2424 21.9419L10.8752 19.9981C10.8576 19.9893 10.8423 19.9763 10.8309 19.9602C10.8195 19.9441 10.8122 19.9254 10.8098 19.9058V10.6071C10.8107 9.18295 11.2173 7.78848 11.9819 6.58696C12.7466 5.38544 13.8377 4.42659 15.1275 3.82264C16.4173 3.21869 17.8524 2.99464 19.2649 3.1767C20.6775 3.35876 22.0089 3.93941 23.1034 4.85067C23.0427 4.88379 22.937 4.94215 22.8668 4.98473L14.9024 9.58517C14.7025 9.69878 14.5366 9.86356 14.4215 10.0626C14.3065 10.2616 14.2466 10.4877 14.2479 10.7175L14.2424 21.9419ZM16.071 17.9991L20.4018 15.4978L24.7325 17.9975V22.9985L20.4018 25.4983L16.071 22.9985V17.9991Z"/>
    </symbol>
    <symbol id="claudeLogo" viewBox="0 0 24 24">
      <path fill="currentColor" d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/>
    </symbol>
  </svg>
  <main class="shell">
    <header>
      <div>
        <p class="eyebrow">Private workflow room</p>
        <h1>Technocore Agent Orchestrator</h1>
        <p class="run">{safe_run_id}</p>
      </div>
      <div class="actions">
        <div id="status" class="status" role="status" aria-live="polite"><span class="dot"></span><span id="statusText">Waiting for run</span></div>
        <button id="closeViewer" type="button">Close viewer</button>
      </div>
    </header>
    <section class="privacy" aria-label="Privacy and verification guarantees">
      <div><strong>Local only</strong><span>Bound to 127.0.0.1</span></div>
      <div><strong>Verified senders</strong><span>DID identity matched</span></div>
      <div><strong>Durable facts</strong><span>Checked against SQLite</span></div>
    </section>
    <section class="panel" aria-label="Verified agent conversation">
      <div class="panel-head"><span>CLAUDE · CODEX · TECHNOCORE</span><span id="count">0 events</span></div>
      <div id="timeline" aria-live="polite"><div id="empty" class="empty">The viewer is ready. Messages will appear after the workflow starts.</div></div>
    </section>
  </main>
  <div id="closed" class="closed"><div><h2>Viewer closed</h2><p>You can close this browser tab.</p></div></div>
  <script nonce="{nonce}">
    const timeline = document.getElementById('timeline');
    const empty = document.getElementById('empty');
    const count = document.getElementById('count');
    const status = document.getElementById('status');
    const statusText = document.getElementById('statusText');
    const closeViewer = document.getElementById('closeViewer');
    let cursor = 0;
    let messageCount = 0;
    let stopped = false;

    function setStatus(value, terminal) {{
      const label = value.replaceAll('_', ' ');
      statusText.textContent = label.charAt(0).toUpperCase() + label.slice(1);
      status.className = terminal ? 'status done' : value === 'waiting_for_run' ? 'status' : 'status live';
    }}

    function createAgentLogo(agent) {{
      const namespace = 'http://www.w3.org/2000/svg';
      const icon = document.createElementNS(namespace, 'svg');
      icon.setAttribute('aria-hidden', 'true');
      icon.setAttribute('focusable', 'false');
      const use = document.createElementNS(namespace, 'use');
      use.setAttribute('href', agent === 'Claude' ? '#claudeLogo' : '#codexLogo');
      icon.append(use);
      return icon;
    }}

    function renderSystemEvent(entry) {{
      empty?.remove();
      const notice = document.createElement('div');
      notice.className = 'system-event';
      const label = document.createElement('strong');
      label.textContent = entry.sender === 'verifier' ? 'Verifier' : 'Workflow supervisor';
      const kind = document.createElement('span');
      kind.textContent = entry.kind.replaceAll('_', ' ');
      const text = document.createElement('span');
      text.className = 'system-text';
      text.textContent = entry.text;
      const time = document.createElement('time');
      time.dateTime = entry.created_at;
      time.textContent = new Date(entry.created_at).toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit', second: '2-digit'}});
      notice.append(label, document.createTextNode(' · '), kind, text, time);
      timeline.append(notice);
      notice.scrollIntoView({{block: 'end', behavior: 'smooth'}});
    }}

    function renderMessage(entry) {{
      empty?.remove();
      if (entry.sender === 'supervisor' || entry.sender === 'verifier') {{
        renderSystemEvent(entry);
        messageCount += 1;
        count.textContent = `${{messageCount}} event${{messageCount === 1 ? '' : 's'}}`;
        return;
      }}
      const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 90;
      const article = document.createElement('article');
      article.className = `message ${{entry.sender}}`;
      const avatar = document.createElement('div');
      avatar.className = 'avatar';
      avatar.append(createAgentLogo(entry.agent));
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      const meta = document.createElement('div');
      meta.className = 'meta';
      const who = document.createElement('strong');
      who.textContent = `${{entry.agent}} · ${{entry.sender}}`;
      const kind = document.createElement('span');
      kind.className = 'kind';
      kind.textContent = entry.kind.replaceAll('_', ' ');
      const time = document.createElement('time');
      time.dateTime = entry.created_at;
      time.textContent = new Date(entry.created_at).toLocaleTimeString([], {{hour: '2-digit', minute: '2-digit', second: '2-digit'}});
      const sequence = document.createElement('span');
      sequence.textContent = `#${{entry.sequence}}`;
      meta.append(who, kind, time, sequence);
      const body = document.createElement('p');
      body.className = 'text';
      body.textContent = entry.text;
      bubble.append(meta, body);
      article.append(avatar, bubble);
      timeline.append(article);
      messageCount += 1;
      count.textContent = `${{messageCount}} event${{messageCount === 1 ? '' : 's'}}`;
      if (nearBottom) article.scrollIntoView({{block: 'end', behavior: 'smooth'}});
    }}

    async function refresh() {{
      if (stopped) return;
      let delay = 1200;
      try {{
        const response = await fetch(`api/timeline?after=${{cursor}}`, {{cache: 'no-store'}});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error?.message || 'Timeline request failed.');
        setStatus(payload.state, payload.terminal);
        for (const entry of payload.entries) renderMessage(entry);
        cursor = payload.cursor;
        delay = payload.at_limit ? 0 : payload.terminal ? 5000 : 1200;
      }} catch (error) {{
        statusText.textContent = error instanceof Error ? error.message : 'Viewer connection failed';
        status.className = 'status error';
        delay = 2500;
      }}
      window.setTimeout(refresh, delay);
    }}

    closeViewer.addEventListener('click', async () => {{
      stopped = true;
      closeViewer.disabled = true;
      try {{ await fetch('api/close', {{method: 'POST'}}); }} catch {{}}
      document.getElementById('closed').classList.add('visible');
    }});

    refresh();
  </script>
</body>
</html>
"""


def _content_security_policy(nonce: str) -> str:
    return (
        "default-src 'none'; "
        f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
