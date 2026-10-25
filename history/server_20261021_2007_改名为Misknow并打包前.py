# -*- coding: utf-8 -*-
"""
《一百枚金币》本地代理 —— 对接 Ollama 本地大模型
依赖：仅标准库，无需 pip install

用法：
    1. 双击「启动游戏.bat」即可（会自动拉起 Ollama 并打开浏览器）
    2. 或者手动运行：python server.py          (默认端口 8000)
                    python server.py 8080    (自定义端口)
"""

import os
import sys
import re
import json
import time
import http.client
import threading
import webbrowser
import subprocess
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# 玩家告别语（服务端兜底判定，模型不听话也能正确收尾）
FAREWELL_RE = re.compile(
    r"拜拜|再见|走了|告辞|回见|下次聊|以后再聊|886|晚安|不聊了|先走|得走了|先撤|回头见"
)


def _extract_json(text):
    """容错解析模型输出：模型常混用全角引号、带 markdown 围栏或尾部杂质。"""
    if not text:
        return None
    s = text.strip()
    # 模型经常把 JSON 的引号写成中文全角引号，先归一化
    norm = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    for cand in (s, norm):
        try:
            return json.loads(cand)
        except Exception:
            pass
    # 去掉 ```json ... ``` 围栏
    for cand in (s, norm):
        s2 = re.sub(r"^```[a-zA-Z]*\s*", "", cand)
        s2 = re.sub(r"\s*```$", "", s2).strip()
        try:
            return json.loads(s2)
        except Exception:
            pass
    # 截取第一个 { 到最后一个 } 之间的内容
    for cand in (s, norm):
        i, j = cand.find("{"), cand.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(cand[i:j + 1])
            except Exception:
                pass
    return None


# ---------- 交易引擎 ----------
# 商品：砖头 / 水泥 / 安全帽 / 手脚架 / 橘子糖 / 香烟 / 白酒。价格与结算全部由服务端计算，AI 只出台词。
_GOODS = {
    'brick': '砖头', 'cement': '水泥', 'helmet': '安全帽', 'scaffold': '手脚架',
    'candy': '橘子糖', 'cigarette': '香烟', 'wine': '白酒'
}
_RE_BUY = re.compile(r'买|进货|采购|要货|来点|拿点|多进|囤|批发')
_RE_SELL = re.compile(r'卖|出货|出售|卖点|卖货|变现|出手')


def _parse_trade_intent(text):
    """从玩家输入中解析交易意图，返回 (direction, item, qty) 或 None。"""
    if not text:
        return None
    if _RE_BUY.search(text):
        direction = 'buy'
    elif _RE_SELL.search(text):
        direction = 'sell'
    else:
        return None
    if re.search(r'砖|砖头|砖块', text):
        item = 'brick'
    elif re.search(r'水泥', text):
        item = 'cement'
    elif re.search(r'安全帽|帽子|头盔', text):
        item = 'helmet'
    elif re.search(r'手脚架|脚手架|架子', text):
        item = 'scaffold'
    elif re.search(r'橘子糖|桔子糖|糖果|糖', text):
        item = 'candy'
    elif re.search(r'香烟|烟', text):
        item = 'cigarette'
    elif re.search(r'白酒|酒', text):
        item = 'wine'
    else:
        return None
    m = re.search(r'(\d{1,3})', text)
    qty = int(m.group(1)) if m else 1
    return direction, item, max(1, min(99, qty))


