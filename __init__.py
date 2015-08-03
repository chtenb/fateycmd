from fate.document import Document
from fate.completer import Completer
from fate.navigation import position_to_coord, coord_to_position
from logging import info, error, debug
from .client import YcmdHandle, Event
from tempfile import gettempdir


class YcmCompleter(Completer):

    """Completer class for fate that uses the ycm engine."""

    def __init__(self, doc):
        Completer.__init__(self)

        info('Trying to start ycm server...')
        self.ycmhandle = YcmdHandle.StartYcmdAndReturnHandle()
        info('Ycm server started successfully...')
        doc.OnQuit.add(self.exit_ycmcompleter)

    def exit_ycmcompleter(self):
        info('Trying to shut down ycm server...')
        self.ycmhandle.Shutdown()

    def parse_file(self):
        if self.ycmhandle.IsReady():
            self.doc.tempfile = save_tmp_file(self.doc)
            self.ycmhandle.SendEventNotification(Event.FileReadyToParse,
                                                 test_filename=self.doc.tempfile,
                                                 filetype=self.doc.filetype)

    def complete(self):
        doc = self.doc
        if self.ycmhandle.IsReady() and hasattr(doc, 'tempfile'):
            # It may happen that the server was not ready for parsing, but is
            # ready now
            line, column = position_to_coord(
                doc.mode.cursor_position(doc), doc.text)
            info((line, column))
            result = doc.completer.SendCodeCompletionRequest(test_filename=doc.tempfile,
                                                             filetype=doc.filetype,
                                                             line_num=line,
                                                             column_num=column)
            completions = [item['insertion_text']
                           for item in result['completions']]
            start_column = result['completion_start_column']
            start_position = coord_to_position(line, start_column, doc.text)
            debug('startcolumn: {}'.format(start_column))
            debug('startpos: {}'.format(start_position))
            return start_position, completions


def init_ycmcompleter(doc):
    doc.completer = YcmCompleter(doc)

Document.OnDocumentInit.add(init_ycmcompleter)


def save_tmp_file(doc):
    tempfile = gettempdir() + '/' + doc.filename.replace('/', '_') + '.fatemp'
    with open(tempfile, 'w') as fd:
        fd.write(doc.text)
    return tempfile

