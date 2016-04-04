# This file is part of HDL Code Checker.
#
# HDL Code Checker is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# HDL Code Checker is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with HDL Code Checker.  If not, see <http://www.gnu.org/licenses/>.
"ModelSim builder implementation"

import os
import os.path as p
import re
from .base_builder import BaseBuilder
from hdlcc.exceptions import SanityCheckError

class MSim(BaseBuilder):
    '''Builder implementation of the ModelSim compiler'''

    # Implementation of abstract class properties
    __builder_name__ = 'msim'

    # MSim specific class properties
    _BuilderStdoutMessageScanner = re.compile('|'.join([
        r"^\*\*\s*([WE])\w+:\s*",
        r"\((\d+)\):",
        r"[\[\(]([\w-]+)[\]\)]\s*",
        r"(.*\.(vhd|sv|svh)\b)",
        r"\s*\(([\w-]+)\)",
        r"\s*(.+)",
        ]), re.I)

    _BuilderStdoutIgnoreLines = re.compile('|'.join([
        r"^\s*$",
        r"^(?!\*\*\s(Error|Warning):).*",
        r".*VHDL Compiler exiting\s*$"]))

    _IterRebuildUnits = re.compile(
        r"(" \
            r"Recompile\s*(?P<lib_name_0>\w+)\.(?P<unit_name_0>\w+)\s+because" \
            r"\s+[^\s]+\s+has changed"
        r"|" \
            r"^\*\* Warning:.*\(vcom-1127\)\s*Entity\s(?P<lib_name_1>\w+)\." \
            r"(?P<unit_name_1>\w+).*"
        r")").finditer

    _BuilderLibraryScanner = re.compile(
        r"^\"(?P<library_name>\w+)\""
        r"\s+maps to directory\s*"
        r"(?P<library_path>.*)\.$", re.I)

    def _shouldIgnoreLine(self, line):
        return self._BuilderStdoutIgnoreLines.match(line)

    def __init__(self, target_folder):
        self._version = ''
        super(MSim, self).__init__(target_folder)
        self._modelsim_ini = p.join(self._target_folder, 'modelsim.ini')

        # Use vlib with '-type directory' switch to get a more consistent
        # folder organization. The vlib command has 3 variants:
        # - version <= 6.2: "Old" library organization
        # - 6.3 <= version <= 10.2: Has the switch but defaults to directory
        # version >= 10.2+: Has the switch and the default is not directory
        if self._version >= '10.2':
            self._vlib_args = ['-type', 'directory']
        else:
            self._vlib_args = []
        self._logger.debug("vlib arguments: '%s'", str(self._vlib_args))

    def _makeMessageRecords(self, line):
        line_number = None
        column = None
        filename = None
        error_number = None
        error_type = None
        error_message = None

        scan = self._BuilderStdoutMessageScanner.scanner(line)

        while True:
            match = scan.match()
            if not match:
                break

            if match.lastindex == 1:
                error_type = match.group(match.lastindex)
            if match.lastindex == 2:
                line_number = match.group(match.lastindex)
            if match.lastindex in (3, 6):
                try:
                    error_number = \
                            re.findall(r"\d+", match.group(match.lastindex))[0]
                except IndexError:
                    error_number = 0
            if match.lastindex == 4:
                filename = match.group(match.lastindex)
            if match.lastindex == 7:
                error_message = match.group(match.lastindex)

        return [{
            'checker'        : self.__builder_name__,
            'line_number'    : line_number,
            'column'         : column,
            'filename'       : filename,
            'error_number'   : error_number,
            'error_type'     : error_type,
            'error_message'  : error_message,
        }]

    def checkEnvironment(self):
        try:
            stdout = self._subprocessRunner(['vcom', '-version'])
            self._version = \
                    re.findall(r"(?<=vcom)\s+([\w\.]+)\s+(?=Compiler)", \
                    stdout[0])[0]
            self._logger.debug("vcom version string: '%s'. " + \
                    "Version number is '%s'", \
                    stdout, self._version)
        except Exception as exc:
            import traceback
            self._logger.warning("Sanity check failed:\n%s",
                                 traceback.format_exc())
            raise SanityCheckError(self.__builder_name__, str(exc))

    def _parseBuiltinLibraries(self):
        "Discovers libraries that exist regardless before we do anything"
        self._createIniFile()
        for line in self._subprocessRunner(['vmap', ]):
            for match in self._BuilderLibraryScanner.finditer(line):
                self._builtin_libraries.append(match.groupdict()['library_name'])

    def getBuiltinLibraries(self):
        return self._builtin_libraries

    def _getUnitsToRebuild(self, line):
        rebuilds = []
        for match in self._IterRebuildUnits(line):
            if not match:
                continue
            mdict = match.groupdict()
            library_name = mdict['lib_name_0'] or mdict['lib_name_1']
            unit_name = mdict['unit_name_0'] or mdict['unit_name_1']
            if None not in (library_name, unit_name):
                rebuilds.append({'library_name' : library_name,
                                 'unit_name' : unit_name})
            else: # pragma: no cover
                _msg = "Something wrong while parsing '%s'. " \
                        "Match is '%s'" % (line, mdict)
                self._logger.error(_msg)
                assert 0, _msg

        return rebuilds

    def _buildSource(self, source, flags=None):
        cmd = ['vcom', '-modelsimini', self._modelsim_ini, '-quiet',
               '-work', p.join(self._target_folder, source.library)]
        if flags:
            cmd += flags
        cmd += [source.filename]

        return self._subprocessRunner(cmd)

    def _createLibrary(self, source):
        try:
            if p.exists(p.join(self._target_folder, source.library)):
                return
            self._mapLibrary(source.library)
        except: # pragma: no cover
            self._logger.debug("Current dir when exception was raised: %s",
                               p.abspath(os.curdir))
            raise

    def _createIniFile(self):
        "Adds a library to a non-existent ModelSim init file"
        _modelsim_ini = p.join(self._target_folder, 'modelsim.ini')

        if p.exists(_modelsim_ini):
            self._logger.warning("modelsim.ini already exists at '%s', "
                                 "returning", _modelsim_ini)
            return
        self._logger.info("modelsim.ini not found at '%s', creating",
                          p.abspath(_modelsim_ini))

        cwd = p.abspath(os.curdir)
        self._logger.debug("Current dir is %s, changing to %s",
                           cwd, self._target_folder)
        os.chdir(self._target_folder)
        if cwd == os.curdir: # pragma: no cover
            self._logger.fatal("cwd: %s, curdir: %s, error!", cwd, os.curdir)
            assert 0


        self._subprocessRunner(['vmap', '-c'])

        self._logger.debug("After vmap at '%s'", p.abspath(os.curdir))
        for _dir in os.listdir(p.abspath(os.curdir)):
            self._logger.debug("- '%s'", _dir)

        self._logger.debug("Current dir is %s, changing to %s",
                           p.abspath(os.curdir), cwd)
        os.chdir(cwd)

    def deleteLibrary(self, library):
        "Deletes a library from ModelSim init file"
        if not p.exists(p.join(self._target_folder, library)):
            self._logger.warning("Library %s doesn't exists", library)
            return
        return self._subprocessRunner(
            ['vdel', '-modelsimini', self._modelsim_ini, '-lib', library,
             '-all'])

    def _mapLibrary(self, library):
        "Adds a library to an existing ModelSim init file"
        self._logger.debug("modelsim.ini found, adding %s", library)

        self._subprocessRunner(['vlib', ] + self._vlib_args +
                               [p.join(self._target_folder, library)])

        self._subprocessRunner(['vmap', '-modelsimini', self._modelsim_ini,
                                library, p.join(self._target_folder, library)])


