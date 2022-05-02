import base64
import json
import re
import requests

from lxml import etree
from urllib.parse import urlparse

from bot import APPDRIVE_EMAIL, APPDRIVE_PASS, GDTOT_CRYPT
from bot.helper.ext_utils.exceptions import DDLException

account = {
    'email': APPDRIVE_EMAIL, 
    'passwd': APPDRIVE_PASS
}

def account_login(client, url, email, password):
    data = {
        'email': email,
        'password': password
    }
    client.post(f'https://{urlparse(url).netloc}/login', data=data)

def gen_payload(data, boundary=f'{"-"*6}_'):
    data_string = ''
    for item in data:
        data_string += f'{boundary}\r\n'
        data_string += f'Content-Disposition: form-data; name="{item}"\r\n\r\n{data[item]}\r\n'
    data_string += f'{boundary}--\r\n'
    return data_string

def parse_info(data):
    info = re.findall(r'>(.*?)<\/li>', data)
    info_parsed = {}
    for item in info:
        kv = [s.strip() for s in item.split(':', maxsplit=1)]
        info_parsed[kv[0].lower()] = kv[1]
    return info_parsed

def appdrive(url: str) -> str:
    if (APPDRIVE_EMAIL or APPDRIVE_PASS) is None:
        raise DDLException("APPDRIVE_EMAIL and APPDRIVE_PASS env vars not provided")
    client = requests.Session()
    client.headers.update({
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.99 Safari/537.36"
    })
    account_login(client, url, account['email'], account['passwd'])
    res = client.get(url)
    key = re.findall(r'"key",\s+"(.*?)"', res.text)[0]
    ddl_btn = etree.HTML(res.content).xpath("//button[@id='drc']")
    info_parsed = parse_info(res.text)
    info_parsed['error'] = False
    info_parsed['link_type'] = 'login'  # direct/login
    headers = {
        "Content-Type": f"multipart/form-data; boundary={'-'*4}_",
    }
    data = {
        'type': 1,
        'key': key,
        'action': 'original'
    }
    if len(ddl_btn):
        info_parsed['link_type'] = 'direct'
        data['action'] = 'direct'
    while data['type'] <= 3:
        try:
            response = client.post(url, data=gen_payload(data), headers=headers).json()
            break
        except: data['type'] += 1
    if 'url' in response:
        info_parsed['gdrive_link'] = response['url']
    elif 'error' in response and response['error']:
        info_parsed['error'] = True
        info_parsed['error_message'] = response['message']
    if urlparse(url).netloc == 'driveapp.in' and not info_parsed['error']:
        res = client.get(info_parsed['gdrive_link'])
        drive_link = etree.HTML(res.content).xpath("//a[contains(@class,'btn')]/@href")[0]
        info_parsed['gdrive_link'] = drive_link
    if not info_parsed['error']:
        return info_parsed
    else:
        raise DDLException(f"{info_parsed['error_message']}")

def gdtot(url: str) -> str:
    if GDTOT_CRYPT is None:
        raise DDLException("GDTOT_CRYPT env var not provided")
    client = requests.Session()
    client.cookies.update({'crypt': GDTOT_CRYPT})
    res = client.get(url)
    res = client.get(f"https://new.gdtot.top/dld?id={url.split('/')[-1]}")
    matches = re.findall(r'gd=(.*?)&', res.text)
    try:
        decoded_id = base64.b64decode(str(matches[0])).decode('utf-8')
    except:
        raise DDLException("Unable to parse link")
    gdrive_url = f'https://drive.google.com/open?id={decoded_id}'
    return gdrive_url
