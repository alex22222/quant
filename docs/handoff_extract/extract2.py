import json, os, glob, re, datetime

BASE = "/Users/henry/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions/wd_quant_d86246807aeb"
OUT = "/Users/henry/projects/quant/docs/handoff_extract"

def clean_user(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<kimi-skill-loaded.*?</kimi-skill-loaded>", "", text, flags=re.S)
    text = re.sub(r"^User activated the skill \"[^\"]+\"\. Follow the loaded skill instructions\.\s*", "", text)
    return text.strip()

def trunc(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    s = s.replace("\r","")
    return s if len(s) <= n else s[:n] + f" …[截断, 原长 {len(s)}]"

summary_all = []
for conv in sorted(glob.glob(os.path.join(BASE, "conv-*"))):
    f = os.path.join(conv, "agents/main/wire.jsonl")
    if not os.path.exists(f): continue
    cid = os.path.basename(conv)
    prompts, final_texts, tool_paths = [], [], []
    last_text = None
    created = None
    for line in open(f):
        try: d = json.loads(line)
        except: continue
        t = d.get("type")
        if t == "metadata": created = d.get("created_at")
        elif t == "turn.prompt":
            for c in d.get("input", []):
                if c.get("type") == "text":
                    txt = clean_user(c.get("text",""))
                    if txt: prompts.append(txt)
        elif t == "context.append_loop_event":
            e = d.get("event", {})
            et = e.get("type")
            if et == "content.part":
                p = e.get("part", {})
                if p.get("type") == "text" and p.get("text","").strip():
                    last_text = p["text"]
            elif et == "tool.call":
                args = e.get("args", {})
                path = args.get("path") if isinstance(args, dict) else None
                if path: tool_paths.append(path)
            elif et == "step.end" and e.get("finishReason") == "end_turn":
                if last_text: final_texts.append(last_text); last_text = None
    ts = datetime.datetime.fromtimestamp(created/1000).strftime("%Y-%m-%d %H:%M") if created else "?"
    sec = [f"\n{'='*70}\n# {cid}  ({ts})", ]
    for i, p in enumerate(prompts):
        sec.append(f"\n## 👤 用户提问 {i+1}\n{trunc(p, 2500)}")
    for i, a in enumerate(final_texts):
        sec.append(f"\n## 🤖 Kimi 最终回复 {i+1}\n{trunc(a, 3500)}")
    if tool_paths:
        uniq = list(dict.fromkeys(tool_paths))
        sec.append(f"\n## 📁 涉及文件 ({len(tool_paths)} 次, {len(uniq)} 唯一)\n" + "\n".join(uniq[:80]))
    summary_all.append("\n".join(sec))

out = os.path.join(OUT, "_SUMMARY.md")
open(out, "w").write("\n".join(summary_all))
print("written", out, os.path.getsize(out), "bytes")