def _resolve_trade(profile, player, trade_input, text):
    """计算交易结果。trade_input 为前端传来的结构化交易（砍价阶段带 offer）。"""
    tprofile = profile.get('trade') or {}
    direction = item = qty = offer = None
    stage = 'quote'
    if isinstance(trade_input, dict):
        direction = trade_input.get('direction')
        item = trade_input.get('item')
        qty = trade_input.get('qty')
        offer = trade_input.get('offer')
        stage = trade_input.get('stage') or 'quote'

    # 没有结构化 direction/item（初次报价）→ 从文本解析
    if not direction or not item:
        intent = _parse_trade_intent(text or '')
        if not intent:
            return None
        direction, item, qty = intent
        stage = 'quote'

    try:
        qty = int(qty) if qty is not None else 1
    except (TypeError, ValueError):
        qty = 1
    qty = max(1, min(99, qty))

    pool = (tprofile.get('sells') if direction == 'buy' else tprofile.get('buys')) or {}
    good = pool.get(item)
    if not good:
        # 该 NPC 不卖/不收这个东西
        return {'action': 'none', 'status': 'unavailable',
                'direction': direction, 'item': item,
                'itemName': _GOODS.get(item, item)}

    base = int(good.get('base', 0) or 0)
    item_name = good.get('name') or _GOODS.get(item, item)
    trust = float(player.get('trust', 0) or 0)

    # 定价：买越信任越便宜，卖越信任越贵（好感度越高，越容易砍到好价）
    if direction == 'buy':
        raw = int(round(base * (1 - trust * 0.04)))
        lo = int(round(base * 0.6))
        hi = int(round(base * 1.2))
    else:
        raw = int(round(base * (1 + trust * 0.04)))
        lo = int(round(base * 0.8))
        hi = int(round(base * 1.4))
    unit = max(lo, min(hi, raw))

    if stage == 'offer':
        try:
            offer = int(offer)
        except (TypeError, ValueError):
            offer = None
        if offer is not None and lo <= offer <= hi:
            total = offer * qty
            if direction == 'buy':
                if player.get('money', 0) < total:
                    return {'action': 'reject', 'status': 'insufficient',
                            'direction': direction, 'item': item, 'itemName': item_name,
                            'qty': qty, 'ask': unit, 'offer': offer, 'total': total,
                            'min': lo, 'max': hi}
                money_delta = -total
                bag_delta = qty
            else:
                bag = player.get('bag') or {}
                if int(bag.get(item, 0) or 0) < qty:
                    return {'action': 'reject', 'status': 'insufficient',
                            'direction': direction, 'item': item, 'itemName': item_name,
                            'qty': qty, 'ask': unit, 'offer': offer, 'total': total,
                            'min': lo, 'max': hi}
                money_delta = total
                bag_delta = -qty
            return {'action': 'accept', 'status': 'accepted',
                    'direction': direction, 'item': item, 'itemName': item_name,
                    'qty': qty, 'unitPrice': offer, 'total': total,
                    'moneyDelta': money_delta, 'bagDelta': {item: bag_delta},
                    'min': lo, 'max': hi}
        # 出价落在允许区间外 → NPC 反出一个价，而不是直接拒绝
        counter = lo if (offer is not None and offer < lo) else hi
        return {'action': 'counter', 'status': 'countered',
                'direction': direction, 'item': item, 'itemName': item_name,
                'qty': qty, 'ask': unit, 'offer': offer, 'counter': counter,
                'min': lo, 'max': hi}

    # 报价阶段
    return {'action': 'quote', 'status': 'quoted',
            'direction': direction, 'item': item, 'itemName': item_name,
            'qty': qty, 'ask': unit, 'min': lo, 'max': hi}


def _canned_trade_reply(trade):
    """交易回合的兜底台词：模型一旦在台词里报数字，就用这句替换，保证绝不乱报价。"""
    action = trade.get('action')
    direction = trade.get('direction')
    if action == 'accept':
        return '行，就按这个数，成交！'
    if action == 'counter':
        return '这个价我做不了，我报个实在的，你看行不行。'
    if action == 'reject':
        return '你钱不够啊，回去再攒攒。' if direction == 'buy' else '你手里没这么多货。'
    if action == 'quote':
        return '进货是吧？我这儿有货，这个价你看合不合适。' if direction == 'buy' \
            else '你要出货？这价我收了。'
    return None


# ---------- 云端：OpenAI 兼容 /chat/completions ----------
# 这两个值配在一起是为了保证：云端再怎么不稳定，也一定能赶在前端 150 秒上限之前把本地 Ollama 接上。
CLOUD_TIMEOUT = 30   # 云端单次请求超时（秒）
CLOUD_BUDGET = 35    # 单回合留给云端的「总时长」上限（秒）：超了就立刻放弃云端、转本地


def _mask_key(k):
    """日志里永远不要打印完整 API Key，只露头尾。"""
    k = (k or '').strip()
    if not k:
        return '(空)'
    if len(k) <= 10:
        return '***'
    return k[:6] + '…' + k[-4:]


