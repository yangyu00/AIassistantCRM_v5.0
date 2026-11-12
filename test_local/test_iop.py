from config import ImmutableYamlConfig
import requests
import datetime

tel = "18866816199"

class Tokens:
    def __init__(self) -> None:
        self.access_token = None
        self.deadline = None


TOKENS = Tokens()

global_config = ImmutableYamlConfig()

token_url = 'http://{host}:{port}/openapi/restOauth2Server/access_token'.format(
    host=global_config['IOP']['TOKEN']['host'], port=global_config['IOP']['TOKEN']['port'])

response = requests.post(token_url, data=f"grant_type=client_credentials"
                                         f"&client_id={global_config['IOP']['TOKEN']['id']}"
                                         f"&client_secret={global_config['IOP']['TOKEN']['secret']}")
res_data = response.json()

if 'access_token' in res_data:
    expires_in = res_data.get('expires_in', 600)
    TOKENS.deadline = datetime.datetime.now() + datetime.timedelta(seconds=(int(expires_in) - 360))
    TOKENS.access_token = res_data['access_token']
    print(TOKENS.access_token)
elif 'error' in res_data:
    TOKENS.access_token = None
    print(res_data)

########################################################################################################
draw_url = ("http://{host}:{port}/openapi/httpService/UserService"
           "?access_token={access_token}&method=qryCustInfoByChannel&format=json").format(
        host=global_config['IOP']['CustInfo']['host'],
        port=global_config['IOP']['CustInfo']['port'],
        access_token=TOKENS.access_token)

headers = {'Content-Type': 'application/json; charset=gbk', 'Accept-Encoding': 'gzip, deflate'}

post_data = {
    "VERSION": f"{global_config['IOP']['CustInfo']['version']}",
    "TELNUM": tel,
    "TYPE": f"{global_config['IOP']['CustInfo']['type']}",
}

response = requests.post(draw_url, json=post_data, headers=headers)
print(response.text)