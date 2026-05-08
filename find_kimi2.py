import json
d = json.load(open('/Users/rk/.openclaw-autoclaw/openclaw.json'))
models = d.get('models', {})
providers = models.get('providers', {})
for pid, pdata in providers.items():
    if 'kimi' in pid.lower() or 'kimi' in json.dumps(pdata).lower():
        print(f'Provider: {pid}')
        print(json.dumps(pdata, indent=2, ensure_ascii=False)[:1000])
        print('---')
