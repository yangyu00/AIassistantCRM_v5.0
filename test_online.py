import base64
import json
import time
import requests
import os
import random

for k in range(1):
    post_data = {"sceneId": "12345", "telnum": "18866816199", "productId": [{"id": "202502120000001"}]}
    start_time = time.time()
    address = 'http://127.0.0.1:9090/api/speechQryService'
    r = requests.post(address, json=post_data)
    end_time = time.time()
    print('****************  ' + str(k) + '  ******************')
    print(f"耗用时间:{end_time - start_time}")
    print(r.json())
