#!/usr/bin/env python
#
# Copyright (C) 2014  Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from logging import debug, error

from base64 import b64encode, b64decode
import hashlib
import hmac
import json
import os
import socket
import subprocess
import tempfile
import urllib
import time

import requests
from enum import Enum

HMAC_HEADER = 'X-Ycm-Hmac'
HMAC_SECRET_LENGTH = 16
SERVER_IDLE_SUICIDE_SECONDS = 10800  # 3 hours
MAX_SERVER_WAIT_TIME_SECONDS = 5

# Set this to True to see ycmd's output interleaved with the client's
INCLUDE_YCMD_OUTPUT = False
DEFINED_SUBCOMMANDS_HANDLER = '/defined_subcommands'
CODE_COMPLETIONS_HANDLER = '/completions'
COMPLETER_COMMANDS_HANDLER = '/run_completer_command'
EVENT_HANDLER = '/event_notification'
EXTRA_CONF_HANDLER = '/load_extra_conf_file'
DIR_OF_THIS_SCRIPT = os.path.dirname(os.path.abspath(__file__))
PATH_TO_YCMD = os.path.join(DIR_OF_THIS_SCRIPT, 'ycmd', 'ycmd')
PATH_TO_EXTRA_CONF = os.path.join(DIR_OF_THIS_SCRIPT, 'ycmd', 'examples',
                                  '.ycm_extra_conf.py')


class Event(Enum):
    FileReadyToParse = 1
    BufferUnload = 2
    BufferVisit = 3
    InsertLeave = 4
    CurrentIdentifierFinished = 5


