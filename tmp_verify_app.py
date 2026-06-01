import os
import importlib
os.environ['VERCEL'] = '1'
app = importlib.import_module('app').app
client = app.test_client()
response = client.get('/')
print('status', response.status_code)
print(response.data[:400])