def _chat_url_candidates(base_url):
    """把玩家填的 Base URL 归一化成若干个待试地址。
    最常见的连不上原因：只填了域名（如 https://api.openai.com），
    少了 /v1/chat/completions 这一段，必然 404 —— 这里自动补齐。"""
    b = (base_url or '').strip().rstrip('/')
    if not b:
        return []
    if b.endswith('/chat/completions'):
        return [b]
    cands = [b + '/chat/completions']
    # 路径里还没有版本段（/v1、/compatible-mode/v1 …）→ 再补一个带 /v1 的
    if re.search(r'/(v\d+|compatible-mode/v\d+|openai)', b) is None:
        cands.append(b + '/v1/chat/completions')
    seen, out = set(), []
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _call_openai_compatible(cloud, messages, temperature=0.75, num_predict=220):
    """把 messages 转发给玩家自填的云端大模型（OpenAI 兼容协议）。
    兼容 DeepSeek / 通义 / Kimi / OpenAI 等。
    返回 (content, used_url)；全部尝试都失败时抛 RuntimeError（内含最后一次的真实错误）。"""
    import urllib.request
    import urllib.error

    base = (cloud.get('baseUrl') or '').strip()
    key = (cloud.get('apiKey') or '').strip()
    model = (cloud.get('model') or '').strip()
    if not base:
        raise ValueError('云端配置缺少 Base URL')
    if not model:
        raise ValueError('云端配置缺少模型名（model）')

    urls = _chat_url_candidates(base)
    common = {"model": model, "messages": messages, "temperature": temperature}
    payloads = [
        dict(common, max_tokens=num_predict, response_format={"type": "json_object"}),
        dict(common, max_tokens=num_predict),                 # 有些服务商不认 response_format
        dict(common, max_completion_tokens=num_predict),       # o1/o3 这类推理模型只认这个字段
        # deepseek-reasoner / o1 这类推理模型不接受 temperature、response_format
        # （带了会直接 400），所以单独留一份"什么都不带"的最小载荷
        {"model": model, "messages": messages, "max_tokens": num_predict},
    ]
    payload_desc = ['json_object+max_tokens', 'max_tokens', 'max_completion_tokens', '无temperature/无response_format']

    sys.stderr.write("[cloud] 调用：model=%s key=%s timeout=%ds\n" % (model, _mask_key(key), CLOUD_TIMEOUT))
    sys.stderr.write("[cloud] 候选地址：%s\n" % ' | '.join(urls))
    if not key:
        sys.stderr.write("[cloud] 警告：API Key 为空，多数服务商会返回 401\n")
    try:
        proxies = urllib.request.getproxies()
        if proxies:
            sys.stderr.write("[cloud] 检测到系统代理：%s（若连不上可关掉代理或设 NO_PROXY）\n" % proxies)
    except Exception:
        pass

    headers = {'Content-Type': 'application/json', 'User-Agent': 'coins-of-100/1.0'}
    if key:
        headers['Authorization'] = 'Bearer ' + key

    last_err = None
    deadline = time.time() + CLOUD_BUDGET     # 单回合留云端的"总时长"上限，超了就放弃云端转本地
    for url in urls:
        for i, payload in enumerate(payloads):
            if time.time() > deadline:
                raise RuntimeError('云端尝试已超过 %d 秒上限，放弃云端转本地：%s'
                                   % (CLOUD_BUDGET, str(last_err) if last_err else '未知错误'))
            sys.stderr.write("[cloud] -> POST %s  (载荷=%s)\n" % (url, payload_desc[i]))
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                    headers=headers, method='POST')
                with urllib.request.urlopen(request, timeout=CLOUD_TIMEOUT) as resp:
                    raw = resp.read().decode('utf-8', 'replace')
                    status = getattr(resp, 'status', None) or resp.getcode()
                    sys.stderr.write("[cloud] <- HTTP %s，%d 字节\n" % (status, len(raw)))
                    data = json.loads(raw)
            except urllib.error.HTTPError as e:
                body = ''
                try:
                    body = e.read().decode('utf-8', 'replace')
                except Exception:
                    pass
                msg = body[:300].replace('\n', ' ')
                try:                       # 优先取服务商给的 error.message，比原文更好读
                    err = (json.loads(body) or {}).get('error') or {}
                    if isinstance(err, dict) and err.get('message'):
                        msg = str(err.get('message'))
                except Exception:
                    pass
                sys.stderr.write("[cloud] <- HTTPError %s：%s\n" % (e.code, msg))
                last_err = RuntimeError('HTTP %s：%s' % (e.code, msg))
                # 只有 400 才是"载荷参数不被支持"，换个载荷可能就通了；
                # 认证错/地址错/限流/服务端 5xx/欠费(402)，换载荷没意义，直接换下一个候选地址
                if e.code == 400:
                    continue
                break
            except urllib.error.URLError as e:
                reason = getattr(e, 'reason', e)
                sys.stderr.write("[cloud] <- URLError：%s\n" % reason)
                # 网络层就断了（超时/拒连/DNS 失败）：换地址、换载荷都一样连不上，直接放弃云端转本地
                raise RuntimeError('网络错误：%s' % reason)
            except Exception as e:
                sys.stderr.write("[cloud] <- 异常：%r\n" % e)
                raise RuntimeError('%s: %s' % (type(e).__name__, e))

            choices = data.get('choices') or []
            if not choices:
                brief = json.dumps(data, ensure_ascii=False)[:200]
                sys.stderr.write("[cloud] <- 返回体没有 choices：%s\n" % brief)
                last_err = RuntimeError('返回体没有 choices：%s' % brief)
                continue
            content = (choices[0].get('message') or {}).get('content') or ''
            if not content.strip():
                sys.stderr.write("[cloud] <- choices 里内容为空\n")
                last_err = RuntimeError('模型返回内容为空')
                continue
            sys.stderr.write("[cloud] <- 成功：%s\n" % url)
            return content, url

    raise RuntimeError('云端调用失败：%s' % (str(last_err) if last_err else '未知错误'))


