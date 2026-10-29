import json, os, glob, re, sys

BASE = "/Users/henry/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions/wd_quant_d86246807aeb"
OUT = "/Users/henry/projects/quant/docs/handoff_extract"

SKIP_PATTERNS = re.compile(r"^(<system-reminder>|User activated the skill|<kimi-skill-loaded|<meta awareness|daimon:|# Available skills|# Automation limits)", re.S)

def clean_user(text):
    """strip system-reminder blocks etc from user text"""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<kimi-skill-loaded.*?</kimi-skill-loaded>", "", text, flags=re.S)
    return text.strip()

def trunc(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    s = s.replace("\r", "")
    return s if len(s) <= n else s[:n] + f" …[截断, 原长 {len(s)}]"

def brief_args(name, args):
    if not isinstance(args, dict): return trunc(str(args), 200)
    if name == "Bash": return "cmd: " + trunc(args.get("command",""), 300)
    if name in ("Read","Write","Edit","Glob","Grep"):
        p = args.get("path") or args.get("pattern") or ""
        extra = ""
        if name == "Edit": extra = " old:" + trunc(args.get("old_string",""), 80)
        return f"{p}{extra}"
    return trunc(json.dumps(args, ensure_ascii=False), 200)

for conv in sorted(glob.glob(os.path.join(BASE, "conv-*"))):
    f = os.path.join(conv, "agents/main/wire.jsonl")
    if not os.path.exists(f): continue
    cid = os.path.basename(conv)
    lines = []
    lines.append(f"# 会话 {cid}")
    lines.append(f"文件: {f} ({os.path.getsize(f)} bytes)")
    created = None
    n_user = n_asst = n_tools = 0
    for line in open(f):
        try: d = json.loads(line)
        except: continue
        t = d.get("type")
        if t == "metadata":
            created = d.get("created_at")
        elif t == "context.append_message":
            m = d.get("message", {})
            if m.get("role") == "user":
                for c in m.get("content", []):
                    if c.get("type") == "text":
                        txt = clean_user(c.get("text",""))
                        if txt and not SKIP_PATTERNS.match(txt):
                            n_user += 1
                            lines.append(f"\n## 👤 用户\n{trunc(txt, 3000)}")
        elif t == "context.append_loop_event":
            e = d.get("event", {})
            et = e.get("type")
            if et == "content.part":
                p = e.get("part", {})
                if p.get("type") == "text" and p.get("text","").strip():
                    n_asst += 1
                    lines.append(f"\n## 🤖 Kimi\n{trunc(p['text'], 4000)}")
            elif et == "tool.call":
                n_tools += 1
                lines.append(f"\n### 🔧 {e.get('name')} — {brief_args(e.get('name'), e.get('args'))}")
            elif et == "tool.result":
                out = e.get("result", {}).get("output", "")
                lines.append(f"  ↳ 结果: {trunc(out, 600)}")
    if created:
        import datetime
        ts = datetime.datetime.fromtimestamp(created/1000).strftime("%Y-%m-%d %H:%M")
        lines.insert(2, f"创建时间: {ts}")
    lines.insert(3, f"统计: 用户消息 {n_user} | 助手回复 {n_asst} | 工具调用 {n_tools}")
    out = os.path.join(OUT, f"{cid}.md")
    open(out, "w").write("\n".join(lines))
    print(f"{cid}: user={n_user} asst={n_asst} tools={n_tools} -> {os.path.getsize(out)} bytes")