# Wrapper around ycmd's HTTP+JSON API
class YcmdHandle(object):

    def __init__(self, popen_handle, port, hmac_secret):
        self._popen_handle = popen_handle
        self._port = port
        self._hmac_secret = hmac_secret
        self._server_location = 'http://127.0.0.1:' + str(port)

    @classmethod
    def StartYcmdAndReturnHandle(cls):
        prepared_options = DefaultSettings()
        hmac_secret = os.urandom(HMAC_SECRET_LENGTH)
        b64_hmac_secret = b64encode(hmac_secret)
        ascii_hmac_secret = b64_hmac_secret.decode('ascii')
        prepared_options['hmac_secret'] = ascii_hmac_secret

        # The temp options file is deleted by ycmd during startup
        with tempfile.NamedTemporaryFile(delete=False) as options_file:
            result = json.dumps(prepared_options).encode('utf-8')
            options_file.write(result)
            options_file.flush()
            server_port = GetUnusedLocalhostPort()
            ycmd_args = ['python',
                         PATH_TO_YCMD,
                         '--port={0}'.format(server_port),
                         '--options_file={0}'.format(options_file.name),
                         '--idle_suicide_seconds={0}'.format(
                             SERVER_IDLE_SUICIDE_SECONDS)]

            std_handles = None if INCLUDE_YCMD_OUTPUT else subprocess.PIPE
            child_handle = subprocess.Popen(ycmd_args,
                                            stdout=std_handles,
                                            stderr=std_handles)
            return cls(child_handle, server_port, hmac_secret)

    def IsAlive(self):
        returncode = self._popen_handle.poll()
        # When the process hasn't finished yet, poll() returns None.
        return returncode is None

    def IsReady(self, include_subservers=False):
        if not self.IsAlive():
            return False
        params = {'include_subservers': 1} if include_subservers else None
        try:
            response = self.GetFromHandler('ready', params)
            response.raise_for_status()
        except requests.ConnectionError as e:
            error(e)
            return False
        return response.json()

    def Shutdown(self):
        if self.IsAlive():
            self._popen_handle.terminate()

    def GetFromHandler(self, handler, params=None):
        response = requests.get(self._BuildUri(handler),
                                headers=self._ExtraHeaders(),
                                params=params)
        self._ValidateResponseObject(response)
        return response

    def PostToHandler(self, handler, data, params=None):
        response = requests.post(self._BuildUri(handler),
                                 json=data,
                                 headers=self._ExtraHeaders(json.dumps(data)),
                                 params=params)
        self._ValidateResponseObject(response)
        return response

    #def SendDefinedSubcommandsRequest(self, completer_target):
        #request_json = BuildRequestData(completer_target=completer_target)
        #debug('==== Sending defined subcommands request ====')
        #self.PostToHandlerAndLog(DEFINED_SUBCOMMANDS_HANDLER, request_json)

    def SendCodeCompletionRequest(self, test_filename, filetype, line_num, column_num):
        request_json = BuildRequestData(test_filename=test_filename,
                                        filetype=filetype,
                                        line_num=line_num,
                                        column_num=column_num)
        debug('==== Sending code-completion request ====')
        response = self.PostToHandler(CODE_COMPLETIONS_HANDLER, request_json)
        return json.loads(response.text)

    #def SendGoToRequest(self, test_filename, filetype, line_num, column_num):
        #request_json = BuildRequestData(test_filename=test_filename,
                                        #command_arguments=['GoTo'],
                                        #filetype=filetype,
                                        #line_num=line_num,
                                        #column_num=column_num)
        #debug('==== Sending GoTo request ====')
        #self.PostToHandlerAndLog(COMPLETER_COMMANDS_HANDLER, request_json)

    def SendEventNotification(self, event_enum, test_filename, filetype,
                              line_num=1,  # just placeholder values
                              column_num=1,
                              extra_data=None):
        request_json = BuildRequestData(test_filename=test_filename,
                                        filetype=filetype,
                                        line_num=line_num,
                                        column_num=column_num)
        if extra_data:
            request_json.update(extra_data)
        request_json['event_name'] = event_enum.name
        debug('==== Sending event notification ====')
        response = self.PostToHandler(EVENT_HANDLER, request_json)
        return json.loads(response.text)

    def LoadExtraConfFile(self, extra_conf_filename):
        request_json = {'filepath': extra_conf_filename}
        self.PostToHandler(EXTRA_CONF_HANDLER, request_json)

    def WaitUntilReady(self, include_subservers=False):
        total_slept = 0
        time.sleep(0.5)
        total_slept += 0.5
        while True:
            try:
                if total_slept > MAX_SERVER_WAIT_TIME_SECONDS:
                    raise RuntimeError(
                        'waited for the server for {0} seconds, aborting'.format(
                            MAX_SERVER_WAIT_TIME_SECONDS))

                if self.IsReady(include_subservers):
                    return
            except requests.exceptions.ConnectionError:
                pass
            finally:
                time.sleep(0.1)
                total_slept += 0.1

    def _ExtraHeaders(self, request_body=None):
        return {HMAC_HEADER: self._HmacForBody(request_body)}

    def _HmacForBody(self, request_body=''):
        result = CreateHexHmac(request_body, self._hmac_secret).encode('utf-8')
        return b64encode(result).decode('utf-8')

    def _BuildUri(self, handler):
        return urllib.parse.urljoin(self._server_location, handler)

    def _ValidateResponseObject(self, response):
        if not ContentHexHmacValid(response.content,
                                   b64decode(response.headers[HMAC_HEADER]),
                                   self._hmac_secret):
            raise RuntimeError('Received invalid HMAC for response!')
        return True


def ContentHexHmacValid(content, hmac_value, hmac_secret):
    return hmac.compare_digest(CreateHexHmac(content, hmac_secret).encode('utf-8'),
                               hmac_value)


def CreateHexHmac(content, hmac_secret):
    """Returns bytes object"""
    # Must ensure that hmac_secret is str and not unicode
    if type(content) == str:
        content = content.encode('utf-8')
    result = hmac.new(bytes(hmac_secret),
                      msg=content,
                      digestmod=hashlib.sha256)
    return result.hexdigest()


def PathToTestFile(filename):
    return os.path.join(DIR_OF_THIS_SCRIPT, 'ycmd', 'examples', 'samples', filename)


def DefaultSettings():
    default_options_path = os.path.join(DIR_OF_THIS_SCRIPT, 'ycmd', 'ycmd',
                                        'default_settings.json')

    with open(default_options_path) as f:
        return json.loads(f.read())


