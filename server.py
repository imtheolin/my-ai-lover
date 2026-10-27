#!/usr/bin/env python3
"""
代理服务器 — 同源服务 HTML + 转发 /v1/chat/completions 到 LM Studio
               + WebSocket 代理到豆包端到端实时语音 API
--------------------------------------------------------------------------
使用： python3 server.py
然后浏览器访问 http://localhost:8080
"""

import http.server
import json
import asyncio
import struct
import hashlib
import base64
import threading
import os
import ssl
import time
import urllib.request
import urllib.error
import sys
import uuid

LM_STUDIO_URL = "http://localhost:1234"
DOUBAO_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    """同时提供静态文件服务和 API 代理"""

    def do_OPTIONS(self):
        """处理 CORS 预检（某些场景仍可能触发）"""
        self._cors_headers()
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/time":
            self._handle_time()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._proxy_to_lmstudio()
        elif self.path == "/tts":
            self._proxy_to_doubao_tts()
        else:
            self.send_error(404)

    def _proxy_to_lmstudio(self):
        """将 POST 请求转发到 LM Studio 并返回结果"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""

            target = f"{LM_STUDIO_URL}/v1/chat/completions"
            req = urllib.request.Request(
                target,
                data=body,
                headers={
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)

        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())

        except urllib.error.URLError as e:
            self._send_error(502, f"无法连接到 LM Studio: {e.reason}")

        except Exception as e:
            self._send_error(500, f"代理错误: {str(e)}")

    def _write_chunk(self, data):
        """写一个 HTTP chunk（data 为空表示结束块）"""
        if data:
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _proxy_to_doubao_tts(self):
        """将 POST /tts 转发到豆包单向流式语音合成 HTTP，流式返回（chunked）"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            data = json.loads(body) if body else {}
        except Exception:
            self._send_error(400, "请求体必须是 JSON")
            return

        text = (data.get("text") or "").strip()
        api_key = (data.get("api_key") or "").strip()
        if not text or not api_key:
            self._send_error(400, "缺少 text 或 api_key")
            return
        speaker = data.get("speaker") or "zh_female_vv_uranus_bigtts"
        resource_id = data.get("resource_id") or "seed-tts-2.0"
        context_texts = data.get("context_texts")

        payload = {
            "req_params": {
                "text": text,
                "speaker": speaker,
                "audio_params": {"format": "wav", "sample_rate": 24000},
            }
        }
        # 语音指令（语气/情绪），仅豆包语音合成模型2.0音色支持，不参与计费
        if context_texts:
            payload["req_params"]["context_texts"] = context_texts

        try:
            req = urllib.request.Request(
                DOUBAO_TTS_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Api-Key": api_key,
                    "X-Api-Resource-Id": resource_id,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context())

            # 流式转发：解析豆包的 JSON 流，每个完整 JSON 对象重新序列化为一行，chunked 编码发回
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self._cors_headers()
            self.end_headers()

            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                # 用 JSONDecoder.raw_decode 从缓冲中提取尽可能多的完整 JSON 对象
                while True:
                    text = buf.decode("utf-8", errors="replace")
                    stripped = text.lstrip(" \t\r\n")
                    if not stripped:
                        buf = b""
                        break
                    try:
                        obj, end = json.JSONDecoder().raw_decode(stripped)
                    except json.JSONDecodeError:
                        # 对象不完整，等更多数据；若迟迟无法解析，打印原始内容便于排查
                        if len(stripped) > 200:
                            print(f"🎙️ [tts] ⚠️ 无法解析的流片段(前200): {stripped[:200]!r}", flush=True)
                        break
                    # 已解析 end 个字符（从 stripped 起点算），重新编码干净输出
                    data_val = obj.get("data") or ""
                    print(f"🎙️ [tts] 转发对象: code={obj.get('code')!r}, message={obj.get('message')!r}, data_len={len(data_val)}, data头={str(data_val)[:40]!r}", flush=True)
                    self._write_chunk(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
                    buf = stripped[end:].encode("utf-8")
                    if not buf:
                        break
            # 结尾再尝试解析一次（可能刚好有完整对象在末尾）
            text = buf.decode("utf-8", errors="replace")
            stripped = text.lstrip(" \t\r\n")
            if stripped:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(stripped)
                    self._write_chunk(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
                except json.JSONDecodeError:
                    pass
            self._write_chunk(b"")  # 结束块

        except urllib.error.HTTPError as e:
            err = e.read().decode(errors="replace")[:500]
            self._send_error(e.code, f"豆包 TTS 错误: {err}")
        except Exception as e:
            self._send_error(502, f"豆包 TTS 转发失败: {str(e)}")

    def _send_error(self, code, message):
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"error": {"message": message}}).encode())

    def _handle_time(self):
        """返回服务器当前时间戳（毫秒），用于前端校准浏览器节流引起的延迟"""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"server_time": int(time.time() * 1000)}).encode())

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")