# ---------- 可按需调整的配置 ----------
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"   # 用 127.0.0.1 而非 localhost，避免 IPv6 解析超时！
OLLAMA_MODEL = "qwenlocal:latest"
PROXY_PORT = 8000
# ------------------------------------


class ProxyHandler(SimpleHTTPRequestHandler):
    # ---------- 静态文件禁用缓存 ----------
    # 浏览器会把 http://127.0.0.1:8000/ourgame7.html 的旧副本缓存住，
    # 改了代码后双击 bat 看到的还是老版本（"bat 里是蓝球、直接开 html 却是新角色"就是这个原因）。
    # 这里对非 /api 的所有响应补上禁用缓存头，保证每次刷新都拿磁盘上的最新文件。
    def end_headers(self):
        if "/api/" not in (self.path or ""):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    # ---------- CORS 预检 ----------
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------- 只拦截 /api/chat 与 /marks ----------
    def do_POST(self):
        """最外层兜底：任何没预料到的异常也要回一段 JSON。
        否则连接会被硬断开，前端只看到 network error，根本不知道出了什么事。"""
        try:
            if (self.path or "").split("?")[0] == "/marks":
                self._handle_marks()
            else:
                self._handle_chat()
        except BrokenPipeError:
            pass                      # 前端已经等不及走了，安静收场
        except Exception as e:
            sys.stderr.write("[proxy] 未预期异常：%r\n" % e)
            try:
                self._send_json(500, {"error": "proxy error: %s" % e})
            except Exception:
                pass

    def _handle_marks(self):
        """接收游戏里「碰撞标记模式」画好的框，原样落盘到 marks.json。
        开发者直接读 marks.json 就能把这些框写成不可见碰撞盒，不用再靠肉眼估坐标。"""
        length = int(self.headers.get("Content-Length", 0))
        try:
            marks = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(marks, list):
                raise ValueError("marks 必须是数组")
        except Exception as e:
            self._send_json(400, {"error": "bad marks: %s" % e})
            return
        path = os.path.join(os.getcwd(), "marks.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)          # 原子替换，避免读到写了一半的文件
        sys.stderr.write("[marks] 已保存 %d 个标记 -> %s\n" % (len(marks), path))
        self._send_json(200, {"ok": True, "count": len(marks)})

    def _handle_chat(self):
        if self.path != "/api/chat":
            self._send_json(404, {"error": "Not Found"})
            return

        # 1. 读取请求体
        length = int(self.headers.get("Content-Length", 0))
        try:
            raw = self.rfile.read(length)
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"error": "Bad request: " + str(e)})
            return

        # 请求摘要日志：一眼看清这一回合玩家说了什么、带了几个历史、用不用云端
        _bg = (req.get("cloud") or {}) if isinstance(req.get("cloud"), dict) else {}
        sys.stderr.write("[proxy] 收到请求：npc=%s input=%r history=%d cloud=%s\n" % (
            req.get("npcId"),
            (req.get("playerInput") or "")[:40],
            len(req.get("history") or []),
            ("%s / %s" % (_bg.get("baseUrl"), _bg.get("model"))) if _bg else "无（本地）"))

        # 2. 交易先由服务端算好价格/结算，AI 只出对应台词
        profile = req.get("profile", {})
        player = req.get("player", {})
        player_input = (req.get("playerInput") or "").strip()
        trade = _resolve_trade(profile, player, req.get("trade"), player_input)

        # 3. 动态构建 System Prompt（把 NPC 身份、玩家状态、交易上下文注入）
        system_prompt = self._build_system_prompt(req, trade)

        # 4. 组装真正的多轮 messages（把对话历史还原成 assistant/user 交替）
        #    之前把 history 塞成一个 JSON 字符串，模型读到的是原始 JSON，所以语气很僵。
        messages = [{"role": "system", "content": system_prompt}]
        for h in req.get("history", []):
            text = str(h.get("text", "")).strip()
            if not text:
                continue
            role = "assistant" if h.get("role") == "npc" else "user"
            messages.append({"role": role, "content": text})
        if player_input:
            messages.append({"role": "user", "content": player_input})
        if len(messages) == 1:
            messages.append({"role": "user", "content": "（玩家走近，正看着你）"})

        # 告别语：服务端先判定一次，再给模型一条明确指令（小模型常常漏判）
        farewell = bool(player_input) and bool(FAREWELL_RE.search(player_input))
        if farewell:
            messages.append({
                "role": "user",
                "content": "（玩家这句是要走了。请用你的身份回一句简短的告别，"
                           "别挽留太久，并把 end 设为 true。）",
            })

        # 4. 选大模型：玩家选了云端就用云端（OpenAI 兼容），否则用本地 Ollama
        cloud = req.get("cloud") if isinstance(req.get("cloud"), dict) else None
        content = None
        cloud_info = None            # 云端调用的结果状态，会原样回传给前端，方便在 HUD / 控制台看到失败原因
        if cloud and (cloud.get("baseUrl") or "").strip():
            sys.stderr.write("[proxy] 本回合走云端：base=%s model=%s\n" % (
                cloud.get("baseUrl"), cloud.get("model")))
            try:
                _t0 = time.time()
                content, used_url = _call_openai_compatible(cloud, messages)
                sys.stderr.write("[proxy] 云端成功 %dms  %s\n" % (
                    round((time.time() - _t0) * 1000), used_url))
                cloud_info = {"used": True, "ok": True, "error": None, "url": used_url}
            except Exception as e:
                sys.stderr.write("[proxy] 云端调用失败，回退本地 Ollama：%s\n" % e)
                content = None
                cloud_info = {"used": True, "ok": False, "error": str(e)}
        elif cloud:
            sys.stderr.write("[proxy] 前端选了云端但 Base URL 为空 → 直接走本地\n")
            cloud_info = {"used": False, "ok": False, "error": "Base URL 为空"}
        else:
            sys.stderr.write("[proxy] 本回合走本地 Ollama\n")

        # 开局界面的「测试连接」：只测云端，不消耗本地 Ollama，也不因为云端失败去回退
        if bool(req.get("cloudTest")) and content is None:
            self._send_json(200, {
                "reply": "(云端连接测试未通过)",
                "cloud": cloud_info or {"used": False, "ok": False, "error": "未填写 Base URL"},
            })
            return

        if content is None:
            ollama_payload = {
                "model": OLLAMA_MODEL,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.75,     # 提高发散度，台词不再千篇一律
                    "num_ctx": 2048,         # 砍掉默认 1M context，大幅降 KV cache
                    "num_predict": 220,      # 允许 1~2 句话，够用又不拖速度
                    "top_p": 0.9,
                    "repeat_penalty": 1.1,   # 抑制复读
                },
                "messages": messages,
            }
            # 4b. 转发给 Ollama
            try:
                _t0 = __import__('time').time()
                data = json.dumps(ollama_payload, ensure_ascii=False).encode("utf-8")
                _conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=60)
                _conn.request("POST", "/api/chat", body=data,
                              headers={"Content-Type": "application/json"})
                _resp = _conn.getresponse()
                ollama_data = json.loads(_resp.read().decode("utf-8"))
                _conn.close()
                _dt = round((__import__('time').time() - _t0) * 1000)
                _od = round(ollama_data.get('total_duration', 0) / 1e6)
                sys.stderr.write("[proxy] %dms  (Ollama: %dms)\n" % (_dt, _od))
            except (ConnectionRefusedError, OSError) as e:
                out = {"error": "Ollama unreachable: " + str(e)}
                if cloud_info:
                    out["cloud"] = cloud_info
                self._send_json(502, out)
                return
            except Exception as e:
                out = {"error": "Ollama error: " + str(e)}
                if cloud_info:
                    out["cloud"] = cloud_info
                self._send_json(502, out)
                return
            content = ollama_data.get("message", {}).get("content", "")

        # 5. content 二次解析（容错，避免模型一次坏输出就让游戏报错）
        ai_result = _extract_json(content)
        if ai_result is None:
            sys.stderr.write("[proxy] JSON 解析失败，走兜底: %s\n" % content[:200])
            m = re.search(r'"reply"\s*:\s*"([^"”]{1,200})', content)
            ai_result = {"reply": m.group(1) if m else "……", "salvaged": True}

        # 6. 规范化字段（补默认值；告别语一律强制 end=true，不依赖模型自觉）
        if not isinstance(ai_result, dict):
            out = {"error": "model returned non-object JSON"}
            if cloud_info:
                out["cloud"] = cloud_info
            self._send_json(502, out)
            return
        ai_result.setdefault("reply", "……")
        ai_result.setdefault("emotion", "neutral")
        ai_result.setdefault("options", [])
        ai_result.setdefault("trustDelta", 0)
        ai_result.setdefault("action", {"type": "none"})
        ai_result.setdefault("end", False)
        if farewell:
            ai_result["end"] = True

        # 交易结果注入响应（无交易时省略）
        if trade is not None:
            # 模型常在台词里乱报价格（阿拉伯数字或中文数字）→ 换成兜底台词，数值统一由系统给出
            canned = _canned_trade_reply(trade)
            if canned and re.search(r"[0-9]|[一二两三四五六七八九十百千万]", str(ai_result.get("reply", ""))):
                ai_result["reply"] = canned
            ai_result["trade"] = trade

        # 7. 回给游戏端（把云端调用状态一并回传，前端才能告诉你为什么连不上）
        if cloud_info:
            ai_result["cloud"] = cloud_info
        self._send_json(200, ai_result)

    # ========================================================================
    # ★★★ 人设注入点（服务端侧）★★★
    # 上面 HTML 里 NPC_PROFILES 的 persona 会随请求传到这个方法，被拼进
    # System Prompt 交给大模型 —— 这就是「NPC 为什么会那样说话」的完整链路。
    #   · 想改某个 NPC 的性格 / 口吻 / 立场        → 去 HTML 改它的 persona（别改这里）
    #   · 想改所有 NPC 共用的硬规则（输出 JSON 格式、选项个数与字数、
    #     禁止报价数字、只对奶奶透露玩家真名、结束语行为…）→ 改下面这段 prompt
    # ========================================================================
    # ---------- 构建 System Prompt ----------
    def _build_system_prompt(self, req, trade=None):
        profile = req.get("profile", {})
        role = profile.get("role", "NPC")
        persona = profile.get("persona", "")
        # 结构化人设（可选；见前端 NPC_PROFILES 顶部的字段说明）。没写的 NPC 这里全是空 → 提示词与旧版逐字一致。
        npc_name = (profile.get("name") or "").strip()
        identity = (profile.get("identity") or "").strip()
        look = (profile.get("look") or "").strip()
        trait = (profile.get("trait") or "").strip()
        speak_style = (profile.get("speakStyle") or "").strip()
        know = [str(x).strip() for x in (profile.get("know") or []) if str(x).strip()]
        dont_know = [str(x).strip() for x in (profile.get("dontKnow") or []) if str(x).strip()]
        player = req.get("player", {})
        trust = req.get("trust", player.get("trust", 0))

        # 把结构化人设拼成几行"你是谁 / 你知道什么"，缺哪项就不输出哪项
        persona_block = ""
        if npc_name:
            persona_block += "你的名字叫「%s」。\n" % npc_name
        if identity:
            persona_block += "你的身份：%s。\n" % identity
        if look:
            persona_block += ("你的外貌气质：%s（把它化成你的神态和口气，"
                              "不要直接向玩家描述自己的长相）。\n" % look)
        if trait:
            persona_block += "你的性格关键词：%s。\n" % trait
        if speak_style:
            persona_block += "你的说话风格：%s。\n" % speak_style
        if know:
            persona_block += ("你知道的事只有这些：%s。玩家问到别的内幕，"
                              "你要么老实说不清楚，要么只按你的身份合理猜一句，绝不许编造新的内幕。\n"
                              % "、".join(know))
        if dont_know:
            persona_block += "这些事你不知道，被问起就说不清楚：%s。\n" % "、".join(dont_know)

        # 交易上下文：只告诉 AI 谈的是什么、进度如何，绝不告诉它报价数字
        trade_hint = ""
        if isinstance(trade, dict):
            d = trade.get("direction")
            item_name = trade.get("itemName", "")
            qty = trade.get("qty", 1)
            action = trade.get("action", "")
            status = trade.get("status", "")
            if d in ("buy", "sell") and item_name:
                verb = "买" if d == "buy" else "卖"
                if action == "accept":
                    trade_hint = "（这单谈成了：玩家要%s%s %d 个。你只回一句爽快的成交话，绝对不要在话里报价格。）" % (verb, item_name, qty)
                elif status == "insufficient":
                    trade_hint = "（这单玩家钱/货不够，没谈成。你只回一句提醒他钱没带够或货不够的话，绝对不要报数字。）"
                elif status == "countered":
                    trade_hint = "（玩家出的价你不接受，你已经还了另一个价。你按性格回一句不松口或为难的话，绝对不要在话里报任何数字。）"
                elif status == "rejected":
                    trade_hint = "（玩家的出价不在合理区间，这单没谈成。你按你的性格回一句不满或为难的话，绝对不要报数字。）"
                else:
                    trade_hint = "（玩家想%s%s，约 %d 个。你按性格应一声、说说行情或还价的话，绝对不要在话里报任何价格数字。）" % (verb, item_name, qty)

        # 称呼：只有奶奶知道玩家名字；其他 NPC 只知道性别，不许编造名字
        p_name = (player.get("name") or "").strip()
        gender = player.get("gender") or "male"
        knows_name = bool(player.get("npcKnowsName"))
        he = "她" if gender == "female" else "他"
        if knows_name and p_name:
            address = ("你看着%s长大，知道%s的大名叫「%s」，可以直接叫%s的名字，语气亲切。"
                       % (he, he, p_name, he))
        elif gender == "female":
            address = ("玩家是个女孩，但你（以及其他人都）不知道她的名字："
                       "称呼她「姑娘 / 丫头 / 小姑娘 / 闺女」这类说法，"
                       "绝对不要瞎编一个名字出来。")
        else:
            address = ("玩家是个男孩，但你（以及其他人都）不知道他的名字："
                       "称呼他「小伙子 / 小兄弟 / 年轻人 / 孩子」这类说法，"
                       "绝对不要瞎编一个名字出来。")

        return (
            "你在游戏《一百枚金币》里扮演角色【{role}】。{persona}\n"
            "{persona_block}"
            "{address}\n"
            "玩家当前状态：金币{money}/100000，精力{energy}，你和他的信任度{trust}（-10~+10）。\n"
            "说话要求：\n"
            "- 你就是【{role}】，每句话都要符合上面的性格和说话习惯，绝不要跑偏成别的角色或旁白腔\n"
            "- 用中文口语聊天，像真人一样，1~2句话，总共不超过80字\n"
            "- 不要书面腔、不要复读、不要每次都用同一种句式开头\n"
            "- 绝不要提到AI、模型、系统、提示词、JSON\n"
            "- 玩家要买/卖东西时，你只负责说人话、演情绪、还价话术，价格和结算由系统算给你，绝不要在台词里报数字\n"
            "- 精力低或信任度高时，可以顺带提一句，但永远以玩家这一句话为准：先接住他说的事，绝不要答非所问\n"
            "- 给出的选项必须顺着玩家这句话往下走（他问学校就给和学校有关的选项），不要给跟当前话题无关的选项\n"
            "- 玩家说再见/拜拜/要走了/下次聊→你回一句告别的话，并把 end 设成 true\n"
            "{trade_hint}"
            "只输出下面这个 JSON，不要任何多余文字（JSON 的引号必须用英文半角双引号 \"）：\n"
            '{{"reply":"你的台词","emotion":"happy|sad|angry|worried|neutral|surprised",'
            '"end":false,"options":["2-3个不超过14字、且和玩家这句话相关的行动选项"],'
            '"trustDelta":-3到3的整数,"action":{{"type":"none|give|take|warp"}}}}'
        ).format(
            role=role,
            persona=persona,
            persona_block=persona_block,
            address=address,
            money=player.get("money", 0),
            energy=player.get("energy", 100),
            trust=trust,
            trade_hint=trade_hint if trade_hint else "",
        )

    # ---------- 统一 JSON 响应 ----------
    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 简化日志 ----------
    def log_message(self, format, *args):
        sys.stderr.write("[proxy] %s\n" % (format % args))


