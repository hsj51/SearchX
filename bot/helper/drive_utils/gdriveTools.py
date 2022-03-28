import logging
import os
import json
import re
import requests

import urllib.parse as urlparse
from urllib.parse import parse_qs
from random import randrange
import time
from telegram import InlineKeyboardMarkup
from telegraph.exceptions import RetryAfterError

from queue import Queue
from threading import Thread

from httplib2 import Http
from googleapiclient.http import HttpRequest
from google_auth_httplib2 import AuthorizedHttp

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import *

from bot import LOGGER, DRIVE_NAME, DRIVE_ID, INDEX_URL, telegra_ph, \
    IS_TEAM_DRIVE, parent_id, USE_SERVICE_ACCOUNTS, DRIVE_INDEX_URL, MAX_THREADS
from bot.helper.ext_utils.bot_utils import *
from bot.helper.telegram_helper import button_builder

logging.getLogger('googleapiclient.discovery').setLevel(logging.ERROR)

if USE_SERVICE_ACCOUNTS:
    SERVICE_ACCOUNT_INDEX = randrange(len(os.listdir("accounts")))

telegraph_limit = 95

class ThreadWorker(Thread):

    def __init__(self, queue, function, callback=None):
        Thread.__init__(self)
        self.queue = queue
        self.function = function
        self.callback = callback

    def run(self):
        while True:
            index, arg = self.queue.get()
            result = exception = None

            try:
                result = self.function(*arg)
            except Exception as e:
                exception = e
            finally:
                if self.callback is not None:
                    self.callback(index, result, exception)
                self.queue.task_done()