# ═══════════════════════════════════════════════════════════════
#  WebSocket 代理 — 豆包端到端实时语音 API
# ═══════════════════════════════════════════════════════════════
# 浏览器 → 本地服务器（简单JSON协议）→ 豆包API（二进制协议）
# 需要安装: pip install websockets
# ═══════════════════════════════════════════════════════════════

DOUBAO_WS_URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"

async def doubao_voice_proxy(websocket):
    """接收浏览器的 WebSocket 连接，代理到豆包实时语音 API"""
    import struct as _struct
    import hashlib as _hashlib
    import base64 as _base64
    import uuid as _uuid

    client_ip = websocket.remote_address
    print(f"🎙️ [proxy] 新客户端连接: {client_ip}")

    try:
        import websockets as ws_lib
    except ImportError:
        print(f"🎙️ [proxy] 缺少 websockets 库")
        await websocket.send(json.dumps({"type": "error", "message": "服务端缺少 websockets 库，请运行: pip install websockets"}))
        return

    # 1) 从浏览器接收认证信息
    print(f"🎙️ [proxy] 等待认证信息...")
    try:
        auth_msg = await asyncio.wait_for(websocket.recv(), timeout=15)
    except asyncio.TimeoutError:
        print(f"🎙️ [proxy] 认证超时")
        await websocket.send(json.dumps({"type": "error", "message": "认证超时"}))
        return
    except Exception as e:
        print(f"🎙️ [proxy] 接收认证信息失败: {e}")
        return

    try:
        auth = json.loads(auth_msg)
    except:
        print(f"🎙️ [proxy] 认证信息格式错误: {auth_msg[:100]}")
        await websocket.send(json.dumps({"type": "error", "message": "认证信息格式错误"}))
        return

    app_id = auth.get("app_id", "")
    access_key = auth.get("access_key", "")
    print(f"🎙️ [proxy] 收到认证: app_id={app_id[:4]}..., access_key={'已填' if access_key else '空'}")
    if not app_id or not access_key:
        await websocket.send(json.dumps({"type": "error", "message": "缺少 App ID 或 Access Key"}))
        return

    # 2) 额外配置
    persona_raw = auth.get("persona_raw", "")
    bot_name = auth.get("bot_name", "神秘网友")
    speaker = auth.get("speaker", "zh_female_vv_jupiter_bigtts")
    speaking_style = auth.get("speaking_style", "语气自然。")
    chat_history = auth.get("chat_history", [])
    rules_content = auth.get("rules_content", "")
    full_persona = auth.get("full_persona", persona_raw)
    diary_content = auth.get("diary_content", "")
    current_time = auth.get("current_time", "")
    call_context = auth.get("call_context", "")

    # 3) 使用 websockets 库连接豆包 API
    print(f"🎙️ [proxy] 正在连接豆包 API...")
    try:
        import ssl as _ssl
        # websockets v12 兼容 Python 3.13
        import websockets as ws_lib

        extra_headers = {
            "X-Api-App-ID": app_id,
            "X-Api-Access-Key": access_key,
            "X-Api-Resource-Id": "volc.speech.dialog",
            "X-Api-App-Key": "PlgvMymc7f3tQnJ6",
        }
        ssl_ctx = _ssl._create_unverified_context()
        doubao = await ws_lib.connect(
            DOUBAO_WS_URL,
            extra_headers=extra_headers,
            ssl=ssl_ctx,
            max_size=10 * 1024 * 1024,
        )
    except Exception as e:
        error_msg = str(e)[:300]
        print(f"🎙️ [proxy] 连接豆包API失败: {error_msg}")
        await websocket.send(json.dumps({
            "type": "error",
            "message": f"连接豆包API失败: {error_msg}\n\n提示：请确认已在火山引擎控制台开通端到端实时语音服务，并检查网络连接。"
        }))
        return

    await websocket.send(json.dumps({"type": "connected"}))
    print(f"🎙️ [proxy] 豆包API连接成功，发送 StartSession...")

    # 二进制协议辅助函数（v2 API 备用）
    def make_text_frame(event_id, payload_dict, session_id=None):
        payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        header = bytearray([0x11, 0x14, 0x10, 0x00])
        optional = bytearray()
        # Event: uint32 big-endian (4 bytes)
        optional += _struct.pack(">I", event_id)
        if session_id:
            sid_bytes = session_id.encode("ascii")
            optional += _struct.pack(">I", len(sid_bytes))
            optional += sid_bytes
        optional += _struct.pack(">I", len(payload_bytes))
        optional += payload_bytes
        return bytes(header) + bytes(optional)

    def parse_response_frame(data):
        if len(data) < 4:
            return None
        flags = data[1] & 0x0F
        offset = 4
        seq_flag = flags & 0x03
        if seq_flag != 0:
            offset += 4
        if flags & 0x04:
            if offset + 4 > len(data):
                return None
            event_id = _struct.unpack(">I", data[offset:offset+4])[0]
            offset += 4
        else:
            event_id = None
        # 豆包服务端在响应帧中总是包含 session_id，不限于 100-199 事件
        if offset + 4 <= len(data):
            sid_len = _struct.unpack(">I", data[offset:offset+4])[0]
            # 检查 sid_len 是否合理（1~100字节的文本）
            if 1 <= sid_len <= 100 and offset + 4 + sid_len <= len(data):
                offset += 4 + sid_len
            # 不合理就当没有 session_id
        if offset + 4 > len(data):
            return None
        payload_size = _struct.unpack(">I", data[offset:offset+4])[0]
        offset += 4
        payload = data[offset:offset+payload_size] if payload_size > 0 else b""
        return {"event_id": event_id, "msg_type": (data[1] >> 4) & 0x0F, "payload": payload}

    # 发送 StartSession（使用二进制协议）
    session_id = str(_uuid.uuid4())

    # 构建 system_role：使用与 DeepSeek 文字聊天完全相同的 <system_rules> 模板
    now_str = current_time or "未知时间"

    website_info = (
        '1.网站的名称是"遇见平行时空的你"。\n'
        '2.这是一个随机匹配聊天网站，你匹配到人来自另一个平行时空的地球。\n'
    )
    worldview = (
        '1. 你和匹配到的人不在同一个时空，所以不要聊"见面"、"买东西"、"点外卖"之类的事情。\n'
    )

    # 聊天记录按 role/content 格式
    history_lines = []
    if chat_history:
        for m in chat_history[-15:]:
            role_label = "user" if m["role"] == "user" else "assistant"
            history_lines.append(f"role={role_label}\ncontent={m['content'][:500]}")
    history_text = "\n".join(history_lines)

    # 构建 dialog_context（API 文档：user/assistant QA 对顺序，数组长度必须为偶数；timestamp 为空会自动补当前时间）
    dialog_context = []
    for m in (chat_history or [])[-12:]:
        role = "user" if m.get("role") == "user" else "assistant"
        text = (m.get("content") or "")[:500]
        if not text:
            continue
        # 合并连续同角色消息，保证严格 user/assistant 交替
        if dialog_context and dialog_context[-1]["role"] == role:
            dialog_context[-1]["text"] += "\n" + text
        else:
            dialog_context.append({"role": role, "text": text})
    dialog_context = dialog_context[-10:]          # 只取最近 10 条
    if dialog_context and dialog_context[0]["role"] != "user":
        dialog_context = dialog_context[1:]        # 必须从 user 开始
    if len(dialog_context) % 2 != 0:
        dialog_context = dialog_context[:-1]       # 保证偶数（QA 成对）

    system_role_text = (
        # f"<system_rules>\n\n"
        # f"按照你的人物设定和规则回复即可。\n\n"
        # f"<action>你正在玩一个随机匹配聊天网站。</action>\n\n"
        f"我给你打了电话，现在电话接通了"
        # f"当前时间：{now_str}。\n\n"
        # f"在这个对话中，你之后所有的回答都要严格遵守以下设定和规则：\n\n"
        # f"<action>你正在玩一个随机匹配聊天网站，该网站的信息如下：</action>\n\n"
        # f"<website_info>\n\n{website_info}\n</website_info>\n\n"
        # 请根据你的人物设定、当前时间、你们的聊天记录和对方聊天。\n\n"
        # f"你讲话的声音要符合你人物设定里的【声音特征】\n\n"
        # f"<yourself>\n\n{full_persona[:4000]}\n\n</yourself>\n\n"
        # f"<hippocampus>\n\n{diary_content[:2000]}\n\n</hippocampus>\n\n"
        # f"{rules_content[:3000]}\n\n"
        # f"</system_rules>"
    )
    # 通话记录文字作为上下文（放在 system 指令最后面，不占用指令主体；内容格式由客户端构建好）
    # if call_context:
    #     system_role_text += f"\n\n{call_context[:3000]}"
    # if history_text:
    #     system_role_text += f"\n\n{history_text[:100]}"

    session_payload = {
        "asr": {"audio_info": {"format": "pcm", "sample_rate": 16000, "channel": 1}},
        "dialog": {"bot_name": bot_name, "system_role": system_role_text[:700], "speaking_style": speaking_style[:500], "dialog_id": "", "dialog_context": dialog_context, "extra": {"model": "O2.0", "enable_music": False,}},
        "tts": {"speaker": speaker, "audio_config": {"format": "pcm_s16le", "sample_rate": 24000, "channel": 1}}
    }

    try:
        print(f"\n{'='*60}", flush=True)
        print(f"🎙️ [proxy] 📤 StartSession 完整 payload:", flush=True)
        print(f"{'='*60}", flush=True)
        print(json.dumps(session_payload, ensure_ascii=False, indent=2), flush=True)
        print(f"{'='*60}\n", flush=True)

        bin_frame = make_text_frame(100, session_payload, session_id)
        await doubao.send(bin_frame)
        print(f"🎙️ [proxy] 已发送二进制 StartSession")

        resp = await asyncio.wait_for(doubao.recv(), timeout=10)
        parsed = parse_response_frame(resp)
        print(f"🎙️ [proxy] 收到响应: event_id={parsed.get('event_id') if parsed else None}")

        if parsed and parsed["event_id"] == 150:
            await websocket.send(json.dumps({"type": "session_started"}))
            print(f"🎙️ [proxy] 会话已建立")
        elif parsed and parsed["event_id"] == 153:
            err = json.loads(parsed["payload"]) if parsed["payload"] else {"error": "未知错误"}
            await websocket.send(json.dumps({"type": "error", "message": f"会话启动失败: {err}"}))
            await doubao.close()
            return
        elif parsed and parsed.get("payload"):
            # 可能是错误帧
            try:
                err_text = parsed["payload"].decode() if isinstance(parsed["payload"], bytes) else str(parsed["payload"])
                await websocket.send(json.dumps({"type": "error", "message": f"API错误: {err_text[:300]}"}))
            except:
                await websocket.send(json.dumps({"type": "error", "message": f"未知API响应 (event={parsed.get('event_id')})"}))
            await doubao.close()
            return
        else:
            await websocket.send(json.dumps({"type": "error", "message": f"意外的响应: event={parsed.get('event_id') if parsed else 'none'}"}))
            await doubao.close()
            return

    except asyncio.TimeoutError:
        await websocket.send(json.dumps({"type": "error", "message": "启动会话超时"}))
        await doubao.close()
        return
    except Exception as e:
        await websocket.send(json.dumps({"type": "error", "message": f"会话启动失败: {str(e)[:200]}"}))
        await doubao.close()
        return

    # 成功启动会话，进入双向代理模式
    print(f"🎙️ [proxy] 会话已建立，开始双向代理")
    closed = False
    _frame_dumped = False  # 调试用

    def safe_json_load(data):
        """安全解析 JSON payload，处理可能多余的尾随字节"""
        if not data:
            return None
        # json.loads 和 raw_decode 需要 str，不是 bytes
        if isinstance(data, bytes):
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                return None
        else:
            text = data
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试 raw_decode 找到第一个 JSON 对象的结束位置
        try:
            decoder = json.JSONDecoder()
            obj, pos = decoder.raw_decode(text)
            if pos < len(text):
                print(f"🎙️ [proxy] ⚠️ JSON 后有多余数据: {len(text)-pos} bytes")
            return obj
        except json.JSONDecodeError:
            return None

    async def proxy_to_doubao():
        """浏览器 → 豆包"""
        nonlocal closed
        try:
            while not closed:
                msg = await websocket.recv()
                if isinstance(msg, str):
                    cmd = json.loads(msg)
                    cmd_type = cmd.get("type", "")
                    print(f"🎙️ [proxy] 浏览器→豆包: {cmd_type}")

                    if cmd_type == "audio":
                        import base64 as _b64
                        audio_bytes = _b64.b64decode(cmd["data"])
                        print(f"🎙️ [proxy] 转发音频: {len(audio_bytes)} bytes")
                        hdr = bytearray([0x11, 0x24, 0x00, 0x00])
                        opts = bytearray()
                        opts += _struct.pack(">I", 200)
                        sid_bytes = session_id.encode("ascii")
                        opts += _struct.pack(">I", len(sid_bytes))
                        opts += sid_bytes
                        opts += _struct.pack(">I", len(audio_bytes))
                        opts += audio_bytes
                        await doubao.send(bytes(hdr) + bytes(opts))

                    elif cmd_type == "say_hello":
                        # say_hello = 强制朗读：content 是 AI 开场白，原样 TTS 念出来
                        hello_payload = {"content": cmd.get("content", "")}
                        frame = make_text_frame(300, hello_payload, session_id)
                        await doubao.send(frame)

                    elif cmd_type == "chat_text_query":
                        # 沉默时主动找话题：把文本作为用户 query 发给豆包（事件 501），让模型生成回复
                        print(f"🎙️ [proxy] 文本 query (event 501): {cmd.get('content', '')[:60]}")
                        frame = make_text_frame(501, {"content": cmd.get("content", "")}, session_id)
                        await doubao.send(frame)

                    elif cmd_type == "finish_session":
                        frame = make_text_frame(102, {}, session_id)
                        await doubao.send(frame)
                        closed = True

                    elif cmd_type == "finish_connection":
                        frame = make_text_frame(2, {}, None)
                        await doubao.send(frame)
                        closed = True

        except Exception as e:
            print(f"🎙️ [proxy] proxy_to_doubao 异常: {e}")
            if not closed:
                closed = True

    async def proxy_from_doubao():
        """豆包 → 浏览器"""
        nonlocal closed
        try:
            while not closed:
                resp = await doubao.recv()
                # 首次收到非建立帧时 dump 原始数据格式（调试用）
                nonlocal _frame_dumped
                if not _frame_dumped and len(resp) > 40:
                    _frame_dumped = True
                    print(f"🎙️ [proxy] 🔬 原始帧 dump: {len(resp)} bytes")
                    print(f"🎙️ [proxy] 🔬   head: {resp[:8].hex(' ')}")
                    print(f"🎙️ [proxy] 🔬   body: {resp[:80].hex(' ')}")
                parsed = parse_response_frame(resp)
                if not parsed:
                    print(f"🎙️ [proxy] 收到无法解析的数据: {len(resp)} bytes")
                    continue

                event_id = parsed["event_id"]
                payload = parsed["payload"]
                print(f"🎙️ [proxy] 豆包→浏览器: event_id={event_id}, payload_size={len(payload)}")
                if event_id and event_id >= 500 and len(payload) < 200 and event_id != 550 and event_id != 559:
                    print(f"🎙️ [proxy] 🔍 event={event_id} raw hex: {payload[:100].hex(' ')}")
                    try:
                        print(f"🎙️ [proxy] 🔍 event={event_id} raw text: {payload[:200].decode('utf-8', errors='replace')}")
                    except:
                        pass

                if event_id == 352:  # TTSResponse - 音频数据
                    b64_data = base64.b64encode(payload).decode("ascii")
                    await websocket.send(json.dumps({"type": "audio", "data": b64_data, "format": "pcm_s16le"}))
                    print(f"🎙️ [proxy] 发送 TTS 音频: {len(payload)} bytes")
                elif event_id == 350:  # TTSSentenceStart
                    info = safe_json_load(payload) or {}
                    await websocket.send(json.dumps({"type": "sentence_start", "text": info.get("text", "")}))
                elif event_id == 359:  # TTSEnded
                    info = safe_json_load(payload) or {}
                    await websocket.send(json.dumps({"type": "tts_ended", "status_code": info.get("status_code", "")}))
                elif event_id == 451:  # ASRResponse
                    info = safe_json_load(payload) or {}
                    for r in info.get("results", []):
                        if not r.get("is_interim"):
                            print(f"🎙️ [proxy] ASR: {r.get('text', '')}")
                            await websocket.send(json.dumps({"type": "asr_text", "text": r.get("text", "")}))
                elif event_id == 450:  # ASRInfo - 用户开始说话
                    print(f"🎙️ [proxy] 用户开始说话")
                    await websocket.send(json.dumps({"type": "user_speaking"}))
                elif event_id == 550:  # ChatResponse
                    info = safe_json_load(payload) or {}
                    print(f"🎙️ [proxy] 聊天回复: {info.get('content', '')[:50]}")
                    await websocket.send(json.dumps({"type": "chat_text", "content": info.get("content", "")}))
                elif event_id == 559:  # ChatEnded
                    print(f"🎙️ [proxy] 聊天结束")
                    await websocket.send(json.dumps({"type": "chat_ended"}))
                elif event_id == 152:  # SessionFinished
                    print(f"🎙️ [proxy] 会话结束")
                    await websocket.send(json.dumps({"type": "session_finished"}))
                    closed = True
                elif event_id == 153:  # SessionFailed
                    err = safe_json_load(payload) or {"raw": payload[:200].decode(errors='replace')}
                    print(f"🎙️ [proxy] 会话失败: {err}")
                    await websocket.send(json.dumps({"type": "error", "message": str(err)}))
                    closed = True
                elif event_id == 599:  # DialogCommonError
                    raw_text = payload.decode('utf-8', errors='replace') if isinstance(payload, bytes) else str(payload)
                    err = safe_json_load(payload) or {"raw": raw_text}
                    print(f"🎙️ [proxy] 对话错误: {err}")
                    await websocket.send(json.dumps({"type": "error", "message": f"对话错误: {err.get('message', err.get('raw', raw_text[:200]))}"}))
                    closed = True
                elif event_id == 52:  # ConnectionFinished
                    print(f"🎙️ [proxy] 连接结束")
                    closed = True
                else:
                    print(f"🎙️ [proxy] 未处理的事件: event_id={event_id}")

        except Exception as e:
            print(f"🎙️ [proxy] proxy_from_doubao 异常: {e}")
            if not closed:
                closed = True

    # 10) 并发运行双向代理
    try:
        print(f"🎙️ [proxy] 开始双向代理...")
        await asyncio.gather(
            proxy_to_doubao(),
            proxy_from_doubao(),
        )
        print(f"🎙️ [proxy] 双向代理结束")
    except Exception as e:
        print(f"🎙️ [proxy] 双向代理异常: {type(e).__name__}: {e}")
    finally:
        try:
            await doubao.close()
        except:
            pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    ws_port = port + 1  # WebSocket 代理端口

    # 启动 HTTP 服务器（在 daemon 线程中运行，多线程支持并发）
    http_server = http.server.ThreadingHTTPServer(("0.0.0.0", port), ProxyHandler)

    print(f"🚀 HTTP 本地服务器运行在 http://localhost:{port}")
    print(f"📁 静态文件目录: {os.getcwd()}")
    print(f"🔁 /v1/chat/completions -> {LM_STUDIO_URL}/v1/chat/completions")
    print(f"🎙️  语音通话端口: {ws_port} (需要 websockets 库)")
    print(f"   运行 pip install websockets 启用语音通话")
    print("")
    print("📌 域名 + HTTPS 部署（宝塔 Nginx 反向代理）:")
    print(f"   1) HTTP: 站点反代目标 http://127.0.0.1:{port}")
    print(f"   2) WebSocket: Nginx location /ws -> http://127.0.0.1:{ws_port} (需 Upgrade 头)")
    print("   3) 前端会自动用 wss://域名/ws 连接语音")
    print("按 Ctrl+C 停止")

    import threading as _threading

    def run_http():
        try:
            http_server.serve_forever()
        except Exception:
            pass

    _threading.Thread(target=run_http, daemon=True).start()

    # 尝试启动 WebSocket 服务器（需要 websockets 库）
    try:
        import websockets as _ws_lib
        ws_ver = tuple(int(x) for x in _ws_lib.__version__.split('.')[:2])
        if ws_ver >= (13, 0):
            print(f"⚠️  websockets {_ws_lib.__version__} 可能与 Python 3.13 不兼容")
            print(f"   建议安装 v12: pip3 install websockets==12.0")

        import asyncio as _asyncio

        async def run_ws():
            print(f"🎙️  语音 WebSocket 服务器启动中 (端口 {ws_port})...")
            try:
                ws_server = await _ws_lib.serve(
                    doubao_voice_proxy,
                    "0.0.0.0",
                    ws_port,
                    max_size=10 * 1024 * 1024,
                )
                print(f"🎙️  语音通话已就绪: ws://localhost:{ws_port}")
                print(f"   API 地址: {DOUBAO_WS_URL}")
                print(f"   如连接失败，请在火山引擎控制台确认正确的 WebSocket 地址")
                await ws_server.wait_closed()
            except OSError as e:
                print(f"⚠️  语音通话端口 {ws_port} 被占用: {e}")

        _asyncio.run(run_ws())
    except ImportError:
        print("⚠️  未安装 websockets 库，语音通话功能不可用")
        print("   运行以下命令安装: pip install websockets")
    except KeyboardInterrupt:
        print("\n服务器已停止")
    finally:
        http_server.server_close()