def GetUnusedLocalhostPort():
    sock = socket.socket()
    # This tells the OS to give us any free port in the range [1024 - 65535]
    sock.bind(('', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def PrettyPrintDict(value):
    # Sad that this works better than pprint...
    return json.dumps(value, sort_keys=True, indent=2).replace(
        '\\n', '\n')


def BuildRequestData(test_filename=None,
                     filetype=None,
                     line_num=None,
                     column_num=None,
                     command_arguments=None,
                     completer_target=None):
    test_path = PathToTestFile(test_filename) if test_filename else ''

    # Normally, this would be the contents of the file as loaded in the editor
    # (possibly unsaved data).
    contents = open(test_path).read() if test_path else ''

    data = {
        'line_num': line_num,
        'column_num': column_num,
        'filepath': test_path,
        'file_data': {
            test_path: {
                'filetypes': [filetype],
                'contents': contents
            }
        }
    }

    if command_arguments:
        data['command_arguments'] = command_arguments
    if completer_target:
        data['completer_target'] = completer_target

    return data


def Main():
    print('Trying to start server...')
    server = YcmdHandle.StartYcmdAndReturnHandle()
    server.WaitUntilReady()

    LanguageAgnosticIdentifierCompletion(server)
    # server.GetFromHandler()
    # PythonSemanticCompletionResults(server)
    # CppSemanticCompletionResults(server)
    # CsharpSemanticCompletionResults(server)

    # This will ask the server for a list of subcommands supported by a given
    # language completer.
    # PythonGetSupportedCommands(server)

    # GoTo is an example of a completer subcommand.
    # Python and C# completers also support the GoTo subcommand.
    # CppGotoDeclaration(server)

    print('Shutting down server...')
    server.Shutdown()


def LanguageAgnosticIdentifierCompletion(server):
    # We're using JavaScript here, but the language doesn't matter; the identifier
    # completion engine just extracts identifiers.
    # server.SendEventNotification(Event.FileReadyToParse,
                                 # test_filename='some_javascript.js',
                                 # filetype='javascript')

    print(server.SendCodeCompletionRequest(test_filename='some_javascript.js',
                                           filetype='javascript',
                                           line_num=21,
                                           column_num=6))


def PythonSemanticCompletionResults(server):
    server.SendEventNotification(Event.FileReadyToParse,
                                 test_filename='some_python.py',
                                 filetype='python')

    server.SendCodeCompletionRequest(test_filename='some_python.py',
                                     filetype='python',
                                     line_num=27,
                                     column_num=6)


def CppSemanticCompletionResults(server):
    server.LoadExtraConfFile(PATH_TO_EXTRA_CONF)

    # NOTE: The server will return diagnostic information about an error in the
    # some_cpp.cpp file that we placed there intentionally (as an example).
    # Clang will recover from this error and still manage to parse the file
    # though.
    server.SendEventNotification(Event.FileReadyToParse,
                                 test_filename='some_cpp.cpp',
                                 filetype='cpp')

    server.SendCodeCompletionRequest(test_filename='some_cpp.cpp',
                                     filetype='cpp',
                                     line_num=25,
                                     column_num=7)


def PythonGetSupportedCommands(server):
    server.SendDefinedSubcommandsRequest(completer_target='python')


def CppGotoDeclaration(server):
    # NOTE: No need to load extra conf file or send FileReadyToParse event, it was
    # already done in CppSemanticCompletionResults.
    server.SendGoToRequest(test_filename='some_cpp.cpp',
                           filetype='cpp',
                           line_num=23,
                           column_num=4)


def CsharpSemanticCompletionResults(server):
    # First such request starts the OmniSharpServer
    server.SendEventNotification(Event.FileReadyToParse,
                                 test_filename='some_csharp.cs',
                                 filetype='cs')

    # We have to wait until OmniSharpServer has started and loaded the solution
    # file
    debug('Waiting for OmniSharpServer to become ready...')
    server.WaitUntilReady(include_subservers=True)
    server.SendCodeCompletionRequest(test_filename='some_csharp.cs',
                                     filetype='cs',
                                     line_num=10,
                                     column_num=15)


if __name__ == "__main__":
    Main()
