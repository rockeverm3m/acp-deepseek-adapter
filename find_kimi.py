import json
d = json.load(open('/Users/rk/.openclaw-autoclaw/openclaw.json'))
for k, v in d.items():
    s = json.dumps(v)
    if 'kimi' in s.lower() or 'coding' in s.lower():
        print(f'--- {k} ---')
        print(json.dumps(v, indent=2, ensure_ascii=False)[:800])
