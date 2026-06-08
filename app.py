"""小鞠知花 · 聊天壳 · 纯净版 (无硬编码密钥)"""
import json, os, re, sqlite3, socket, requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'komari_memory.db')

# ─── SKILL.md ────────────────────
SKILL_PATH = os.path.join(os.path.dirname(__file__), 'prompt.txt')
if not os.path.exists(SKILL_PATH):
    SKILL_PATH = os.path.expanduser('~/.claude/skills/komari-chika/SKILL.md')
SYSTEM_PROMPT = open(SKILL_PATH, 'r', encoding='utf-8').read() if os.path.exists(SKILL_PATH) else "你是小鞠知花。"

# ─── API 配置 (纯环境变量) ───────
API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
API_BASE = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/anthropic')
API_URL = f"{API_BASE.rstrip('/')}/v1/messages"
MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-pro')

# 如果没设环境变量，尝试从cc-connect config读取
if not API_KEY:
    cfg = os.path.expanduser('~/.cc-connect/config.toml')
    if os.path.exists(cfg):
        txt = open(cfg, encoding='utf-8').read()
        m = re.search(r'api_key\s*=\s*"([^"]+)"', txt)
        if m: API_KEY = m.group(1)
        m = re.search(r'base_url\s*=\s*"([^"]+)"', txt)
        if m: API_URL = f"{m.group(1).rstrip('/')}/v1/messages"
        m = re.search(r'model\s*=\s*"([^"]+)"', txt)
        if m: MODEL = m.group(1)

# ─── 数据库 ────────────────────
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, key TEXT, value TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(session_id, key));
        CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
    ''')
    db.commit(); db.close()
init_db()

sessions_cache = {}

def load_session(sid):
    if sid in sessions_cache: return sessions_cache[sid]
    db = sqlite3.connect(DB_PATH)
    db.execute('INSERT OR REPLACE INTO sessions (id, last_active) VALUES (?, CURRENT_TIMESTAMP)', (sid,))
    rows = db.execute('SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC LIMIT 60', (sid,)).fetchall()
    mems = {k:v for k,v in db.execute('SELECT key, value FROM memories WHERE session_id=?', (sid,)).fetchall()}
    db.close()
    now = datetime.now()
    t = now.strftime("%Y年%m月%d日 %H:%M")
    w = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    mc = "\n\n你记得：\n" + "\n".join(f"- {k}: {v}" for k,v in mems.items()) if mems else ""
    msgs = [{"role":"system","content":SYSTEM_PROMPT + f"\n\n当前时间：{t} {w}。请据此决定你在哪、做什么、语气如何。" + mc}]
    for r,c in rows: msgs.append({"role":r,"content":c})
    sessions_cache[sid] = msgs
    return msgs

def save_msg(sid, role, content):
    db = sqlite3.connect(DB_PATH)
    db.execute('INSERT INTO messages (session_id, role, content) VALUES (?,?,?)', (sid, role, content))
    db.execute('DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE session_id=? ORDER BY id ASC LIMIT MAX(0,(SELECT COUNT(*) FROM messages WHERE session_id=?)-500))', (sid,sid))
    db.commit(); db.close()
    sessions_cache.pop(sid, None)

def save_mem(sid, key, value):
    db = sqlite3.connect(DB_PATH)
    db.execute('INSERT OR REPLACE INTO memories (session_id, key, value) VALUES (?,?,?)', (sid, key, value))
    db.commit(); db.close()
    sessions_cache.pop(sid, None)

def extract_mem(sid, umsg, areply):
    for pat, label in [(r'我叫(.{1,20})','对方的名字'),(r'我喜欢(.{1,30})','对方喜欢'),(r'我是[一-龥]{1,15}(?:的|$|，|。)','对方的身份')]:
        m = re.search(pat, umsg)
        if m:
            v = m.group(1).strip().rstrip('。，,.!！') if m.lastindex else m.group(0)
            if v: save_mem(sid, label, v)

# ─── 路由 ────────────────────
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/chat', methods=['POST'])
def chat():
    d = request.json; sid = d.get('session_id','default'); umsg = d.get('message','').strip()
    if not umsg: return jsonify({"error":"empty"}), 400
    save_msg(sid, "user", umsg)
    msgs = load_session(sid) + [{"role":"user","content":umsg}]
    if len(msgs) > 61: msgs = [msgs[0]] + msgs[-60:]

    try:
        r = requests.post(API_URL,
            headers={"Authorization":f"Bearer {API_KEY}","Content-Type":"application/json","anthropic-version":"2023-06-01"},
            json={"model":MODEL,"messages":msgs,"max_tokens":512,"temperature":0.9,"stream":True},
            timeout=60, stream=True)
        if r.status_code != 200: return jsonify({"error":f"API {r.status_code}"}), r.status_code

        def gen():
            full = ""
            for line in r.iter_lines():
                if not line: continue
                line = line.decode('utf-8')
                if line.startswith("data: "):
                    d2 = line[6:]
                    if d2 == "[DONE]": break
                    try:
                        ch = json.loads(d2)
                        if ch.get("type")=="content_block_delta":
                            t = ch.get("delta",{}).get("text","")
                            if t: full += t; yield f"data: {json.dumps({'text':t})}\n\n"
                    except: pass
            if full:
                save_msg(sid, "assistant", full)
                try: extract_mem(sid, umsg, full)
                except: pass
            yield "data: [DONE]\n\n"
        return Response(stream_with_context(gen()), content_type='text/event-stream', headers={'Cache-Control':'no-cache'})
    except requests.exceptions.Timeout: return jsonify({"error":"timeout"}), 504
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route('/api/reset', methods=['POST'])
def reset():
    sid = request.json.get('session_id','default')
    db = sqlite3.connect(DB_PATH)
    db.execute('DELETE FROM messages WHERE session_id=?',(sid,))
    db.execute('DELETE FROM memories WHERE session_id=?',(sid,))
    db.commit(); db.close()
    sessions_cache.pop(sid, None)
    return jsonify({"ok":True})

@app.route('/api/memories')
def list_mem():
    sid = request.args.get('session_id','default')
    db = sqlite3.connect(DB_PATH)
    rows = db.execute('SELECT key, value FROM memories WHERE session_id=?',(sid,)).fetchall()
    db.close()
    return jsonify({k:v for k,v in rows})

@app.route('/api/history')
def get_history():
    sid = request.args.get('session_id','default')
    db = sqlite3.connect(DB_PATH)
    rows = db.execute('SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id ASC',(sid,)).fetchall()
    db.close()
    return jsonify([{'role':r,'content':c,'time':t} for r,c,t in rows])

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 9527))
    print("🌸 小鞠知花 · 聊天壳")
    print(f"   SKILL.md: {len(SYSTEM_PROMPT)}字符 {'✅' if len(SYSTEM_PROMPT)>1000 else '⚠️'}")
    print(f"   本地: http://127.0.0.1:{port}")
    h = socket.gethostname(); ip = socket.gethostbyname(h)
    print(f"   手机: http://{ip}:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
