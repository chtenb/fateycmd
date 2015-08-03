from ..document import Document
from ..navigation import position_to_coord, coord_to_position
from logging import info, error, debug
from .client import YcmdHandle, Event
from tempfile import gettempdir


def start_ycm_server(doc):
    # TODO: Only start server if wanted
    info('Trying to start ycm server...')

    server = YcmdHandle.StartYcmdAndReturnHandle()
    #server.WaitUntilReady()
    doc.completer = server
    info('Ycm server started successfully...')
    doc.OnQuit.add(exit_ycm_server)


def exit_ycm_server(doc):
    info('Trying to shut down ycm server...')
    doc.completer.Shutdown()

Document.OnDocumentInit.add(start_ycm_server)
Document.completer = None


def save_tmp_file(doc):
    tempfile = gettempdir() + '/' + doc.filename.replace('/', '_') + '.fatemp'
    with open(tempfile, 'w') as fd:
        fd.write(doc.text)
    return tempfile


def parse_file(doc):
    if doc.completer.IsReady():
        doc.tempfile = save_tmp_file(doc)
        doc.completer.SendEventNotification(Event.FileReadyToParse,
                                            test_filename=doc.tempfile,
                                            filetype=doc.filetype)


def complete(doc):
    if doc.completer.IsReady() and hasattr(doc, 'tempfile'):
        # It may happen that the server was not ready for parsing, but is ready now
        line, column = position_to_coord(doc.mode.cursor_position(doc), doc.text)
        info((line, column))
        result = doc.completer.SendCodeCompletionRequest(test_filename=doc.tempfile,
                                                         filetype=doc.filetype,
                                                         line_num=line,
                                                         column_num=column)
        completions = [item['insertion_text'] for item in result['completions']]
        start_column = result['completion_start_column']
        start_position = coord_to_position(line, start_column, doc.text)
        debug('startcolumn: {}'.format(start_column))
        debug('startpos: {}'.format(start_position))
        return start_position, completions
