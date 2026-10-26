# 临时冒烟测试脚本（联调验证后删除）——第五轮：综述超时修复验证
import time

import httpx

c = httpx.Client(base_url='http://127.0.0.1:8000', timeout=60)

r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'admin123'})
tok = {'Authorization': 'Bearer ' + r.json()['access_token']}
print('login:', r.status_code)

r = c.post('/api/qa/review', headers=tok, json={'subscription_id': 1})
print('review ask:', r.status_code, r.json())
sid = r.json()['session_id']
finished = False
for i in range(120):  # 最长 20 分钟
    time.sleep(10)
    steps = c.get(f'/api/qa/{sid}/steps', headers=tok).json()
    if steps['finished'] or i % 3 == 0:
        print('  ', [(s['key'], s['status']) for s in steps['steps'] if s['status'] != 'pending'])
    if steps['finished']:
        finished = True
        break

res = c.get(f'/api/qa/{sid}/result', headers=tok).json()
print('finished:', res['finished'], '| papers:', res.get('paper_count'),
      '| sections:', [s['title'] for s in res.get('sections', [])])
print('summary head:', res['answer'][:250].replace('\n', ' '))