def _ollama_alive():
    """探测 Ollama 是否在跑"""
    try:
        c = http.client.HTTPConnection("127.0.0.1", 11434, timeout=2)
        c.request("GET", "/api/tags")
        c.getresponse().read()
        c.close()
        return True
    except Exception:
        return False


def _ensure_ollama():
    """Ollama 没跑就尝试自动拉起，方便双击启动"""
    if _ollama_alive():
        return True
    print("  检测到 Ollama 未运行，正在自动启动 ...")
    try:
        subprocess.Popen("ollama serve", shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000)  # CREATE_NO_WINDOW，不弹黑框
        for _ in range(20):
            time.sleep(0.5)
            if _ollama_alive():
                print("  Ollama 已启动")
                return True
    except Exception as e:
        print("  自动启动 Ollama 失败：%s" % e)
    print("  请手动在终端执行：ollama serve")
    return False


def main():
    # 切换工作目录到脚本所在目录，这样 SimpleHTTPRequestHandler 能找到 HTML
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    port = PROXY_PORT
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    game_url = "http://127.0.0.1:%d/ourgame7.html" % port
    # 注意用 127.0.0.1，不要用 localhost（Windows 上 localhost 会先试 IPv6，超时 20 秒）

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
        # 多线程：一个慢的云端/Ollama 请求不会再堵住后面所有请求
        # （单线程时玩家连着对话会互相排队，一次请求卡住就整个页面"后端连不上"）
    except OSError:
        # 端口已被占用 —— 说明服务已经在跑，直接把游戏打开
        print("端口 %d 已被占用，服务应已在运行，直接打开游戏。" % port)
        webbrowser.open(game_url)
        return

    print("=" * 50)
    print("  《一百枚金币》本地代理已启动")
    print("  游戏地址: %s" % game_url)
    print("  Ollama:   %s" % OLLAMA_URL)
    print("  模型:     %s" % OLLAMA_MODEL)
    print("  按 Ctrl+C 停止")
    print("=" * 50)

    _ensure_ollama()

    # 1.5 秒后自动打开浏览器（想手动打开就删掉这一行）
    threading.Timer(1.5, lambda: webbrowser.open(game_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