class GoogleDriveHelper:
    def __init__(self, name=None, listener=None):
        self.listener = listener
        self.name = name
        self.__G_DRIVE_TOKEN_FILE = "token.json"
        # Check https://developers.google.com/drive/scopes for all available scopes
        self.__OAUTH_SCOPE = ['https://www.googleapis.com/auth/drive']
        self.__G_DRIVE_DIR_MIME_TYPE = "application/vnd.google-apps.folder"
        self.__G_DRIVE_BASE_DOWNLOAD_URL = "https://drive.google.com/uc?id={}&export=download"
        self.__G_DRIVE_DIR_BASE_DOWNLOAD_URL = "https://drive.google.com/drive/folders/{}"
        self.__service = self.authorize()
        self.telegraph_content = []
        self.path = []
        self.total_bytes = 0
        self.total_files = 0
        self.total_folders = 0
        self.transferred_size = 0
        self.alt_auth = False
        self.responses = {}
        self.dir_list = {}

    def authorize(self):
        # Get credentials
        credentials = None
        if not USE_SERVICE_ACCOUNTS:
            if os.path.exists(self.__G_DRIVE_TOKEN_FILE):
                credentials = Credentials.from_authorized_user_file(self.__G_DRIVE_TOKEN_FILE, self.__OAUTH_SCOPE)
            if credentials is None or not credentials.valid:
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
        else:
            LOGGER.info(f"Authorizing with {SERVICE_ACCOUNT_INDEX}.json file")
            credentials = service_account.Credentials.from_service_account_file(
                f'accounts/{SERVICE_ACCOUNT_INDEX}.json', scopes=self.__OAUTH_SCOPE)

        self.credentials = credentials
        authorized_http = AuthorizedHttp(credentials, http=Http())
        return build('drive', 'v3', cache_discovery=False, requestBuilder=self.build_request, http=authorized_http)

    def alt_authorize(self):
        credentials = None
        if USE_SERVICE_ACCOUNTS and not self.alt_auth:
            self.alt_auth = True
            if os.path.exists(self.__G_DRIVE_TOKEN_FILE):
                LOGGER.info("Authorizing with token.json file")
                credentials = Credentials.from_authorized_user_file(self.__G_DRIVE_TOKEN_FILE, self.__OAUTH_SCOPE)
                if credentials is None or not credentials.valid:
                    if credentials and credentials.expired and credentials.refresh_token:
                        credentials.refresh(Request())

                self.credentials = credentials
                authorized_http = AuthorizedHttp(credentials, http=Http())
                return build('drive', 'v3', cache_discovery=False, requestBuilder=self.build_request, http=authorized_http)
        return None

    def build_request(self, http, *args, **kwargs):
        new_http = AuthorizedHttp(self.credentials, http=Http())
        return HttpRequest(new_http, *args, **kwargs)

    @staticmethod
    def getIdFromUrl(link: str):
        if "folders" in link or "file" in link:
            regex = r"https://drive\.google\.com/(drive)?/?u?/?\d?/?(mobile)?/?(file)?(folders)?/?d?/([-\w]+)[?+]?/?(w+)?"
            res = re.search(regex, link)
            if res is None:
                raise IndexError("Drive ID not found")
            return res.group(5)
        parsed = urlparse.urlparse(link)
        return parse_qs(parsed.query)['id'][0]

    def deleteFile(self, link: str):
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError, IndexError):
            msg = "Drive ID not found"
            LOGGER.error(f"{msg}")
            return msg
        msg = ''
        try:
            res = self.__service.files().delete(fileId=file_id, supportsTeamDrives=IS_TEAM_DRIVE).execute()
            msg = "Successfully deleted"
        except HttpError as err:
            if "File not found" in str(err):
                msg = "No such file exists"
            elif "insufficientFilePermissions" in str(err):
                msg = "Insufficient file permissions"
                token_service = self.alt_authorize()
                if token_service is not None:
                    self.__service = token_service
                    return self.deleteFile(link)
            else:
                msg = str(err)
            LOGGER.error(f"{msg}")
        finally:
            return msg

    def switchServiceAccount(self):
        global SERVICE_ACCOUNT_INDEX
        service_account_count = len(os.listdir("accounts"))
        if SERVICE_ACCOUNT_INDEX == service_account_count - 1:
            SERVICE_ACCOUNT_INDEX = 0
        SERVICE_ACCOUNT_INDEX += 1
        LOGGER.info(f"Authorizing with {SERVICE_ACCOUNT_INDEX}.json file")
        self.__service = self.authorize()

    def __set_permission(self, drive_id):
        permissions = {
            'role': 'reader',
            'type': 'anyone',
            'value': None,
            'withLink': True
        }
        return self.__service.permissions().create(supportsTeamDrives=True, fileId=drive_id,
                                                   body=permissions).execute()

    def setPerm(self, link: str):
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError, IndexError):
            msg = "Drive ID not found"
            LOGGER.error(f"{msg}")
            return msg
        msg = ''
        try:
            res = self.__set_permission(file_id)
            msg = "Successfully set permissions"
        except HttpError as err:
            if "File not found" in str(err):
                msg = "No such file exists"
            elif "insufficientFilePermissions" in str(err):
                msg = "Insufficient file permissions"
                token_service = self.alt_authorize()
                if token_service is not None:
                    self.__service = token_service
                    return self.setPerm(link)
            else:
                msg = str(err)
            LOGGER.error(f"{msg}")
        finally:
            return msg

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def copyFile(self, file_id, dest_id, status):
        body = {
            'parents': [dest_id]
        }
        try:
            res = self.__service.files().copy(supportsAllDrives=True, fileId=file_id, body=body).execute()
            return res
        except HttpError as err:
            if err.resp.get('content-type', '').startswith('application/json'):
                reason = json.loads(err.content).get('error').get('errors')[0].get('reason')
                if reason == 'userRateLimitExceeded' or reason == 'dailyLimitExceeded':
                    if USE_SERVICE_ACCOUNTS:
                        self.switchServiceAccount()
                        return self.copyFile(file_id, dest_id, status)
                    else:
                        LOGGER.info(f"Warning: {reason}")
                        raise err
                else:
                    raise err

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def getFileMetadata(self, file_id):
        return self.__service.files().get(supportsAllDrives=True, fileId=file_id,
                                              fields="name, id, mimeType, size").execute()

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def getFilesByFolderId(self, folder_id):
        page_token = None
        query = f"'{folder_id}' in parents and trashed = false"
        files = []
        while True:
            response = self.__service.files().list(supportsTeamDrives=True,
                                                   includeTeamDriveItems=True,
                                                   q=query,
                                                   spaces='drive',
                                                   pageSize=200,
                                                   fields='nextPageToken, files(id, name, mimeType, size)',
                                                   pageToken=page_token).execute()
            for file in response.get('files', []):
                files.append(file)
            page_token = response.get('nextPageToken', None)
            if page_token is None:
                break
        return files

    def clone(self, link, status):
        self.transferred_size = 0
        self.total_files = 0
        self.total_folders = 0
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError, IndexError):
            msg = "Drive ID not found"
            LOGGER.error(f"{msg}")
            return msg
        msg = ""
        try:
            meta = self.getFileMetadata(file_id)
            status.set_source_folder(meta.get('name'), self.__G_DRIVE_DIR_BASE_DOWNLOAD_URL.format(meta.get('id')))
            if meta.get("mimeType") == self.__G_DRIVE_DIR_MIME_TYPE:
                dir_id = self.create_directory(meta.get('name'), parent_id)
                self.cloneFolder(meta.get('name'), meta.get('name'), meta.get('id'), dir_id, status)
                status.set_status(True)
                msg += f'<b>Filename: </b><code>{meta.get("name")}</code>'
                msg += f'\n<b>Size: </b>{get_readable_file_size(self.transferred_size)}'
                msg += f"\n<b>Type: </b>Folder"
                msg += f"\n<b>SubFolders: </b>{self.total_folders}"
                msg += f"\n<b>Files: </b>{self.total_files}"
                msg += f'\n\n<a href="{self.__G_DRIVE_DIR_BASE_DOWNLOAD_URL.format(dir_id)}">Drive Link</a>'
                if DRIVE_INDEX_URL is not None:
                    url = requests.utils.requote_uri(f'{DRIVE_INDEX_URL}/{meta.get("name")}/')
                    msg += f' | <a href="{url}">Index Link</a>'
            else:
                file = self.copyFile(meta.get('id'), parent_id, status)
                try:
                    typ = file.get('mimeType')
                except:
                    typ = 'File' 
                msg += f'<b>Filename: </b><code>{file.get("name")}</code>'
                try:
                    msg += f'\n<b>Size: </b>{get_readable_file_size(int(meta.get("size", 0)))}'
                    msg += f'\n<b>Type: </b>{typ}'
                    msg += f'\n\n<a href="{self.__G_DRIVE_BASE_DOWNLOAD_URL.format(file.get("id"))}">Drive Link</a>'
                except TypeError:
                    pass
                if DRIVE_INDEX_URL is not None:
                    url = requests.utils.requote_uri(f'{DRIVE_INDEX_URL}/{file.get("name")}')
                    msg += f' | <a href="{url}">Index Link</a>'
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            err = str(err).replace('>', '').replace('<', '')
            LOGGER.error(err)
            if "User rate limit exceeded" in str(err):
                msg = "User rate limit exceeded"
            elif "File not found" in str(err):
                token_service = self.alt_authorize()
                if token_service is not None:
                    self.__service = token_service
                    return self.clone(link)
                msg = "No such file exists"
            else:
                msg = str(err)
            LOGGER.error(f"{msg}")
        return msg

    def cloneFolder(self, name, local_path, folder_id, parent_id, status):
        LOGGER.info(f"Syncing: {local_path}")
        files = self.getFilesByFolderId(folder_id)
        if len(files) == 0:
            return parent_id
        for file in files:
            if file.get('mimeType') == self.__G_DRIVE_DIR_MIME_TYPE:
                self.total_folders += 1
                file_path = os.path.join(local_path, file.get('name'))
                current_dir_id = self.create_directory(file.get('name'), parent_id)
                self.cloneFolder(file.get('name'), file_path, file.get('id'), current_dir_id, status)
            else:
                self.copyFile(file.get('id'), parent_id, status)
                self.total_files += 1
                self.transferred_size += int(file.get('size', 0))
                status.set_name(file.get('name'))
                status.add_size(int(file.get('size')))

    @retry(wait=wait_exponential(multiplier=2, min=3, max=6), stop=stop_after_attempt(5),
           retry=retry_if_exception_type(HttpError), before=before_log(LOGGER, logging.DEBUG))
    def create_directory(self, directory_name, parent_id):
        file_metadata = {
            "name": directory_name,
            "mimeType": self.__G_DRIVE_DIR_MIME_TYPE
        }
        if parent_id is not None:
            file_metadata["parents"] = [parent_id]
        file = self.__service.files().create(supportsTeamDrives=True, body=file_metadata).execute()
        file_id = file.get("id")
        if not IS_TEAM_DRIVE:
            self.__set_permission(file_id)
        LOGGER.info("Created: {}".format(file.get("name")))
        return file_id

    def count(self, link):
        try:
            file_id = self.getIdFromUrl(link)
        except (KeyError, IndexError):
            msg = "Drive ID not found"
            LOGGER.error(f"{msg}")
            return msg
        msg = ""
        try:
            meta = self.getFileMetadata(file_id)
            mime_type = meta.get('mimeType')
            if mime_type == self.__G_DRIVE_DIR_MIME_TYPE:
                self.gDrive_directory(meta)
                msg += f'<b>Name: </b><code>{meta.get("name")}</code>'
                msg += f'\n<b>Size: </b>{get_readable_file_size(self.total_bytes)}'
                msg += f'\n<b>Type: </b>Folder'
                msg += f'\n<b>SubFolders: </b>{self.total_folders}'
                msg += f'\n<b>Files: </b>{self.total_files}'
            else:
                msg += f'<b>Name: </b><code>{meta.get("name")}</code>'
                if mime_type is None:
                    mime_type = 'File'
                self.total_files += 1
                self.gDrive_file(meta)
                msg += f'\n<b>Size: </b>{get_readable_file_size(self.total_bytes)}'
                msg += f'\n<b>Type: </b>{mime_type}'
                msg += f'\n<b>Files: </b>{self.total_files}'
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            err = str(err).replace('>', '').replace('<', '')
            LOGGER.error(err)
            if "File not found" in str(err):
                token_service = self.alt_authorize()
                if token_service is not None:
                    self.__service = token_service
                    return self.count(link)
                msg = "No such file exists"
            else:
                msg = str(err)
            LOGGER.error(f"{msg}")
        return msg

    def gDrive_file(self, filee):
        size = int(filee.get('size', 0))
        self.total_bytes += size

    def gDrive_directory(self, drive_folder):
        files = self.getFilesByFolderId(drive_folder['id'])
        if len(files) == 0:
            return
        for filee in files:
            shortcut_details = filee.get('shortcutDetails')
            if shortcut_details is not None:
                mime_type = shortcut_details['targetMimeType']
                file_id = shortcut_details['targetId']
                filee = self.getFileMetadata(file_id)
            else:
                mime_type = filee.get('mimeType')
            if mime_type == self.__G_DRIVE_DIR_MIME_TYPE:
                self.total_folders += 1
                self.gDrive_directory(filee)
            else:
                self.total_files += 1
                self.gDrive_file(filee)

    def get_recursive_list(self, file, root_id="root"):
        return_list = []
        if not root_id:
            root_id = file.get('teamDriveId')
        if root_id == "root":
            root_id = self.__service.files().get(fileId='root', fields="id").execute().get('id')
        x = file.get("name")
        y = file.get("id")
        while y != root_id:
            return_list.append(x)
            file = self.__service.files().get(
                fileId = file.get("parents")[0],
                supportsAllDrives=True,
                fields='id, name, parents'
            ).execute()
            x = file.get("name")
            y = file.get("id")
        return_list.reverse()
        return root_id, return_list

    def escapes(self, str_val):
        chars = ['\\', "'", '"', r'\a', r'\b', r'\f', r'\n', r'\r', r'\t']
        for char in chars:
            str_val = str_val.replace(char, '\\' + char)
        return str_val

    def drive_query_backup(self, parent_id):
        query = f"'{parent_id}' in parents and (name contains '{self.file_name}')"
        response = self.__service.files().list(supportsTeamDrives=True,
                                               includeTeamDriveItems=True,
                                               q=query,
                                               spaces='drive',
                                               pageSize=1000,
                                               fields='files(id, name, mimeType, size, parents)',
                                               orderBy='folder, modifiedTime desc').execute()["files"]
        return response

    def drive_query(self, index, parent_id, query):
        if parent_id != "root":
            self.__batch.add(self.__service.files().list(supportsTeamDrives=True,
                                                        includeTeamDriveItems=True,
                                                        teamDriveId=parent_id,
                                                        q=query,
                                                        corpora='drive',
                                                        spaces='drive',
                                                        pageSize=1000,
                                                        fields='files(id, name, mimeType, size, teamDriveId, parents)',
                                                        orderBy='folder, modifiedTime desc'), request_id=index)
        else:
            self.__batch.add(self.__service.files().list(q=query + " and 'me' in owners",
                                                        pageSize=1000,
                                                        spaces='drive',
                                                        fields='files(id, name, mimeType, size, parents)',
                                                        orderBy='folder, modifiedTime desc'), request_id=index)


    def batch_response_callback(self, request_id, response, exception):
        if exception is not None:
            LOGGER.exception(f"Failed to call the drive api")
            LOGGER.exception(exception)
        if response["files"] is not None:
            response = response["files"]
        else:
            response = self.drive_query_backup( DRIVE_ID[int(request_id)] )
        self.responses[int(request_id)] = response


    def recursive_list_callback(self, index, result, exception):
        if exception is not None:
            LOGGER.exception(f"Failed to get recursive drive dir list")
            LOGGER.exception(exception)
        else:
            self.dir_list[result[0]][index] = result[1]

    def drive_list(self, file_name):

        token_service = self.alt_authorize()
        if token_service is not None:
            self.__service = token_service
        self.__batch = self.__service.new_batch_http_request(callback=self.batch_response_callback)

        query = ""
        file_name = self.escapes(file_name)
        if re.search("^-d ", file_name, re.IGNORECASE):
            query += "mimeType = 'application/vnd.google-apps.folder' and "
            file_name = file_name[2: len(file_name)]
        elif re.search("^-f ", file_name, re.IGNORECASE):
            query += "mimeType != 'application/vnd.google-apps.folder' and "
            file_name = file_name[2: len(file_name)]
        if len(file_name) > 2:
            remove_list = ['A', 'a', 'X', 'x']
            if file_name[1] == ' ' and file_name[0] in remove_list:
                file_name = file_name[2: len(file_name)]

        var = re.split('[ ._,\\[\\]-]+', file_name)
        for text in var:
            if text != '':
                query += f"name contains '{text}' and "
        query += "trashed=false"

        index = 0
        start_time = time.time()
        self.file_name = file_name

        for parent_id in DRIVE_ID:
            self.responses[index] = None
            self.drive_query(str(index), parent_id, query)
            if index + 1 % 100 == 0:
                self.__batch.execute()
            index += 1
        if index + 1 % 100 != 0:
            self.__batch.execute()

        THREADS = 0
        queue = Queue()
        for index, response in self.responses.items():
            if INDEX_URL[index] is not None:
                self.dir_list[DRIVE_ID[index]] = {}
                if response:
                    count = -1
                    for file in response:
                        count += 1
                        if THREADS < MAX_THREADS:
                            worker = ThreadWorker(queue, function=self.get_recursive_list, callback=self.recursive_list_callback)
                            worker.start()
                            THREADS += 1
                        queue.put((count, (file, DRIVE_ID[index])))

        #wait until all threading processes are finished
        queue.join()

        msg = ''
        content_count = 0
        reached_max_limit = False
        add_title_msg = True
        for index, response in self.responses.items():
            parent_id = DRIVE_ID[index]
            add_drive_title = True
            if response:
                count = -1
                for file in response:
                    count += 1
                    if add_title_msg:
                        msg = f'<h4>Query: {file_name}</h4><br>'
                        add_title_msg = False
                    if add_drive_title:
                        msg += f"╾────────────╼<br><b>{DRIVE_NAME[index]}</b><br>╾────────────╼<br>"
                        add_drive_title = False

                    # Detect whether current entity is a folder or file
                    if file.get('mimeType') == "application/vnd.google-apps.folder":
                        msg += f"🗂️<code>{file.get('name')}</code> <b>(folder)</b><br>" \
                               f"<b><a href='https://drive.google.com/drive/folders/{file.get('id')}'>Drive Link</a></b>"
                        if INDEX_URL[index] is not None:
                            url_path = "/".join(
                                [requests.utils.quote(n, safe='') for n in self.dir_list[parent_id][count]])
                            url = f'{INDEX_URL[index]}/{url_path}/'
                            msg += f'<b> | <a href="{url}">Index Link</a></b>'
                    else:
                        msg += f"📄<code>{file.get('name')}</code> <b>({get_readable_file_size(int(file.get('size', 0)))})" \
                               f"</b><br><b><a href='https://drive.google.com/uc?id={file.get('id')}" \
                               f"&export=download'>Drive Link</a></b>"
                        if INDEX_URL[index] is not None:
                            url_path = "/".join(
                                [requests.utils.quote(n, safe='') for n in self.dir_list[parent_id][count]])
                            url = f'{INDEX_URL[index]}/{url_path}'
                            msg += f'<b> | <a href="{url}">Index Link</a></b>'

                    msg += '<br><br>'
                    content_count += 1
                    if content_count % telegraph_limit == 0:
                        self.telegraph_content.append(msg)
                        msg = ""

        if msg != '':
            self.telegraph_content.append(msg)

        msg = f"Found {content_count} results in {round(time.time() - start_time, 2)}s"


        total_pages = len(self.telegraph_content)
        if total_pages == 0:
            return "Found nothing", None

        acc_no=-1
        tg_pg_acc = len(telegra_ph)
        page_per_acc = 3
        for i in range(total_pages):

            if i % page_per_acc == 0:
                acc_no = (acc_no+1) % tg_pg_acc

            ## Add prev page link
            if i != 0:
                self.telegraph_content[i] +=  f'<b><a href="https://telegra.ph/{self.path[i-1]}">Prev</a> | Page {i+1}/{total_pages}</b>'
            else:
                self.telegraph_content[i] += f'<b>Page {i+1}/{total_pages}</b>'

            try:
                self.path.append(
                telegra_ph[acc_no].create_page(title='SearchX',
                                          author_name='XXX',
                                          author_url='https://github.com/hsj51/SearchX',
                                          html_content=self.telegraph_content[i])['path'])
            except RetryAfterError as e:
                LOGGER.info(f"Telegra.ph limit hit, sleeping for {e.retry_after}s")
                time.sleep(e.retry_after)
                self.path.append(
                telegra_ph[acc_no].create_page(title='SearchX',
                                          author_name='XXX',
                                          author_url='https://github.com/hsj51/SearchX',
                                          html_content=self.telegraph_content[i])['path'])

            if i != 0:
                ## Edit prev page to add next page link
                self.telegraph_content[i-1] += f'<b> | <a href="https://telegra.ph/{self.path[i]}">Next</a></b>'
                try:
                    telegra_ph[ (acc_no - 1) if i % page_per_acc == 0 else acc_no ].edit_page(path = self.path[i-1],
                                    title = 'SearchX',
                                    author_name='XXX',
                                    author_url='https://github.com/hsj51/SearchX',
                                    html_content=self.telegraph_content[i-1])
                except RetryAfterError as e:
                    LOGGER.info(f"Telegra.ph limit hit, sleeping for {e.retry_after}s")
                    time.sleep(e.retry_after)
                    telegra_ph[ acc_no - 1 if i % page_per_acc == 0 else acc_no ].edit_page(path = self.path[i-1],
                                    title = 'SearchX',
                                    author_name='XXX',
                                    author_url='https://github.com/hsj51/SearchX',
                                    html_content=self.telegraph_content[i-1])


        buttons = button_builder.ButtonMaker()
        buttons.build_button("VIEW HERE", f"https://telegra.ph/{self.path[0]}")

        return msg, InlineKeyboardMarkup(buttons.build_menu(1))
