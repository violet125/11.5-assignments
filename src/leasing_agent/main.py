"""服务入口：HTTP 服务 + 路由分发。"""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .api.routes import ApiError, GET_ROUTES, POST_ROUTES
from .security import PermissionError_
from .storage import connect, seed_spaces_if_empty

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / 'web'

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765

MAX_BODY_BYTES = 2 * 1024 * 1024


def _decode_header_value(value: str) -> str:
    """把请求头值还原为 UTF-8 文本。

    若值仍持有 latin-1 字节（常见于客户端直接写 UTF-8 字节），先转回字节再按
    UTF-8 解码；若已正确解码则原样返回。
    """
    if all(ord(char) < 256 for char in value):
        try:
            decoded = value.encode('latin-1').decode('utf-8')
            return decoded
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = 'LeasingAgent/1.0'

    def log_message(self, format: str, *args: Any) -> None:
        """默认不打印访问日志，避免淹没业务输出。设置 AGENT_ACCESS_LOG=1 可开启。"""
        if os.getenv('AGENT_ACCESS_LOG'):
            super().log_message(format, *args)

    def reply(self, status: int, data: Any, mime: str = 'application/json; charset=utf-8') -> None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        elif isinstance(data, str):
            body = data.encode('utf-8')
        else:
            body = str(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    # --- 静态文件 ---
    def _serve_static(self, path: str) -> None:
        if path in ('/', '/index.html'):
            target = WEB_DIR / 'index.html'
        else:
            candidate = (WEB_DIR / path.lstrip('/')).resolve()
            try:
                candidate.relative_to(WEB_DIR.resolve())
            except ValueError:
                self.reply(403, {'error': '路径越界'})
                return
            target = candidate
        if not target.is_file():
            self.reply(404, {'error': '未找到'})
            return
        mime = {
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
        }.get(target.suffix, 'application/octet-stream')
        self.reply(200, target.read_text(encoding='utf-8'), mime)

    def _headers_map(self) -> Dict[str, str]:
        """读取请求头，兼容两种中文编码来源。

        标准库解析 header 时可能保留 latin-1 字节（客户端直接写 UTF-8 字节），
        也可能已按 UTF-8 解码。这里两种都尝试，取能正确还原的那个。
        """
        result: Dict[str, str] = {}
        for key, value in self.headers.items():
            if not isinstance(value, str):
                value = str(value)
            result[key.lower()] = _decode_header_value(value)
        return result

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route in GET_ROUTES:
            try:
                payload = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                self.reply(200, GET_ROUTES[route](payload, self._headers_map()))
            except ApiError as exc:
                self.reply(exc.status, {'error': exc.message})
            except PermissionError_ as exc:
                self.reply(403, {'error': str(exc)})
            except KeyError as exc:
                self.reply(404, {'error': f'未找到：{exc}'})
            except Exception:
                traceback.print_exc()
                self.reply(500, {'error': '服务器内部错误'})
            return
        self._serve_static(route)

    def do_PUT(self) -> None:
        self._handle_write()

    def do_POST(self) -> None:
        self._handle_write()

    def _handle_write(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        handler = POST_ROUTES.get(route)
        if handler is None and self.command == 'PUT':
            handler = POST_ROUTES.get(route)
        if handler is None:
            self.reply(404, {'error': '未找到'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0:
                raise ApiError(400, '请求内容为空')
            if length > MAX_BODY_BYTES:
                raise ApiError(400, '请求内容超过 2MB')
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(400, '请求体不是合法 JSON') from exc
            if not isinstance(payload, dict):
                raise ApiError(400, '请求体必须为 JSON 对象')
            self.reply(200, handler(payload, self._headers_map()))
        except ApiError as exc:
            self.reply(exc.status, {'error': exc.message})
        except PermissionError_ as exc:
            self.reply(403, {'error': str(exc)})
        except ValueError as exc:
            self.reply(400, {'error': str(exc)})
        except KeyError as exc:
            self.reply(404, {'error': f'未找到：{exc}'})
        except Exception:
            traceback.print_exc()
            self.reply(500, {'error': '服务器内部错误'})


def build_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    connect()  # 初始化数据库
    seeded = seed_spaces_if_empty()
    if seeded:
        print(f'已载入虚构样例房源 {seeded} 条（演示数据，不可用于真实招商）')
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    host = os.getenv('AGENT_HOST', DEFAULT_HOST)
    port = int(os.getenv('AGENT_PORT', str(DEFAULT_PORT)))
    server = build_server(host, port)
    print(f'招商 Agent：http://{host}:{port}（仅本机访问）')
    print('提示：角色标识通过前端右上角选择，权限在后端校验。')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
