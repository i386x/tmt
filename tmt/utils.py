
""" Test Metadata Utilities """

import contextlib
import copy
import dataclasses
import datetime
import functools
import io
import os
import pathlib
import pprint
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import unicodedata
import urllib.parse
from collections import OrderedDict
from functools import lru_cache
from threading import Thread
from typing import (IO, TYPE_CHECKING, Any, Callable, Dict, Generator, Generic,
                    Iterable, List, NamedTuple, Optional, Pattern, Sequence,
                    Tuple, Type, TypeVar, Union, cast, overload)

import click
import fmf
import jinja2
import jinja2.exceptions
import jsonschema
import pkg_resources
import requests
import requests.adapters
import requests.packages.urllib3.util.retry
import urllib3.exceptions
from click import echo, style, wrap_text
from ruamel.yaml import YAML, scalarstring
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.parser import ParserError
from ruamel.yaml.representer import Representer

if sys.version_info >= (3, 8):
    from typing import Literal, Protocol
else:
    from typing_extensions import Literal, Protocol

import tmt.log

if TYPE_CHECKING:
    import tmt.base
    import tmt.cli
    import tmt.steps


class Path(pathlib.PosixPath):
    # Apparently, `pathlib`` does not offer `relpath` transition between
    # parallel trees, instead, a `ValueError`` is raised when `self` does not
    # lie under `other`. Overriding the original implementation with one based
    # on `os.path.relpath()`, which is more suited to tmt code base needs.
    #
    # ignore[override]: does not match the signature on purpose, our use is
    # slightly less generic that what pathlib supports, to be usable with
    # os.path.relpath.
    def relative_to(self, other: Union[str, 'Path']) -> 'Path':  # type: ignore[override]
        return Path(os.path.relpath(self, other))

    # * `Path.is_relative_to()`` has been added in 3.9
    # https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.is_relative_to
    #
    # * The original implementation calls `relative_to()`, which we just made
    # to return a relative path for all possible inputs, even those the original
    # implementation considers to not be relative to each other. Therefore, we
    # need to override `is_relative_to()` even for other Python versions, to not
    # depend on `ValueError` raised by the original `relative_to()`.
    #
    # ignore[override]: does not match the signature on purpose, our use is
    # slightly less generic that what pathlib supports, to be usable with
    # os.path.relpath.
    def is_relative_to(self, other: 'Path') -> bool:  # type: ignore[override]
        # NOTE: the following is not perfect, but it should be enough for
        # what tmt needs to know about its paths.

        # Acquire the relative path from the one we're given and the other...
        relpath = os.path.relpath(str(self), str(other))

        # ... and if the relative path starts with `..`, it means we had to
        # walk *up* from `other` and descend to `path`, therefore `path` cannot
        # be a subtree of `other`.

        return not relpath.startswith('..')

    def unrooted(self) -> 'Path':
        """ Return the path as if it was not starting in file system root """

        if self.is_absolute():
            return self.relative_to('/')

        return self


log = fmf.utils.Logging('tmt').logger


# Default workdir root and max
WORKDIR_ROOT = Path('/var/tmp/tmt')
WORKDIR_MAX = 1000

# Maximum number of lines of stdout/stderr to show upon errors
OUTPUT_LINES = 100
# Default output width
OUTPUT_WIDTH = 79

# Hierarchy indent
INDENT = 4

# Default name and order for step plugins
DEFAULT_NAME = 'default'
DEFAULT_PLUGIN_ORDER = 50
DEFAULT_PLUGIN_ORDER_MULTIHOST = 10
DEFAULT_PLUGIN_ORDER_REQUIRES = 70
DEFAULT_PLUGIN_ORDER_RECOMMENDS = 75

# Config directory
CONFIG_PATH = Path('~/.config/tmt')

# Special process return code
PROCESS_TIMEOUT = 124

# Default select.select(timeout) in seconds
DEFAULT_SELECT_TIMEOUT = 5

# Default shell and options to be set for all shell scripts
DEFAULT_SHELL = "/bin/bash"
SHELL_OPTIONS = 'set -eo pipefail'

# Defaults for HTTP/HTTPS retries and timeouts (see `retry_session()`).
DEFAULT_RETRY_SESSION_RETRIES: int = 3
DEFAULT_RETRY_SESSION_BACKOFF_FACTOR: float = 0.1

# Defaults for HTTP/HTTPS retries for getting environment file
# Retry with exponential backoff, maximum duration ~511 seconds
ENVFILE_RETRY_SESSION_RETRIES: int = 10
ENVFILE_RETRY_SESSION_BACKOFF_FACTOR: float = 1

# Default for wait()-related options
DEFAULT_WAIT_TICK: float = 30.0
DEFAULT_WAIT_TICK_INCREASE: float = 1.0

# A stand-in variable for generic use.
T = TypeVar('T')

# A FMF context type, representing name/values context.
FmfContextType = Dict[str, List[str]]

# A "environment" type, representing name/value environment variables.
EnvironmentType = Dict[str, str]

# Workdir argument type, can be True, a string, a path or None
WorkdirArgumentType = Union[Literal[True], Path, None]

# Workdir type, can be None or a path
WorkdirType = Optional[Path]

# Option to skip to initialize work tree in plan
PLAN_SKIP_WORKTREE_INIT = 'plan_skip_worktree_init'

# List of schemas that need to be ignored in a plan
PLAN_SCHEMA_IGNORED_IDS: List[str] = [
    '/schemas/provision/hardware',
    '/schemas/provision/kickstart'
    ]


class BaseLoggerFnType(Protocol):
    def __call__(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0,
            level: int = 1) -> None:
        pass


class LevelessLoggerFnType(Protocol):
    def __call__(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0) -> None:
        pass


class SemanticLoggerFnType(Protocol):
    def __call__(self, message: str, shift: int = 0) -> None:
        pass


LoggerFnType = Union[
    BaseLoggerFnType,
    LevelessLoggerFnType,
    SemanticLoggerFnType]


class Config:
    """ User configuration """

    def __init__(self) -> None:
        """ Initialize config directory path """
        self.path = CONFIG_PATH.expanduser()
        if not self.path.exists():
            try:
                self.path.mkdir(parents=True)
            except OSError as error:
                raise GeneralError(
                    f"Failed to create config '{self.path}'.\n{error}")

    @property
    def _last_run_symlink(self) -> Path:
        return self.path / 'last-run'

    @property
    def last_run(self) -> Optional[Path]:
        """ Get the last run workdir path """
        return self._last_run_symlink.resolve() if self._last_run_symlink.is_symlink() else None

    @last_run.setter
    def last_run(self, workdir: Path) -> None:
        """ Set the last run to the given run workdir """

        try:
            self._last_run_symlink.unlink()
        except OSError:
            pass

        try:
            self._last_run_symlink.symlink_to(workdir)
        except FileExistsError:
            # Race when tmt runs in parallel
            log.warning(f"Race condition, unable to save last run '{workdir}'.")
        except OSError as error:
            raise GeneralError(
                f"Unable to save last run '{self.path}'.\n{error}")


# TODO: `StreamLogger` is a dedicated thread fillowing given stream, passing their content to
# tmt's logging methods. Thread is needed because of some amount of blocking involved in the
# process, but it has a side effect of `NO_COLOR` envvar being ignored. When tmt spots `NO_COLOR`
# envvar, it flips a `color` flag in its Click context. But since contexts are thread-local,
# thread powering `StreamLogger` is not aware of this change, and all Click methods it calls
# - namely `echo` and `style` in depths of logging code - would still apply colors depending on
# tty setup.
#
# Passing Click context from the main thread to `StreamLogger` instances to replace their context
# is one way to solve it, another might be logging being more explicit and transparent, e.g. with
# https://github.com/teemtee/tmt/issues/1565.
class StreamLogger(Thread):
    """
    Reading pipes of running process in threads.

    Code based on:
    https://github.com/packit/packit/blob/main/packit/utils/logging.py#L10
    """

    def __init__(self,
                 stream: Optional[IO[bytes]],
                 log_header: str,
                 logger: BaseLoggerFnType,
                 click_context: Optional[click.Context]) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.output: List[str] = []
        self.log_header = log_header
        self.logger = logger
        self.click_context = click_context

    def run(self) -> None:
        if self.stream is None:
            return

        if self.click_context is not None:
            click.globals.push_context(self.click_context)

        for _line in self.stream:
            line = _line.decode('utf-8', errors='replace')
            if line != '':
                self.logger(
                    self.log_header,
                    line.rstrip('\n'),
                    'yellow',
                    level=3)
            self.output.append(line)

    def get_output(self) -> str:
        return "".join(self.output)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Common
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

CommonDerivedType = TypeVar('CommonDerivedType', bound='Common')

#: A single element of command-line.
_CommandElement = str


class ShellScript:
    """ A shell script, a free-form blob of text understood by a shell. """

    def __init__(self, script: str) -> None:
        """
        A shell script, a free-form blob of text understood by a shell.

        :param script: the actual script to be encapsulated by ``ShellScript``
            wrapper.
        """

        self._script = textwrap.dedent(script)

    def __str__(self) -> str:
        return self._script

    def __add__(self, other: 'ShellScript') -> 'ShellScript':
        return ShellScript.from_scripts([self, other])

    def __and__(self, other: 'ShellScript') -> 'ShellScript':
        return ShellScript(f'{self} && {other}')

    def __or__(self, other: 'ShellScript') -> 'ShellScript':
        return ShellScript(f'{self} || {other}')

    @classmethod
    def from_scripts(cls, scripts: List['ShellScript']) -> 'ShellScript':
        """
        Create a single script from many shorter ones.

        Scripts are merged into a single ``ShellScript`` instance, joined
        together with ``;`` character.

        :param scripts: scripts to merge into one.
        """

        return ShellScript('; '.join(script._script for script in scripts))

    def to_element(self) -> _CommandElement:
        """ Convert a shell script to a command element """

        return self._script

    def to_shell_command(self) -> 'Command':
        """
        Convert a shell script into a shell-driven command.

        Turns a shell script into a full-fledged command one might pass to the OS.
        Basically what would ``run(script, shell=True)`` do.
        """

        return Command(DEFAULT_SHELL, '-c', self.to_element())


class Command:
    """ A command with its arguments. """

    def __init__(self, *elements: _CommandElement) -> None:
        self._command = elements

    def __str__(self) -> str:
        return self.to_element()

    def __add__(self, other: Union['Command', List[str]]) -> 'Command':
        if isinstance(other, Command):
            return Command(*self._command, *other._command)

        return Command(*self._command, *other)

    def to_element(self) -> _CommandElement:
        """
        Convert a command to a shell command line element.

        Use when a command or just a list of command options should become a part
        of another command. Common examples of such "higher level" commands
        would be would be ``rsync -e`` or ``ansible-playbook --ssh-common-args``.
        """

        return ' '.join(shlex.quote(s) for s in self._command)

    def to_script(self) -> ShellScript:
        """
        Convert a command to a shell script.

        Use when a command is supposed to become a part of a shell script.
        """

        return ShellScript(' '.join(shlex.quote(s) for s in self._command))

    def to_popen(self) -> List[str]:
        """ Convert a command to form accepted by :py:mod:`subprocess.Popen` """

        return list(self._command)


class CommandOutput(NamedTuple):
    stdout: Optional[str]
    stderr: Optional[str]


class _CommonBase:
    """
    A base class for **all** classes contributing to "common" tree of classes.

    All classes derived from :py:class:`Common` or mixin classes used to enhance
    classes derived from :py:class:`Common` need to have this class as one of
    its most distant ancestors. They should not descend directly from ``object``
    class, ``_CommonBase`` needs to be used instead.

    Our classes and mixins use keyword-only arguments, and with mixins in play,
    we do not have a trivial single-inheritance tree, therefore it's not simple
    to realize when a ``super().__init__`` belongs to ``object``. To deliver
    arguments to all classes, our ``__init__()`` methods must accept all
    parameters, even those they have no immediate use for, and propagate them
    via ``**kwargs``. Sooner or later, one of the classes would try to call
    ``object.__init__(**kwargs)``, but this particular ``__init__()`` accepts
    no keyword arguments, which would lead to an exception.

    ``_CommonBase`` sits at the root of the inheritance tree, and is responsible
    for calling ``object.__init__()`` *with no arguments*. Thanks to method
    resolution order, all "branches" of our tree of common classes should lead
    to ``_CommonBase``, making sure the call to ``object`` is correct. To behave
    correctly, ``_CommonBase`` needs to check which class is the next in the MRO
    sequence, and stop propagating arguments.
    """

    def __init__(self, **kwargs: Any) -> None:
        mro = type(self).__mro__
        # ignore[name-defined]: mypy does not recognize __class__, but it
        # exists and it's documented.
        # https://peps.python.org/pep-3135/
        # https://github.com/python/mypy/issues/4177
        parent = mro[mro.index(__class__) + 1]  # type: ignore[name-defined]

        if parent is object:
            super().__init__()

        else:
            super().__init__(**kwargs)


class Common(_CommonBase):
    """
    Common shared stuff

    Takes care of command line context, options and workdir handling.
    Provides logging functions info(), verbose() and debug().
    Implements read() and write() for comfortable file access.
    Provides the run() method for easy command execution.
    """

    # Command line context, options and workdir
    _context: Optional['tmt.cli.Context'] = None
    # When set to true, _opt will be ignored (default will be returned)
    ignore_class_options: bool = False
    _options: Dict[str, Any] = dict()
    _workdir: WorkdirType = None

    # TODO: must be declared outside of __init__(), because it must exist before
    # __init__() gets called to allow logging helpers work correctly when used
    # from mixins. But that's not very clean, is it? :( Maybe decoupling logging
    # from Common class would help, such a class would be able to initialize
    # itself without involving the rest of Common code. On the other hand,
    # Common owns workdir, for example, whose value affects logging too, so no
    # clear solution so far.
    #
    # Note: cannot use CommonDerivedType - it's a TypeVar filled in by the type
    # given to __init__() and therefore the type it's representing *now* is
    # unknown. but we know `parent` will be derived from `Common` class, so it's
    # mostly fine.
    parent: Optional['Common'] = None

    def inject_logger(self, logger: tmt.log.Logger) -> None:
        self._logger = logger

    def __init__(
            self,
            *,
            parent: Optional[CommonDerivedType] = None,
            name: Optional[str] = None,
            workdir: WorkdirArgumentType = None,
            context: Optional['tmt.cli.Context'] = None,
            relative_indent: int = 1,
            logger: tmt.log.Logger,
            **kwargs: Any) -> None:
        """
        Initialize name and relation with the parent object

        Prepare the workdir for provided id / directory path
        or generate a new workdir name if workdir=True given.
        Store command line context and options for future use
        if context is provided.
        """

        super().__init__(
            parent=parent,
            name=name,
            workdir=workdir,
            context=context,
            relative_indent=relative_indent,
            logger=logger,
            **kwargs)

        # Use lowercase class name as the default name
        self.name = name or self.__class__.__name__.lower()
        self.parent = parent

        # Store command line context
        if context:
            self._save_context_to_instance(context)

            # TODO: not needed here, apparently, it's applied elsewhere, not to
            # each and every Common child.
            # logger.apply_verbosity_options(**self._options)

        self.inject_logger(logger)

        # Prepare a safe variant of the name which does not contain
        # spaces or other special characters to prevent problems with
        # tools which do not expect them (e.g. in directory names).
        self.safe_name = re.sub(r"[^\w/-]+", "-", self.name).strip("-")

        # Relative log indent level shift against the parent
        self._relative_indent = relative_indent

        # Initialize the workdir if requested
        self._workdir_load(workdir)

    def __str__(self) -> str:
        """ Name is the default string representation """
        return self.name

    @classmethod
    def _save_context(cls, context: 'tmt.cli.Context') -> None:
        """ Save provided command line context and options for future use """
        cls._context = context
        cls._options = context.params

    def _save_context_to_instance(self, context: 'tmt.cli.Context') -> None:
        """ Save provided command line context and options to the instance """
        self._context = context
        self._options = context.params

    @overload
    @classmethod
    def _opt(cls, option: str) -> Any:
        pass

    @overload
    @classmethod
    def _opt(cls, option: str, default: T) -> T:
        pass

    @classmethod
    def _opt(cls, option: str, default: Any = None) -> Any:
        """ Get an option from the command line context (class version) """
        if cls.ignore_class_options:
            return default
        return cls._options.get(option, default)

    @property
    def _context_object(self) -> Optional['tmt.cli.ContextObject']:
        if self._context is None:
            return None

        return self._context.obj

    def _fmf_context(self) -> FmfContextType:
        """ Return the current fmf context """
        if self._context_object is None:
            return dict()

        return self._context_object.fmf_context

    def opt(self, option: str, default: Optional[Any] = None) -> Any:
        """
        Get an option from the command line options

        Checks also parent options. For flags (boolean values) parent's
        True wins over child's False (e.g. run --quiet enables quiet
        mode for all included plans and steps).

        For options that can be used multiple times, the child overrides
        the parent if it was defined (e.g. run -av provision -vvv runs
        all steps except for provision in mildly verbose mode, provision
        is run with the most verbosity).

        Environment variables override command line options.
        """
        # Translate dashes to underscores to match click's conversion
        option = option.replace('-', '_')
        # Check the environment first
        # TODO: moved to log.py
        if option == 'debug':
            try:
                debug = os.environ['TMT_DEBUG']
                return int(debug)
            except ValueError:
                raise GeneralError(
                    f"Invalid debug level '{debug}', use an integer.")
            except KeyError:
                pass

        # Get local option
        local = self._options.get(option, default)
        # Check parent option
        parent = None
        if self.parent:
            parent = self.parent.opt(option)
        # Special handling for special flags (parent's yes always wins)
        if option in ['quiet', 'force', 'dry']:
            return parent if parent else local
        # Special handling for counting options (child overrides the
        # parent if it was defined)
        elif option in ['debug', 'verbose']:
            winner = local if local else parent
            if winner is None:
                winner = 0
            return winner
        else:
            return parent if parent is not None else local

    def _level(self) -> int:
        """ Hierarchy level """
        if self.parent is None:
            return -1
        else:
            return self.parent._level() + self._relative_indent

    def _indent(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0) -> str:
        """ Indent message according to the object hierarchy """

        return tmt.log.indent(
            key,
            value=value,
            color=color,
            level=self._level() + shift)

    def print(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0) -> None:
        """ Print a message regardless the quiet mode """
        self._logger.print(key, value=value, color=color, shift=shift)

    def info(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0) -> None:
        """ Show a message unless in quiet mode """
        self._logger.info(key, value=value, color=color, shift=shift)

    def verbose(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0,
            level: int = 1) -> None:
        """
        Show message if in requested verbose mode level

        In quiet mode verbose messages are not displayed.
        """
        self._logger.verbose(key, value=value, color=color, shift=shift, level=level)

    def debug(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 0,
            level: int = 1) -> None:
        """
        Show message if in requested debug mode level

        In quiet mode debug messages are not displayed.
        """
        self._logger.debug(key, value=value, color=color, shift=shift, level=level)

    def warn(self, message: str, shift: int = 0) -> None:
        """ Show a yellow warning message on info level, send to stderr """
        self._logger.warn(message, shift=shift)

    def fail(self, message: str, shift: int = 0) -> None:
        """ Show a red failure message on info level, send to stderr """
        self._logger.fail(message, shift=shift)

    def _command_verbose_logger(
            self,
            key: str,
            value: Optional[str] = None,
            color: Optional[str] = None,
            shift: int = 1,
            level: int = 3) -> None:
        """
        Reports the executed command in verbose mode.

        This is a tailored verbose() function used for command logging where
        default parameters are adjusted (to preserve the function type).
        """
        self.verbose(key=key, value=value, color=color, shift=shift, level=level)

    def _run(self,
             command: Command,
             cwd: Optional[Path],
             shell: bool,
             env: Optional[EnvironmentType],
             log: Optional[BaseLoggerFnType],
             join: bool = False,
             interactive: bool = False,
             timeout: Optional[int] = None) -> CommandOutput:
        """
        Run command, capture the output

        By default stdout and stderr are captured separately.
        Use join=True to merge stderr into stdout.
        Use timeout=<seconds> to finish process after given time
        """
        # By default command ouput is logged using debug
        if not log:
            log = self.debug
        # Prepare the environment
        if env:
            if not isinstance(env, dict):
                raise GeneralError(f"Invalid environment '{env}'.")
            # Do not modify current process environment
            environment = os.environ.copy()
            environment.update(env)
        else:
            environment = None
        self.debug('environment', pprint.pformat(environment), level=4)

        # Set only for shell=True as it would affect command
        executable = DEFAULT_SHELL if shell else None

        # Run the command in interactive mode if requested
        if interactive:
            try:
                subprocess.run(
                    command.to_popen(),
                    cwd=cwd, shell=shell, env=environment, check=True,
                    executable=executable)
            except subprocess.CalledProcessError:
                # Interactive mode can return non-zero if the last command
                # failed, ignore errors here
                pass
            finally:
                return CommandOutput(None, None)

        # Create the process
        try:
            process = subprocess.Popen(
                command.to_popen(),
                cwd=cwd, shell=shell, env=environment,
                start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if join else subprocess.PIPE,
                executable=executable)
        except FileNotFoundError as error:
            raise RunError(
                f"File '{error.filename}' not found.",
                command,
                127,
                caller=self)

        stdout_thread = StreamLogger(
            process.stdout,
            log_header='out',
            logger=log,
            click_context=click.get_current_context(silent=True))
        stderr_thread = stdout_thread
        if not join:
            stderr_thread = StreamLogger(
                process.stderr,
                log_header='err',
                logger=log,
                click_context=click.get_current_context(silent=True))
        stdout_thread.start()
        if not join:
            stderr_thread.start()

        # A bit of logging helpers for debugging duration behavior
        start_timestamp = time.monotonic()

        def _event_timestamp() -> str:
            return f'{time.monotonic() - start_timestamp:.4}'

        def log_event(msg: str) -> None:
            self.debug('Command event', f'{_event_timestamp()} {msg}', level=4)

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log_event(f'duration "{timeout}" exceeded')

            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            log_event('sent SIGKILL signal')

            process.wait()
            log_event('kill confirmed')

            process.returncode = PROCESS_TIMEOUT

        log_event('waiting for stream readers')

        stdout_thread.join()
        log_event('stdout reader done')

        if not join:
            stderr_thread.join()
            log_event('stderr reader done')

        # Handle the exit code, return output
        if process.returncode != 0:
            raise RunError(
                message=f"Command returned '{process.returncode}'.",
                command=command,
                returncode=process.returncode,
                stdout=stdout_thread.get_output(),
                stderr=stderr_thread.get_output(),
                caller=self)
        if join:
            return CommandOutput(
                stdout_thread.get_output(), None)
        else:
            return CommandOutput(
                stdout_thread.get_output(), stderr_thread.get_output())

    def run(self,
            command: Command,
            friendly_command: Optional[str] = None,
            silent: bool = False,
            message: Optional[str] = None,
            cwd: Optional[Path] = None,
            dry: bool = False,
            shell: bool = False,
            env: Optional[EnvironmentType] = None,
            interactive: bool = False,
            join: bool = False,
            log: Optional[BaseLoggerFnType] = None,
            timeout: Optional[int] = None) -> CommandOutput:
        """
        Run command, give message, handle errors

        Command is run in the workdir be default.
        In dry mode commands are not executed unless dry=True.
        Environment is updated with variables from the 'env' dictionary.

        Output is logged using self.debug() or custom 'log' function.
        A user friendly command string 'friendly_command' will be shown,
        if provided, at the beginning of the command output.

        Returns named tuple CommandOutput.
        """

        # A bit of logging - command, default message, error message for later...
        # for debug output we want to rather print actual command rather than
        # the provided printable command
        if isinstance(command, (list, tuple)):
            full_command_string = ' '.join(shlex.quote(s) for s in command)
        else:
            full_command_string = command

        if message:
            self.verbose(message, level=2)

        # Add full command to the debug log, short version to verbose/custom log
        self.debug(f'Run command: {full_command_string}', level=2)
        if not silent and friendly_command:
            logger = log or self.verbose
            logger("cmd", friendly_command, color="yellow", level=2)

        # Nothing more to do in dry mode (unless requested)
        if self.opt('dry') and not dry:
            return CommandOutput(None, None)

        # Run the command, handle the exit code
        cwd = cwd or self.workdir

        # Fail nicely if the working directory does not exist
        if cwd and not cwd.exists():
            raise GeneralError(
                f"The working directory '{cwd}' does not exist.")

        try:
            return self._run(
                command, cwd, shell, env, log if not silent else None, join, interactive, timeout)
        except RunError as error:
            self.debug(error.message, level=3)
            message = f"Failed to run command: {friendly_command} Reason: {error.message}"
            raise RunError(
                message, error.command, error.returncode,
                error.stdout, error.stderr, caller=self)

    def read(self, path: Path, level: int = 2) -> str:
        """ Read a file from the workdir """
        if self.workdir:
            path = self.workdir / path
        self.debug(f"Read file '{path}'.", level=level)
        try:
            with open(path, encoding='utf-8', errors='replace') as data:
                return data.read()
        except OSError as error:
            raise FileError(f"Failed to read '{path}'.\n{error}")

    def write(
            self,
            path: Path,
            data: str,
            mode: str = 'w',
            level: int = 2) -> None:
        """ Write a file to the workdir """
        if self.workdir:
            path = self.workdir / path
        action = 'Append to' if mode == 'a' else 'Write'
        self.debug(f"{action} file '{path}'.", level=level)
        # Dry mode
        if self.opt('dry'):
            return
        try:
            with open(path, mode, encoding='utf-8', errors='replace') as file:
                file.write(data)
        except OSError as error:
            raise FileError(f"Failed to write '{path}'.\n{error}")

    def _workdir_init(self, id_: WorkdirArgumentType = None) -> None:
        """
        Initialize the work directory

        The 'workdir_root' variable is initialized from TMT_WORKDIR_ROOT
        environment variable, if present, otherwise defaults to WORKDIR_ROOT.

        If 'id' is a path, that directory is used instead. Otherwise a
        new workdir is created under the 'workdir_root' directory.
        """

        if 'TMT_WORKDIR_ROOT' in os.environ:
            workdir_root = Path(os.environ['TMT_WORKDIR_ROOT'])
        else:
            workdir_root = WORKDIR_ROOT

        # Prepare the workdir name from given id or path
        if isinstance(id_, Path):
            # Use provided directory if full path given
            if '/' in str(id_):
                workdir = id_
            # Construct directory name under workdir root
            else:
                workdir = workdir_root / id_
            # Resolve any relative paths
            workdir = workdir.resolve()
        # Weird workdir id
        elif id_ is not None:
            raise GeneralError(
                f"Invalid workdir '{id_}', expected a path or None.")

        def _check_or_create_workdir_root_with_perms() -> None:
            """ If created workdir_root has to be 1777 for multi-user"""
            if not workdir_root.is_dir():
                try:
                    workdir_root.mkdir(exist_ok=True, parents=True)
                    workdir_root.chmod(0o1777)
                except OSError as error:
                    raise FileError(f"Failed to prepare workdir '{workdir_root}': {error}")

        if id_ is None:
            # Prepare workdir_root first
            _check_or_create_workdir_root_with_perms()

            # Generated unique id or fail, has to be atomic call
            for id_bit in range(1, WORKDIR_MAX + 1):
                directory = 'run-{}'.format(str(id_bit).rjust(3, '0'))
                workdir = workdir_root / directory
                try:
                    # Call is atomic, no race possible
                    workdir.mkdir(parents=True)
                    break
                except FileExistsError:
                    pass
            else:
                raise GeneralError(
                    f"Workdir full. Cleanup the '{workdir_root}' directory.")
        else:
            # Cleanup possible old workdir if called with --scratch
            if self.opt('scratch'):
                self._workdir_cleanup(workdir)

            if workdir.is_relative_to(workdir_root):
                _check_or_create_workdir_root_with_perms()

            # Create the workdir
            create_directory(workdir, 'workdir', quiet=True)

        # TODO: chicken and egg problem: when `Common` is instantiated, the workdir
        # path might be already known, but it's often not created yet. Therefore
        # a logfile handler cannot be attached to the given logger.
        # This is a problem, as we modify a given logger, and we may modify the
        # incorrect logger, and we may modify 3rd party app logger. The solution
        # to our little logging problem would probably be related to refactoring
        # of workdir creation some day in the future.
        self._logger.add_logfile_handler(workdir / tmt.log.LOG_FILENAME)
        self._workdir = workdir

    def _workdir_name(self) -> Optional[Path]:
        """ Construct work directory name from parent workdir """
        # Need the parent workdir
        if self.parent is None or self.parent.workdir is None:
            return None
        # Join parent name with self
        return self.parent.workdir / self.safe_name.lstrip("/")

    def _workdir_load(self, workdir: WorkdirArgumentType) -> None:
        """
        Create the given workdir if it is not None

        If workdir=True, the directory name is automatically generated.
        """
        if workdir is True:
            self._workdir_init()
        elif workdir is not None:
            self._workdir_init(workdir)

    def _workdir_cleanup(self, path: Optional[Path] = None) -> None:
        """ Clean up the work directory """
        directory = path or self._workdir_name()
        if directory is not None:
            if directory.is_dir():
                self.debug(f"Clean up workdir '{directory}'.", level=2)
                shutil.rmtree(directory)
        self._workdir = None

    @property
    def workdir(self) -> Optional[Path]:
        """ Get the workdir, create if does not exist """
        if self._workdir is None:
            self._workdir = self._workdir_name()
            # Workdir not enabled, even parent does not have one
            if self._workdir is None:
                return None
            # Create a child workdir under the parent workdir
            create_directory(self._workdir, 'workdir', quiet=True)

        return self._workdir

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Exceptions
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


class GeneralError(Exception):
    """ General error """

    def __init__(self, message: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(message, *args, **kwargs)

        self.message = message


class GitUrlError(GeneralError):
    """ Remote git url is not reachable """


class FileError(GeneralError):
    """ File operation error """


class RunError(GeneralError):
    """ Command execution error """

    def __init__(
            self,
            message: str,
            command: Command,
            returncode: int,
            stdout: Optional[str] = None,
            stderr: Optional[str] = None,
            caller: Optional[Common] = None,
            *args: Any,
            **kwargs: Any) -> None:
        super().__init__(message, *args, **kwargs)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        # Store instance of caller to get additional details
        # in post processing (e.g. verbose level)
        self.caller = caller


class MetadataError(GeneralError):
    """ General metadata error """


class SpecificationError(MetadataError):
    """ Metadata specification error """

    def __init__(
            self,
            message: str,
            validation_errors: Optional[List[Tuple[jsonschema.ValidationError, str]]] = None,
            *args: Any,
            **kwargs: Any) -> None:
        super().__init__(message, *args, **kwargs)
        self.validation_errors = validation_errors


class ConvertError(MetadataError):
    """ Metadata conversion error """


class StructuredFieldError(GeneralError):
    """ StructuredField parsing error """


class WaitingIncomplete(GeneralError):
    """ Waiting incomplete """

    def __init__(self) -> None:
        super().__init__('Waiting incomplete')


class WaitingTimedOutError(GeneralError):
    """ Waiting ran out of time """

    def __init__(
            self,
            check: 'WaitCheckType[T]',
            timeout: datetime.timedelta,
            check_success: bool = False) -> None:
        if check_success:
            super().__init__(
                f"Waiting for condition '{check.__name__}' succeeded but took too much time "
                f"after waiting {timeout}."
                )

        else:
            super().__init__(
                f"Waiting for condition '{check.__name__}' timed out "
                f"after waiting {timeout}."
                )

        self.check = check
        self.timeout = timeout
        self.check_success = check_success


# Step exceptions


class DiscoverError(GeneralError):
    """ Discover step error """


class ProvisionError(GeneralError):
    """ Provision step error """


class PrepareError(GeneralError):
    """ Prepare step error """


class ExecuteError(GeneralError):
    """ Execute step error """


class RebootTimeoutError(ExecuteError):
    """ Reboot failed due to a timeout """


class ReportError(GeneralError):
    """ Report step error """


class FinishError(GeneralError):
    """ Finish step error """


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Utilities
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


def uniq(values: List[T]) -> List[T]:
    """ Return a list of all unique items from ``values`` """
    return list(set(values))


def flatten(lists: Generator[List[T], None, None], unique: bool = False) -> List[T]:
    """
    "Flatten" a list of lists into a single-level list.

    :param lists: an iterable of lists to flatten.
    :param unique: if set, duplicate items would be removed, leaving only
        a single instance in the final list.
    :returns: list of items from all given lists.
    """

    flattened: List[T] = sum(lists, [])

    return uniq(flattened) if unique else flattened


def quote(string: str) -> str:
    """ Surround a string with double quotes """
    return f'"{string}"'


def ascii(text: Any) -> bytes:
    """ Transliterate special unicode characters into pure ascii """
    if not isinstance(text, str):
        text = str(text)
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore')


def listify(
        data: Union[Tuple[Any, ...], List[Any], str, Dict[Any, Any]],
        split: bool = False,
        keys: Optional[List[str]] = None) -> Union[List[Any], Dict[Any, Any]]:
    """
    Ensure that variable is a list, convert if necessary
    For dictionaries check all items or only those with provided keys.
    Also split strings on white-space/comma if split=True.
    """
    separator = re.compile(r'[\s,]+')
    if isinstance(data, tuple):
        data = list(data)
    if isinstance(data, list):
        return fmf.utils.split(data, separator) if split else data
    if isinstance(data, str):
        return fmf.utils.split(data, separator) if split else [data]
    if isinstance(data, dict):
        for key in keys or data:
            if key in data:
                data[key] = listify(data[key], split=split)
        return data
    return [data]


def copytree(
        src: Path,
        dst: Path,
        symlinks: bool = False,
        dirs_exist_ok: bool = False,
        ) -> Path:
    """ Similar to shutil.copytree but with dirs_exist_ok for Python < 3.8 """
    # No need to reimplement for newer python or if argument is not requested
    if not dirs_exist_ok or sys.version_info >= (3, 8):
        return cast(
            Path,
            shutil.copytree(src=src, dst=dst, symlinks=symlinks, dirs_exist_ok=dirs_exist_ok))
    # Choice was to either copy python implementation and change ONE line
    # or use rsync (or cp with shell)
    # We need to copy CONTENT of src into dst
    # so src has to end with / and dst cannot
    rsync_src, rsync_dst = str(src), str(dst)
    if rsync_src[-1] != '/':
        rsync_src += '/'
    if rsync_dst[-1] == '/':
        rsync_dst = rsync_dst[:-1]

    command = ["rsync", "-r"]
    if symlinks:
        command.append('-l')
    command.extend([rsync_src, rsync_dst])

    log.debug(f"Calling command '{command}'.")
    outcome = subprocess.run(
        command,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, universal_newlines=True)

    if outcome.returncode != 0:
        raise shutil.Error(
            [f"Unable to copy '{src}' into '{dst}' using rsync.",
             outcome.returncode, outcome.stdout])
    return dst


# These two are helpers for shell_to_dict and environment_to_dict -
# there is some overlap of their functionality.
def _add_simple_var(result: EnvironmentType, var: str) -> None:
    """
    Add a single NAME=VALUE pair into result dictionary

    Parse given string VAR to its constituents, NAME and VALUE, and add
    them to the provided dict.
    """

    matched = re.match("([^=]+)=(.*)", var)
    if not matched:
        raise GeneralError(f"Invalid variable specification '{var}'.")
    name, value = matched.groups()
    result[name] = value


def _add_file_vars(
        *,
        result: EnvironmentType,
        filepath: str,
        logger: tmt.log.Logger) -> None:
    """
    Add variables loaded from file into the result dictionary

    Load mapping from a YAML file 'filepath', and add its content -
    "name: value" entries - to the provided dict.
    """

    if not filepath[1:]:
        raise GeneralError(
            f"Invalid variable file specification '{filepath}'.")

    try:
        with open(filepath[1:], 'r') as file:
            # Handle empty file as an empty environment
            content = file.read()
            if not content:
                logger.warn(f"Empty environment file '{filepath}'.")
                return
            file_vars = yaml_to_dict(content)
    except Exception as exception:
        raise GeneralError(
            f"Failed to load variables from '{filepath}': {exception}")

    for name, value in file_vars.items():
        result[name] = str(value)


def shell_to_dict(variables: Union[str, List[str]]) -> EnvironmentType:
    """
    Convert shell-like variables into a dictionary

    Accepts single string or list of strings. Allowed forms are:
    'X=1'
    'X=1 Y=2 Z=3'
    ['X=1', 'Y=2', 'Z=3']
    ['X=1 Y=2 Z=3', 'A=1 B=2 C=3']
    'TXT="Some text with spaces in it"'
    """
    if not isinstance(variables, (list, tuple)):
        variables = [variables]
    result: EnvironmentType = dict()
    for variable in variables:
        if variable is None:
            continue
        for var in shlex.split(variable):
            _add_simple_var(result, var)

    return result


def environment_to_dict(
        *,
        variables: Union[str, List[str]],
        logger: tmt.log.Logger) -> EnvironmentType:
    """
    Convert environment variables into a dictionary

    Variables may be specified in the following two ways:

    * NAME=VALUE pairs
    * @foo.yaml

    If "variable" starts with "@" character, it is treated as a path to
    a YAML file that contains "key: value" pairs which are then
    transparently loaded and added to the final dictionary.

    In general, allowed inputs are the same as in "shell_to_dict"
    function, with the addition of "@foo.yaml" form:
    'X=1'
    'X=1 Y=2 Z=3'
    ['X=1', 'Y=2', 'Z=3']
    ['X=1 Y=2 Z=3', 'A=1 B=2 C=3']
    'TXT="Some text with spaces in it"'
    @foo.yaml
    @../../bar.yaml
    """

    if not isinstance(variables, (list, tuple)):
        variables = [variables]
    result: EnvironmentType = dict()

    for variable in variables:
        if variable is None:
            continue
        for var in shlex.split(variable):
            if var.startswith('@'):
                _add_file_vars(result=result, filepath=var, logger=logger)
            else:
                _add_simple_var(result, var)

    return result


@lru_cache(maxsize=None)
def environment_file_to_dict(
        *,
        filename: str,
        root: Optional[Path] = None,
        logger: tmt.log.Logger) -> EnvironmentType:
    """
    Read environment variables from the given file.

    File should be in YAML format (``.yaml`` or ``.yml`` suffixes), or in dotenv format.

    .. code-block:: bash
       :caption: dotenv file example

       A=B
       C=D

    .. code-block:: yaml
       :caption: YAML file example

       A: B
       C: D

    Path to each file should be relative to the metadata tree root.

    .. note::

       For loading environment variables from multiple files, see
       :py:func:`environment_files_to_dict`.
    """

    root = root or Path.cwd()
    filename = filename.strip()
    environment_filepath: Optional[Path] = None

    # Fetch a remote file
    if filename.startswith("http"):
        # Create retry session for longer retries, see #1229
        session = retry_session.create(
            retries=ENVFILE_RETRY_SESSION_RETRIES,
            backoff_factor=ENVFILE_RETRY_SESSION_BACKOFF_FACTOR,
            allowed_methods=('GET',),
            status_forcelist=(
                429,  # Too Many Requests
                500,  # Internal Server Error
                502,  # Bad Gateway
                503,  # Service Unavailable
                504   # Gateway Timeout
                ),
            )
        try:
            response = session.get(filename)
            response.raise_for_status()
            content = response.text
        except requests.RequestException as error:
            raise GeneralError(
                f"Failed to fetch the environment file from '{filename}'. "
                f"The problem was: '{error}'")

    # Read a local file
    else:
        # Ensure we don't escape from the metadata tree root

        root = root.resolve()
        environment_filepath = root.joinpath(filename).resolve()

        if not environment_filepath.is_relative_to(root):
            raise GeneralError(
                f"The 'environment-file' path '{environment_filepath}' is outside "
                f"of the metadata tree root '{root}'.")
        if not environment_filepath.is_file():
            raise GeneralError(f"File '{environment_filepath}' doesn't exist.")

        content = environment_filepath.read_text()

    # Parse yaml file
    if os.path.splitext(filename)[1].lower() in ('.yaml', '.yml'):
        environment = parse_yaml(content)

    else:
        try:
            environment = parse_dotenv(content)

        except ValueError:
            raise GeneralError(
                f"Failed to extract variables from environment file "
                f"'{environment_filepath or filename}'. Ensure it has the proper format "
                f"(i.e. A=B).")

    if not environment:
        logger.warn(f"Empty environment file '{filename}'.")

        return {}

    return environment


def environment_files_to_dict(
        *,
        filenames: Iterable[str],
        root: Optional[Path] = None,
        logger: tmt.log.Logger) -> EnvironmentType:
    """
    Read environment variables from the given list of files.

    Files should be in YAML format (``.yaml`` or ``.yml`` suffixes), or in dotenv format.

    .. code-block:: bash
       :caption: dotenv file example

       A=B
       C=D

    .. code-block:: yaml
       :caption: YAML file example

       A: B
       C: D

    Path to each file should be relative to the metadata tree root.

    .. note::

       For loading environment variables from a single file, see
       :py:func:`environment_file_to_dict`, which is a function
       ``environment_files_to_dict()`` calls for each file,
       accumulating data from all input files.
    """

    root = root or Path.cwd()

    result: EnvironmentType = {}

    for filename in filenames:
        result.update(environment_file_to_dict(filename=filename, root=root, logger=logger))

    return result


@contextlib.contextmanager
def modify_environ(
        new_elements: EnvironmentType) -> Generator[None, None, None]:
    """ A context manager for os.environ that restores the initial state """
    environ_backup = os.environ.copy()
    os.environ.clear()
    os.environ.update(new_elements)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(environ_backup)


def context_to_dict(*, context: List[str], logger: tmt.log.Logger) -> FmfContextType:
    """
    Convert command line context definition into a dictionary

    Does the same as environment_to_dict() plus separates possible
    comma-separated values into lists. Here's a couple of examples:

    distro=fedora-33 ---> {'distro': ['fedora']}
    arch=x86_64,ppc64 ---> {'arch': ['x86_64', 'ppc64']}
    """
    return {
        key: value.split(',')
        for key, value in environment_to_dict(variables=context, logger=logger).items()}


def dict_to_yaml(
        data: Union[Dict[str, Any], List[Any], 'tmt.base._RawFmfId'],
        width: Optional[int] = None,
        sort: bool = False,
        start: bool = False) -> str:
    """ Convert dictionary into yaml """
    output = io.StringIO()
    yaml = YAML()
    yaml.indent(mapping=4, sequence=4, offset=2)
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.encoding = 'utf-8'
    yaml.width = width
    yaml.explicit_start = start

    # For simpler dumping of well-known classes
    def _represent_path(representer: Representer, data: Path) -> Any:
        return representer.represent_scalar('tag:yaml.org,2002:str', str(data))

    yaml.representer.add_representer(pathlib.Path, _represent_path)
    yaml.representer.add_representer(pathlib.PosixPath, _represent_path)
    yaml.representer.add_representer(Path, _represent_path)

    # Convert multiline strings
    scalarstring.walk_tree(data)
    if sort:
        # Sort the data https://stackoverflow.com/a/40227545
        sorted_data = CommentedMap()
        for key in sorted(data):
            # ignore[literal-required]: `data` may be either a generic
            # dictionary, or _RawFmfId which allows only a limited set
            # of keys. That spooks mypy, but we do not add any keys,
            # therefore we will not escape TypedDict constraints.
            sorted_data[key] = data[key]  # type: ignore[literal-required]
        data = sorted_data
    yaml.dump(data, output)
    return output.getvalue()


YamlTypType = Literal['rt', 'safe', 'unsafe', 'base']


def yaml_to_dict(data: Any,
                 yaml_type: Optional[YamlTypType] = None) -> Dict[Any, Any]:
    """ Convert yaml into dictionary """
    yaml = YAML(typ=yaml_type)
    loaded_data = yaml.load(data)
    if loaded_data is None:
        return dict()
    if not isinstance(loaded_data, dict):
        raise GeneralError(
            f"Expected dictionary in yaml data, "
            f"got '{type(loaded_data).__name__}'.")
    return loaded_data


def yaml_to_list(data: Any,
                 yaml_type: Optional[YamlTypType] = 'safe') -> List[Any]:
    """ Convert yaml into list """
    yaml = YAML(typ=yaml_type)
    try:
        loaded_data = yaml.load(data)
    except ParserError as error:
        raise GeneralError(f"Invalid yaml syntax: {error}")

    if loaded_data is None:
        return list()
    if not isinstance(loaded_data, list):
        raise GeneralError(
            f"Expected list in yaml data, "
            f"got '{type(loaded_data).__name__}'.")
    return loaded_data


def key_to_option(key: str) -> str:
    """ Convert a key name to corresponding option name """

    return key.replace('_', '-')


def option_to_key(option: str) -> str:
    """ Convert an option name to corresponding key name """

    return option.replace('-', '_')


@dataclasses.dataclass
class FieldMetadata(Generic[T]):
    """
    A dataclass metadata container used by our custom dataclass field management.

    Attached to fields defined with :py:func:`field`
    """

    #: CLI option parameters, for lazy option creation.
    option_args: Optional['FieldCLIOption'] = None
    option_kwargs: Optional[Dict[str, Any]] = None
    option_choices: Union[None, Sequence[str], Callable[[], Sequence[str]]] = None

    #: A :py:func:`click.option` decorator defining a corresponding CLI option.
    _option: Optional['tmt.options.ClickOptionDecoratorType'] = None

    #: A normalization callback to call when loading the value from key source
    #: (performed by :py:class:`NormalizeKeysMixin`).
    normalize_callback: Optional['NormalizeCallback[T]'] = None

    # Callbacks for custom serialize/unserialize operations (performed by
    # :py:class:`SerializableContainer`).
    serialize_callback: Optional['SerializeCallback[T]'] = None
    unserialize_callback: Optional['SerializeCallback[T]'] = None

    @property
    def option(self) -> Optional['tmt.options.ClickOptionDecoratorType']:
        if self._option is None and self.option_args and self.option_kwargs:
            if isinstance(self.option_choices, (list, tuple)):
                self.option_kwargs['type'] = click.Choice(self.option_choices)

            elif callable(self.option_choices):
                self.option_kwargs['type'] = click.Choice(self.option_choices())

            self._option = click.option(
                *self.option_args,
                **self.option_kwargs
                )

        return self._option


def dataclass_field_by_name(cls: Any, name: str) -> 'dataclasses.Field[T]':
    """
    Return a dataclass/data container field info by the field's name.

    Surprisingly, :py:mod:`dataclasses` package does not have a helper for
    this. One can iterate over fields, but there's no *public* API for
    retrieving a field when one knows its name.

    :param cls: a dataclass/data container class whose fields to search.
    :param name: field name to retrieve.
    :raises GeneralError: when the field does not exist.
    """

    for field in dataclasses.fields(cls):
        if field.name == name:
            return field

    else:
        raise GeneralError(f"Could not find field '{name}' in class '{cls.__name__}'.")


def dataclass_field_metadata(field: 'dataclasses.Field[T]') -> 'FieldMetadata[T]':
    """
    Return a dataclass/data container field metadata.

    Dataclass fields have a mapping to hold fields' key/value metadata, and to
    support linters in their job, instead of storing tmt's custom data directly
    in the mapping, we use a special container to hold metadata we need.

    :param field: a dataclass/container field to retrieve metadata for.
    :returns: metadata container, either the one attached to the given field
        or an empty one when field has no metadata.
    """

    return field.metadata.get('tmt', FieldMetadata())


@dataclasses.dataclass
class DataContainer:
    """ A base class for objects that have keys and values """

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to a mapping.

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.
        """

        return dataclasses.asdict(self)

    def to_minimal_dict(self) -> Dict[str, Any]:
        """
        Convert to a mapping with unset keys omitted.

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.
        """

        return {
            key: value for key, value in self.to_dict().items() if value is not None
            }

    # This method should remain a class-method: 1. list of keys is known
    # already, therefore it's not necessary to create an instance, and
    # 2. some functionality makes use of this knowledge.
    @classmethod
    def keys(cls) -> Generator[str, None, None]:
        """ Iterate over key names """

        for field in dataclasses.fields(cls):
            yield field.name

    def values(self) -> Generator[Any, None, None]:
        """ Iterate over key values """

        yield from self.to_dict().values()

    def items(self) -> Generator[Tuple[str, Any], None, None]:
        """ Iterate over key/value pairs """

        yield from self.to_dict().items()

    @classmethod
    def _default(cls, key: str, default: Any = None) -> Any:
        """
        Return a default value for a given key.

        Keys may have a default value, or a default *factory* has been specified.

        :param key: key to look for.
        :param default: when key has no default value, ``default`` is returned.
        :returns: a default value defined for the key, or its ``default_factory``'s
            return value of ``default_factory``, or ``default`` when key has no
            default value.
        """

        for field in dataclasses.fields(cls):
            if key != field.name:
                continue

            if not isinstance(field.default_factory, dataclasses._MISSING_TYPE):
                return field.default_factory()

            if not isinstance(field.default, dataclasses._MISSING_TYPE):
                return field.default

        else:
            return default

    @property
    def is_bare(self) -> bool:
        """
        Check whether all keys are either unset or have their default value.

        :returns: ``True`` if all keys either hold their default value
            or are not set at all, ``False`` otherwise.
        """

        for field in dataclasses.fields(self):
            value = getattr(self, field.name)

            if not isinstance(field.default_factory, dataclasses._MISSING_TYPE):
                if value != field.default_factory():
                    return False

            elif not isinstance(field.default, dataclasses._MISSING_TYPE):
                if value != field.default:
                    return False

            else:
                pass

        return True


SpecBasedContainerT = TypeVar('SpecBasedContainerT', bound='SpecBasedContainer')


class SpecBasedContainer(DataContainer):
    @classmethod
    def from_spec(cls: Type[SpecBasedContainerT], spec: Any) -> SpecBasedContainerT:
        """
        Convert from a specification file or from a CLI option

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.

        See :py:meth:`to_spec` for its counterpart.
        """

        raise NotImplementedError()

    def to_spec(self) -> Dict[str, Any]:
        """
        Convert to a form suitable for saving in a specification file

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.

        See :py:meth:`from_spec` for its counterpart.
        """

        return self.to_dict()

    def to_minimal_spec(self) -> Dict[str, Any]:
        """
        Convert to specification, skip default values

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.

        See :py:meth:`from_spec` for its counterpart.
        """

        return self.to_minimal_dict()


SerializableContainerDerivedType = TypeVar(
    'SerializableContainerDerivedType',
    bound='SerializableContainer')


SerializeCallback = Callable[[T], Any]
UnserializeCallback = Callable[[Any], T]


@dataclasses.dataclass
class SerializableContainer(DataContainer):
    """ A mixin class for saving and loading objects """

    @classmethod
    def default(cls, key: str, default: Any = None) -> Any:
        return cls._default(key, default=default)

    #
    # Moving data between containers and objects owning them
    #

    def inject_to(self, obj: Any) -> None:
        """
        Inject keys from this container into attributes of a given object
        """

        for name, value in self.items():
            setattr(obj, name, value)

    @classmethod
    def extract_from(cls: Type[SerializableContainerDerivedType],
                     obj: Any) -> SerializableContainerDerivedType:
        """ Extract keys from given object, and save them in a container """

        data = cls()

        for key in cls.keys():
            value = getattr(obj, key)
            if value is not None:
                setattr(data, key, value)

        return data

    #
    # Serialization - writing containers into YAML files, and restoring
    # them later.
    #

    def to_serialized(self) -> Dict[str, Any]:
        """
        Convert to a form suitable for saving in a file.

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.

        See :py:meth:`from_serialized` for its counterpart.
        """

        fields = self.to_dict()

        for name in fields.keys():
            field: dataclasses.Field[Any] = dataclass_field_by_name(self, name)
            serialize_callback = dataclass_field_metadata(field).serialize_callback

            if serialize_callback:
                fields[name] = serialize_callback(getattr(self, name))

        # Add a special field tracking what class we just shattered to pieces.
        fields.update({
            '__class__': {
                'module': self.__class__.__module__,
                'name': self.__class__.__name__
                }
            })

        return fields

    @classmethod
    def from_serialized(
            cls: Type[SerializableContainerDerivedType],
            serialized: Dict[str, Any]) -> SerializableContainerDerivedType:
        """
        Convert from a serialized form loaded from a file.

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.

        See :py:meth:`to_serialized` for its counterpart.
        """

        # Our special key may or may not be present, depending on who
        # calls this method.  In any case, it is not needed, because we
        # already know what class to restore: this one.
        serialized.pop('__class__', None)

        obj = cls(**serialized)

        for keyname, value in serialized.items():
            field: dataclasses.Field[Any] = dataclass_field_by_name(obj, keyname)
            unserialize_callback = dataclass_field_metadata(field).unserialize_callback

            if unserialize_callback:
                # Set attribute by adding it to __dict__ directly. Messing with setattr()
                # might cause re-use of mutable values by other instances.
                obj.__dict__[keyname] = unserialize_callback(value)

        return obj

    # ignore[misc,type-var]: mypy is correct here, method does return a
    # TypeVar, but there is no way to deduce the actual type, because
    # the method is static. That's on purpose, method tries to find the
    # class to unserialize, therefore it's simply unknown. Returning Any
    # would make mypy happy, but we do know the return value will be
    # derived from SerializableContainer. We can mention that, and
    # silence mypy about the missing actual type.
    @staticmethod
    def unserialize(
            serialized: Dict[str, Any]
            ) -> SerializableContainerDerivedType:  # type: ignore[misc,type-var]
        """
        Convert from a serialized form loaded from a file.

        Similar to :py:meth:`from_serialized`, but this method knows
        nothing about container's class, and will locate the correct
        module and class by inspecting serialized data. Discovered
        class' :py:meth:`from_serialized` is then used to create the
        container.

        Used to transform data read from a YAML file into original
        containers when their classes are not know to the code.
        Restoring such containers requires inspection of serialized data
        and dynamic imports of modules as needed.

        See https://tmt.readthedocs.io/en/stable/classes.html#class-conversions for more details.

        See :py:meth:`to_serialized` for its counterpart.
        """

        from tmt.plugins import import_member

        # Unpack class info, to get nicer variable names
        if "__class__" not in serialized:
            raise GeneralError(
                "Failed to load saved state, probably because of old data format.\n"
                "Use 'tmt clean runs' to clean up old runs.")

        klass_info = serialized.pop('__class__')
        klass = import_member(klass_info['module'], klass_info['name'])

        # Stay away from classes that are not derived from this one, to
        # honor promise given by return value annotation.
        assert issubclass(klass, SerializableContainer)

        # Apparently, the issubclass() check above is not good enough for mypy.
        return cast(SerializableContainerDerivedType, klass.from_serialized(serialized))


def markdown_to_html(filename: Path) -> str:
    """
    Convert markdown to html

    Expects: Markdown document as a file.
    Returns: An HTML document as a string.
    """
    try:
        import markdown
    except ImportError:
        raise ConvertError("Install tmt-test-convert to export tests.")

    try:
        with open(filename, 'r') as file:
            try:
                text = file.read()
            except UnicodeError:
                raise MetadataError(f"Unable to read '{filename}'.")
            return markdown.markdown(text)
    except IOError:
        raise ConvertError(f"Unable to open '{filename}'.")


def shell_variables(
        data: Union[List[str], Tuple[str, ...], Dict[str, Any]]) -> List[str]:
    """
    Prepare variables to be consumed by shell

    Convert dictionary or list/tuple of key=value pairs to list of
    key=value pairs where value is quoted with shlex.quote().
    """

    # Convert from list/tuple
    if isinstance(data, list) or isinstance(data, tuple):
        converted_data = []
        for item in data:
            splitted_item = item.split('=')
            key = splitted_item[0]
            value = shlex.quote('='.join(splitted_item[1:]))
            converted_data.append(f'{key}={value}')
        return converted_data

    # Convert from dictionary
    return [f"{key}={shlex.quote(str(value))}" for key, value in data.items()]


def duration_to_seconds(duration: str) -> int:
    """ Convert sleep time format into seconds """
    units = {
        's': 1,
        'm': 60,
        'h': 60 * 60,
        'd': 60 * 60 * 24,
        }
    try:
        match = re.match(r'^(\d+)([smhd]?)$', str(duration))
        if match is None:
            raise SpecificationError(f"Invalid duration '{duration}'.")
        number, suffix = match.groups()
        return int(number) * units.get(suffix, 1)
    except (ValueError, AttributeError):
        raise SpecificationError(f"Invalid duration '{duration}'.")


@overload
def verdict(
        decision: bool,
        comment: Optional[str] = None,
        good: str = 'pass',
        bad: str = 'fail',
        problem: str = 'warn',
        **kwargs: Any) -> bool:
    pass


@overload
def verdict(
        decision: None,
        comment: Optional[str] = None,
        good: str = 'pass',
        bad: str = 'fail',
        problem: str = 'warn',
        **kwargs: Any) -> None:
    pass


def verdict(
        decision: Optional[bool],
        comment: Optional[str] = None,
        good: str = 'pass',
        bad: str = 'fail',
        problem: str = 'warn',
        **kwargs: Any) -> Optional[bool]:
    """
    Print verdict in green, red or yellow based on the decision

    The supported decision values are:

        True .... good (green)
        False ... bad (red)
        None .... problem (yellow)

    Anything else raises an exception. Additional arguments
    are passed to the `echo` function. Returns back the decision.
    """

    if decision is False:
        text = style(bad, fg='red')
    elif decision is True:
        text = style(good, fg='green')
    elif decision is None:
        text = style(problem, fg='yellow')
    else:
        raise GeneralError(
            "Invalid decision value, must be 'True', 'False' or 'None'.")
    if comment:
        text = text + ' ' + comment
    echo(text, **kwargs)
    return decision


FormatWrap = Literal[True, False, 'auto']


def format(
        key: str,
        value: Union[None, bool, str, List[Any], Dict[Any, Any]] = None,
        indent: int = 24,
        width: int = 72,
        wrap: FormatWrap = 'auto',
        key_color: Optional[str] = 'green',
        value_color: Optional[str] = 'black') -> str:
    """
    Nicely format and indent a key-value pair

    The following values for 'wrap' are supported:

        True .... always reformat text and wrap long lines
        False ... preserve text, no new line changes
        auto .... wrap only if text contains a long line
    """
    indent_string = (indent + 1) * ' '
    # Key
    output = '{} '.format(str(key).rjust(indent, ' '))
    if key_color is not None:
        output = style(output, fg=key_color)
    # Bool
    if isinstance(value, bool):
        output += ('true' if value else 'false')
    # List
    elif isinstance(value, list):
        # Make sure everything is string, prepare list, check for spaces
        value = [str(item) for item in value]
        listed_text = fmf.utils.listed(value)
        has_spaces = any(item.find(' ') > -1 for item in value)
        # Use listed output only for short lists without spaces
        if len(listed_text) < width - indent and not has_spaces:
            output += listed_text
        # Otherwise just place each item on a new line
        else:
            output += ('\n' + indent_string).join(value)
    # Dictionary
    elif isinstance(value, dict):
        # Place each key value pair on a separate line
        output += ('\n' + indent_string).join(
            f'{item[0]}: {item[1]}' for item in value.items())
    # Text
    elif isinstance(value, str):
        # In 'auto' mode enable wrapping when long lines present
        if wrap == 'auto':
            wrap = any(len(line) + indent - 7 > width for line in value.split('\n'))
        if wrap:
            output += (wrap_text(
                value, width=width,
                preserve_paragraphs=True,
                initial_indent=indent_string,
                subsequent_indent=indent_string).lstrip())
        else:
            output += (('\n' + indent_string).join(
                value.rstrip().split('\n')))
    else:
        output += str(value)
    return output


def create_directory(
        path: Path,
        name: str,
        dry: bool = False,
        quiet: bool = False) -> None:
    """ Create a new directory, handle errors """
    say = log.debug if quiet else echo
    if path.is_dir():
        say("Directory '{}' already exists.".format(path))
        return
    if dry:
        say("Directory '{}' would be created.".format(path))
        return
    try:
        path.mkdir(exist_ok=True, parents=True)
        say("Directory '{}' created.".format(path))
    except OSError as error:
        raise FileError("Failed to create {} '{}' ({})".format(
            name, path, error)) from error


def create_file(
        path: Path,
        content: str,
        name: str,
        dry: bool = False,
        force: bool = False,
        mode: int = 0o664,
        quiet: bool = False) -> None:
    """ Create a new file, handle errors """
    say = log.debug if quiet else echo
    action = 'would be created' if dry else 'created'
    if path.exists():
        if force:
            action = 'would be overwritten' if dry else 'overwritten'
        else:
            raise FileError("File '{}' already exists.".format(path))

    if dry:
        say("{} '{}' {}.".format(name.capitalize(), path, action))
        return

    try:
        path.write_text(content)
        say("{} '{}' {}.".format(name.capitalize(), path, action))
        path.chmod(mode)
    except OSError as error:
        raise FileError("Failed to create {} '{}' ({})".format(
            name, path, error))


# Avoid multiple subprocess calls for the same url
@lru_cache(maxsize=None)
def check_git_url(url: str) -> str:
    """ Check that a remote git url is accessible """
    try:
        log.debug(f"Check git url '{url}'.")
        subprocess.check_call(
            ["git", "ls-remote", "--heads", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"GIT_ASKPASS": "echo", "GIT_TERMINAL_PROMPT": "0"})
        return url
    except subprocess.CalledProcessError:
        raise GitUrlError(f"Unable to contact remote git via '{url}'.")


def public_git_url(url: str) -> str:
    """
    Convert a git url into a public format

    Return url in the format which can be accessed without
    authentication. For now just cover the most common services.
    """

    # GitHub, GitLab
    # old: git@github.com:teemtee/tmt.git
    # new: https://github.com/teemtee/tmt.git
    matched = re.match('git@(.*):(.*)', url)
    if matched:
        host, project = matched.groups()
        return f'https://{host}/{project}'

    # RHEL packages
    # old: git+ssh://psplicha@pkgs.devel.redhat.com/tests/bash
    # old: ssh://psplicha@pkgs.devel.redhat.com/tests/bash
    # old: ssh://pkgs.devel.redhat.com/tests/bash
    # new: git://pkgs.devel.redhat.com/tests/bash
    matched = re.match(
        r'(git\+)?ssh://(\w+@)?(pkgs\.devel\.redhat\.com)/(.*)', url)
    if matched:
        _, _, host, project = matched.groups()
        return f'git://{host}/{project}'

    # Fedora packages, Pagure
    # old: git+ssh://psss@pkgs.fedoraproject.org/tests/shell
    # old: ssh://psss@pkgs.fedoraproject.org/tests/shell
    # new: https://pkgs.fedoraproject.org/tests/shell
    matched = re.match(r'(git\+)?ssh://(\w+@)?([^/]*)/(.*)', url)
    if matched:
        _, _, host, project = matched.groups()
        return f'https://{host}/{project}'

    # Otherwise return unmodified
    return url


def web_git_url(url: str, ref: str, path: Optional[Path] = None) -> str:
    """
    Convert a public git url into a clickable web url format

    Compose a clickable link from git url, ref and path to file
    for the most common git servers.
    """
    if path:
        path = Path(urllib.parse.quote_plus(str(path), safe="/"))

    # Special handling for pkgs.devel (ref at the end)
    if 'pkgs.devel' in url:
        url = url.replace('git://', 'https://').replace('.com', '.com/cgit')
        url += '/tree'
        if path:
            url += str(path)
        url += f'?h={ref}'
        return url

    # GitHub & GitLab
    if any(server in url for server in ['github', 'gitlab']):
        url = url.replace('.git', '').rstrip('/')
        url += f'/tree/{ref}'

    if path:
        url += str(path)

    return url


@lru_cache(maxsize=None)
def fmf_id(name: str, fmf_root: Path, always_get_ref: bool = False) -> 'tmt.base.FmfId':
    """ Return full fmf identifier of the node """

    def run(command: str) -> str:
        """ Run command, return output """
        cwd = fmf_root
        result = subprocess.run(
            command.split(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd)
        return result.stdout.strip().decode("utf-8")

    from tmt.base import FmfId

    fmf_id = FmfId(name=name)

    # Prepare url (for now handle just the most common schemas)
    branch = run("git rev-parse --abbrev-ref --symbolic-full-name @{u}")
    try:
        remote_name = branch[:branch.index('/')]
    except ValueError:
        remote_name = 'origin'
    remote = run(f"git config --get remote.{remote_name}.url")
    fmf_id.url = public_git_url(remote) if remote else None

    # Construct path (if different from git root)
    git_root = Path(run('git rev-parse --show-toplevel'))
    if git_root.resolve() != fmf_root.resolve():
        fmf_id.path = Path('/') / fmf_root.relative_to(git_root)

    # Get the ref (skip for the default)
    def_branch = default_branch(git_root)
    if def_branch is None:
        fmf_id.ref = None
    else:
        ref = run('git rev-parse --abbrev-ref HEAD')
        if ref != def_branch or always_get_ref:
            fmf_id.ref = ref
        else:
            # Note that it is a valid configuration without having a default
            # branch here. Consumers of returned fmf_id object should check
            # the fmf_id contains everything they need.
            fmf_id.ref = None

    return fmf_id


class TimeoutHTTPAdapter(requests.adapters.HTTPAdapter):
    """
    Spice up request's session with custom timeout.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.timeout = kwargs.pop('timeout', None)

        super().__init__(*args, **kwargs)

    # ignore[override]: signature does not match superclass on purpose.
    # send() does declare plenty of parameters we do not care about.
    def send(  # type: ignore[override]
            self,
            request: requests.PreparedRequest,
            **kwargs: Any) -> requests.Response:
        kwargs.setdefault('timeout', self.timeout)

        return super().send(request, **kwargs)


# ignore[misc]: the package *does* exist, and Retry class as well, it's
# somehow opaque to mypy.
class RetryStrategy(requests.packages.urllib3.util.retry.Retry):  # type: ignore[misc]
    def increment(
            self,
            *args: Any,
            **kwargs: Any
            ) -> requests.packages.urllib3.util.retry.Retry:
        error = cast(Optional[Exception], kwargs.get('error', None))

        # Detect a subset of exception we do not want to follow with a retry.
        if error is not None:
            # Failed certificate verification - this issue will probably not get any better
            # should we try again.
            if isinstance(error, urllib3.exceptions.SSLError) \
                    and 'certificate verify failed' in str(error):

                # [mpr] I'm not sure how stable this *iternal* API is, but pool seems to be the
                # only place aware of the remote hostname. Try our best to get the hostname for
                # a better error message, but don't crash because of a missing attribute or
                # something as dumb.

                connection_pool = kwargs.get('_pool', None)

                if connection_pool is not None and hasattr(connection_pool, 'host'):
                    message = f"Certificate verify failed for '{connection_pool.host}'."
                else:
                    message = 'Certificate verify failed.'

                raise GeneralError(message) from error

        return super().increment(*args, **kwargs)


# ignore[type-arg]: base class is a generic class, but we cannot list
# its parameter type, because in Python 3.6 the class "is not subscriptable".
class retry_session(contextlib.AbstractContextManager):  # type: ignore[type-arg]
    """
    Context manager for requests.Session() with retries and timeout
    """
    @staticmethod
    def create(
            retries: int = DEFAULT_RETRY_SESSION_RETRIES,
            backoff_factor: float = DEFAULT_RETRY_SESSION_BACKOFF_FACTOR,
            allowed_methods: Optional[Tuple[str, ...]] = None,
            status_forcelist: Optional[Tuple[int, ...]] = None,
            timeout: Optional[int] = None
            ) -> requests.Session:
        retry_strategy = RetryStrategy(
            total=retries,
            status_forcelist=status_forcelist,
            # `method_whitelist`` has been renamed to `allowed_methods` since
            # urllib3 1.26, and it will be removed in urllib3 2.0.
            # `allowed_methods` is therefore the future-proof name, but for the
            # sake of backward compatibility, internally we need to use the
            # deprecated parameter for now. Or request newer urllib3, but that
            # might a problem because of RPM availability.
            method_whitelist=allowed_methods,
            backoff_factor=backoff_factor)

        if timeout is not None:
            http_adapter: requests.adapters.HTTPAdapter = TimeoutHTTPAdapter(
                timeout=timeout, max_retries=retry_strategy)
        else:
            http_adapter = requests.adapters.HTTPAdapter(
                max_retries=retry_strategy)

        session = requests.Session()
        session.mount('http://', http_adapter)
        session.mount('https://', http_adapter)

        return session

    def __init__(
            self,
            retries: int = DEFAULT_RETRY_SESSION_RETRIES,
            backoff_factor: float = DEFAULT_RETRY_SESSION_BACKOFF_FACTOR,
            allowed_methods: Optional[Tuple[str, ...]] = None,
            status_forcelist: Optional[Tuple[int, ...]] = None,
            timeout: Optional[int] = None
            ) -> None:
        self.retries = retries
        self.backoff_factor = backoff_factor
        self.allowed_methods = allowed_methods
        self.status_forcelist = status_forcelist
        self.timeout = timeout

    def __enter__(self) -> requests.Session:
        return self.create(
            retries=self.retries,
            backoff_factor=self.backoff_factor,
            allowed_methods=self.allowed_methods,
            status_forcelist=self.status_forcelist,
            timeout=self.timeout)

    def __exit__(self, *args: Any) -> None:
        pass


def remove_color(text: str) -> str:
    """ Remove ansi color sequences from the string """
    return re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)


def default_branch(repository: Path, remote: str = 'origin') -> Optional[str]:
    """ Detect default branch from given local git repository """
    # Make sure the '.git/refs/remotes/{remote}' directory is present
    git_remotes_dir = repository / f'.git/refs/remotes/{remote}'
    if not git_remotes_dir.exists():
        return None

    head = repository / f'.git/refs/remotes/{remote}/HEAD'
    # Make sure the HEAD reference is available
    if not head.exists():
        subprocess.run(
            f'git remote set-head {remote} --auto'.split(), cwd=repository)
    # The ref format is 'ref: refs/remotes/origin/main'
    with open(head) as ref:
        return ref.read().strip().split('/')[-1]


def parse_dotenv(content: str) -> EnvironmentType:
    """ Parse dotenv (shell) format of variables """
    return dict([line.split("=", maxsplit=1)
                for line in shlex.split(content, comments=True)])


def parse_yaml(content: str) -> EnvironmentType:
    """ Parse variables from yaml, ensure flat dictionary format """
    yaml_as_dict = YAML(typ="safe").load(content)
    # Handle empty file as an empty environment
    if yaml_as_dict is None:
        return dict()
    if any(isinstance(val, dict) for val in yaml_as_dict.values()):
        raise GeneralError(
            "Can't set the environment from the nested yaml config. The "
            "config should be just key, value pairs.")
    return {key: str(value) for key, value in yaml_as_dict.items()}


def validate_git_status(test: 'tmt.base.Test') -> Tuple[bool, str]:
    """
    Validate that test has current metadata on fmf_id

    Return a tuple (boolean, message) as the result of validation.

    Checks that sources:
    - all local changes are committed
    - up to date on remote repository
    - .fmf/version marking fmf root is committed as well

    When all checks pass returns (True, '').
    """
    sources = test.node.sources + \
        [os.path.join(test.node.root, '.fmf', 'version')]

    # Use tmt's run instead of subprocess.run
    run = Common(logger=test._logger).run

    # Check for not committed metadata changes
    cmd = Command(
        'git',
        'status', '--porcelain',
        '--',
        *sources
        )
    try:
        result = run(cmd, cwd=Path(test.node.root), join=True)
    except RunError as error:
        return (
            False,
            f"Failed to run git status: {error.stdout}"
            )

    not_committed = []
    assert result.stdout is not None
    for line in result.stdout.split('\n'):
        if line:
            # XY PATH or XY ORIG -> PATH. XY and PATH are separated by space
            not_committed.append(line[3:])

    if not_committed:
        return (False, "Uncommitted changes in " + " ".join(not_committed))

    # Check for not pushed changes
    cmd = Command("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    try:
        result = run(cmd, cwd=Path(test.node.root))
    except RunError as error:
        return (
            False,
            f'Failed to get remote branch, error raised: "{error.stderr}"'
            )

    assert result.stdout is not None
    remote_ref = result.stdout.strip()

    cmd = Command(
        'git',
        'diff',
        f'HEAD..{remote_ref}',
        '--name-status',
        '--',
        *sources
        )
    try:
        result = run(cmd, cwd=Path(test.node.root))
    except RunError as error:
        return (
            False,
            f'Failed to diff against remote branch, error raised: "{error.stderr}"')

    not_pushed = []
    assert result.stdout is not None
    for line in result.stdout.split('\n'):
        if line:
            _, path = line.strip().split('\t', maxsplit=2)
            not_pushed.append(path)
    if not_pushed:
        return (False, "Not pushed changes in " + " ".join(not_pushed))

    return (True, '')


def generate_runs(
        path: Path,
        id_: Optional[str] = None) -> Generator[Path, None, None]:
    """ Generate absolute paths to runs from path """
    # Prepare absolute workdir path if --id was used
    if id_:
        run_path = Path(id_)
        if '/' not in id_:
            run_path = path / run_path
        if run_path.is_absolute():
            if run_path.exists():
                yield run_path
            return
    if not path.exists():
        return
    for childpath in path.iterdir():
        abs_child_path = childpath.absolute()
        # If id_ is None, the abs_path is considered valid (no filtering
        # is being applied). If it is defined, it has been transformed
        # to absolute path and must be equal to abs_path for the run
        # in abs_path to be generated.
        invalid_id = id_ and str(abs_child_path) != id_
        invalid_run = not abs_child_path.joinpath('run.yaml').exists()
        if not abs_child_path.is_dir() or invalid_id or invalid_run:
            continue
        yield abs_child_path


def load_run(run: 'tmt.base.Run') -> Tuple[bool, Optional[Exception]]:
    """ Load a run and its steps from the workdir """
    try:
        run.load_from_workdir()
    except GeneralError as error:
        return False, error
    for plan in run.plans:
        for step in plan.steps(disabled=True):
            step.load()
    return True, None


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  StructuredField
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

SFSectionValueType = Union[str, List[str]]


class StructuredField:
    """
    Handling multiple text data in a single text field

    The StructuredField allows you to easily store and extract several
    sections of text data to/from a single text field. The sections are
    separated by section names in square brackets and can be hosted in
    other text as well.

    The section names have to be provided on a separate line and there
    must be no leading/trailing white space before/after the brackets.
    The StructuredField supports two versions of the format:

    Version 0: Simple, concise, useful when neither the surrounding text
    or the section data can contain lines which could resemble section
    names. Here's an example of a simple StructuredField::

        Note written by human.

        [section-one]
        Section one content.

        [section-two]
        Section two content.

        [section-three]
        Section three content.

        [end]

        Another note written by human.

    Version 1: Includes unique header to prevent collisions with the
    surrounding text and escapes any section-like lines in the content::

        Note written by human.

        [structured-field-start]
        This is StructuredField version 1. Please, edit with care.

        [section-one]
        Section one content.

        [section-two]
        Section two content.
        [structured-field-escape][something-resembling-section-name]

        [section-three]
        Section three content.

        [structured-field-end]

        Another note written by human.

    Note that an additional empty line is added at the end of each
    section to improve the readability. This line is not considered
    to be part of the section content.

    Besides handling the whole section content it's also possible to
    store several key-value pairs in a single section, similarly as in
    the ini config format::

        [section]
        key1 = value1
        key2 = value2
        key3 = value3

    Provide the key name as the optional argument 'item' when accessing
    these single-line items. Note that the section cannot contain both
    plain text data and key-value pairs.

    Example::

        field = qe.StructuredField()
        field.set("project", "Project Name")
        field.set("details", "somebody", "owner")
        field.set("details", "2013-05-27", "started")
        field.set("description", "This is a description.\\n"
                "It spans across multiple lines.\\n")
        print field.save()

            [structured-field-start]
            This is StructuredField version 1. Please, edit with care.

            [project]
            Project Name

            [details]
            owner = somebody
            started = 2013-05-27

            [description]
            This is a description.
            It spans across multiple lines.

            [structured-field-end]

        field.version(0)
        print field.save()

            [project]
            Project Name

            [details]
            owner = somebody
            started = 2013-05-27

            [description]
            This is a description.
            It spans across multiple lines.

            [end]

    Multiple values for the same key are supported as well. Enable this
    feature with 'multi=True' when initializing the structured field.
    If multiple values are present their list will be returned instead
    of a single string. Similarly use list for setting multiple values::

        field = qe.StructuredField(multi=True)
        requirements = ['hypervisor=', 'labcontroller=lab.example.com']
        field.set("hardware", requirements, "hostrequire")
        print field.save()

            [structured-field-start]
            This is StructuredField version 1. Please, edit with care.

            [hardware]
            hostrequire = hypervisor=
            hostrequire = labcontroller=lab.example.com

            [structured-field-end]

        print field.get("hardware", "hostrequire")

            ['hypervisor=', 'labcontroller=lab.example.com']
    """

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  StructuredField Special
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def __init__(
            self,
            text: Optional[str] = None,
            version: int = 1,
            multi: bool = False) -> None:
        """ Initialize the structured field """
        self.version(version)
        self._header: str = ""
        self._footer: str = ""
        # Sections are internally stored in their serialized form, i.e. as
        # strings.
        self._sections: Dict[str, str] = {}
        self._order: List[str] = []
        self._multi = multi
        if text is not None:
            self.load(text)

    def __iter__(self) -> Generator[str, None, None]:
        """ By default iterate through all available section names """
        for section in self._order:
            yield section

    def __nonzero__(self) -> bool:
        """ True when any section is defined """
        return len(self._order) > 0

    __bool__ = __nonzero__

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  StructuredField Private
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def _load_version_zero(self, text: str) -> None:
        """ Load version 0 format """
        # Attempt to split the text according to the section tag
        section = re.compile(r"\n?^\[([^\]]+)\]\n", re.MULTILINE)
        parts = section.split(text)
        # If just one part ---> no sections present, just plain text
        if len(parts) == 1:
            self._header = parts[0]
            return
        # Pick header & footer, make sure [end] tag is present
        self._header = parts[0]
        self._footer = re.sub("^\n", "", parts[-1])
        if parts[-2] != "end":
            raise StructuredFieldError("No [end] section tag found")
        # Convert to dictionary and save the order
        keys = parts[1:-2:2]
        values = parts[2:-2:2]
        for key, value in zip(keys, values):
            self.set(key, value)

    def _load(self, text: str) -> None:
        """ Load version 1+ format """
        # The text must exactly match the format
        format = re.compile(
            r"(.*)^\[structured-field-start\][ \t]*\n"
            r"(.*)\n\[structured-field-end\][ \t]*\n(.*)",
            re.DOTALL + re.MULTILINE)
        # No match ---> plain text or broken structured field
        matched = format.search(text)
        if not matched:
            if "[structured-field" in text:
                raise StructuredFieldError("StructuredField parse error")
            self._header = text
            log.debug("StructuredField not found, treating as a plain text")
            return
        # Save header & footer (remove trailing new lines)
        self._header = re.sub("\n\n$", "\n", matched.groups()[0])
        if self._header:
            log.debug(u"Parsed header:\n{0}".format(self._header))
        self._footer = re.sub("^\n", "", matched.groups()[2])
        if self._footer:
            log.debug(u"Parsed footer:\n{0}".format(self._footer))
        # Split the content on the section names
        section = re.compile(r"\n\[([^\]]+)\][ \t]*\n", re.MULTILINE)
        parts = section.split(matched.groups()[1])
        # Detect the version
        version_match = re.search(r"version (\d+)", parts[0])
        if not version_match:
            log.error(parts[0])
            raise StructuredFieldError(
                "Unable to detect StructuredField version")
        self.version(int(version_match.groups()[0]))
        log.debug(
            "Detected StructuredField version {0}".format(
                self.version()))
        # Convert to dictionary, remove escapes and save the order
        keys = parts[1::2]
        escape = re.compile(r"^\[structured-field-escape\]", re.MULTILINE)
        values = [escape.sub("", value) for value in parts[2::2]]
        for key, value in zip(keys, values):
            self.set(key, value)
        log.debug(u"Parsed sections:\n{0}".format(
            pprint.pformat(self._sections)))

    def _save_version_zero(self) -> str:
        """ Save version 0 format """
        result = []
        if self._header:
            result.append(self._header)
        for section, content in self.iterate():
            result.append(u"[{0}]\n{1}".format(section, content))
        if self:
            result.append(u"[end]\n")
        if self._footer:
            result.append(self._footer)
        return "\n".join(result)

    def _save(self) -> str:
        """ Save version 1+ format """
        result = []
        # Regular expression for escaping section-like lines
        escape = re.compile(r"^(\[.+\])$", re.MULTILINE)
        # Header
        if self._header:
            result.append(self._header)
        # Sections
        if self:
            result.append(
                u"[structured-field-start]\n"
                u"This is StructuredField version {0}. "
                u"Please, edit with care.\n".format(self._version))
            for section, content in self.iterate():
                result.append(u"[{0}]\n{1}".format(section, escape.sub(
                    "[structured-field-escape]\\1", content)))
            result.append(u"[structured-field-end]\n")
        # Footer
        if self._footer:
            result.append(self._footer)
        return "\n".join(result)

    def _read_section(self, content: str) -> Dict[str, SFSectionValueType]:
        """ Parse config section and return ordered dictionary """
        dictionary: Dict[str, SFSectionValueType] = OrderedDict()
        for line in content.split("\n"):
            # Remove comments and skip empty lines
            line = re.sub("#.*", "", line)
            if re.match(r"^\s*$", line):
                continue
            # Parse key and value
            matched = re.search("([^=]+)=(.*)", line)
            if not matched:
                raise StructuredFieldError(
                    "Invalid key/value line: {0}".format(line))
            key = matched.groups()[0].strip()
            value = matched.groups()[1].strip()
            # Handle multiple values if enabled
            if key in dictionary and self._multi:
                stored_value = dictionary[key]
                if isinstance(stored_value, list):
                    stored_value.append(value)
                else:
                    dictionary[key] = [stored_value, value]
            else:
                dictionary[key] = value
        return dictionary

    def _write_section(self, dictionary: Dict[str, SFSectionValueType]) -> str:
        """ Convert dictionary into a config section format """
        section = ""
        for key in dictionary:
            if isinstance(dictionary[key], list):
                for value in dictionary[key]:
                    section += "{0} = {1}\n".format(key, value)
            else:
                section += "{0} = {1}\n".format(key, dictionary[key])
        return section

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  StructuredField Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def iterate(self) -> Generator[Tuple[str, str], None, None]:
        """ Return (section, content) tuples for all sections """
        for section in self:
            yield section, self._sections[section]

    def version(self, version: Optional[int] = None) -> int:
        """ Get or set the StructuredField version """
        if version is not None:
            if version in [0, 1]:
                self._version = version
            else:
                raise StructuredFieldError(
                    "Bad StructuredField version: {0}".format(version))
        return self._version

    def load(self, text: str, version: Optional[int] = None) -> None:
        """ Load the StructuredField from a string """
        if version is not None:
            self.version(version)
        # Make sure we got a text, convert from bytes if necessary
        if isinstance(text, bytes):
            text = text.decode("utf8")
        if not isinstance(text, str):
            raise StructuredFieldError(
                "Invalid StructuredField, expecting string")
        # Remove possible carriage returns
        text = re.sub("\r\n", "\n", text)
        # Make sure the text has a new line at the end
        if text and text[-1] != "\n":
            text += "\n"
        log.debug(u"Parsing StructuredField\n{0}".format(text))
        # Parse respective format version
        if self._version == 0:
            self._load_version_zero(text)
        else:
            self._load(text)

    def save(self) -> str:
        """ Convert the StructuredField into a string """
        if self.version() == 0:
            return self._save_version_zero()
        else:
            return self._save()

    def header(self, content: Optional[str] = None) -> str:
        """ Get or set the header content """
        if content is not None:
            self._header = content
        return self._header

    def footer(self, content: Optional[str] = None) -> str:
        """ Get or set the footer content """
        if content is not None:
            self._footer = content
        return self._footer

    def sections(self) -> List[str]:
        """ Get the list of available sections """
        return self._order

    def get(
            self,
            section: str,
            item: Optional[str] = None) -> SFSectionValueType:
        """ Return content of given section or section item """
        try:
            content = self._sections[section]
        except KeyError:
            raise StructuredFieldError(
                "Section [{0!r}] not found".format(ascii(section)))
        # Return the whole section content
        if item is None:
            return content
        # Return only selected item from the section
        try:
            return self._read_section(content)[item]
        except KeyError:
            raise StructuredFieldError(
                "Unable to read '{0!r}' from section '{1!r}'".format(
                    ascii(item), ascii(section)))

    def set(self, section: str, content: Any,
            item: Optional[str] = None) -> None:
        """ Update content of given section or section item """
        # Convert to string if necessary, keep lists untouched
        if isinstance(content, list):
            pass
        elif isinstance(content, bytes):
            content = content.decode("utf8")
        elif not isinstance(content, str):
            content = str(content)
        # Set the whole section content
        if item is None:
            # Add new line if missing
            if content and content[-1] != "\n":
                content += "\n"
            self._sections[section] = content
        # Set only selected item from the section
        else:
            try:
                current = self._sections[section]
            except KeyError:
                current = ""
            dictionary = self._read_section(current)
            dictionary[item] = content
            self._sections[section] = self._write_section(dictionary)
        # Remember the order when adding a new section
        if section not in self._order:
            self._order.append(section)

    def remove(self, section: str, item: Optional[str] = None) -> None:
        """ Remove given section or section item """
        # Remove the whole section
        if item is None:
            try:
                del self._sections[section]
                del self._order[self._order.index(section)]
            except KeyError:
                raise StructuredFieldError(
                    "Section [{0!r}] not found".format(ascii(section)))
        # Remove only selected item from the section
        else:
            try:
                dictionary = self._read_section(self._sections[section])
                del (dictionary[item])
            except KeyError:
                raise StructuredFieldError(
                    "Unable to remove '{0!r}' from section '{1!r}'".format(
                        ascii(item), ascii(section)))
            self._sections[section] = self._write_section(dictionary)


class DistGitHandler:
    """ Common functionality for DistGit handlers """
    sources_file_name = 'sources'
    uri = "/rpms/{name}/{filename}/{hashtype}/{hash}/{filename}"

    usage_name: str  # Name to use for dist-git-type
    re_source: Pattern[str]
    re_ignore_extensions: Pattern[str] = re.compile(r'\.(sign|asc|key)$')
    lookaside_server: str
    remote_substring: Pattern[str]

    def url_and_name(self, cwd: Optional[Path] = None) -> List[Tuple[str, str]]:
        """
        Return list of urls and basenames of the used source

        The 'cwd' parameter has to be a DistGit directory.
        """
        cwd = cwd or Path.cwd()
        # Assumes <package>.spec
        globbed = list(cwd.glob('*.spec'))
        if len(globbed) != 1:
            raise GeneralError(f"No .spec file is present in '{cwd}'.")
        package = globbed[0].stem
        ret_values = []
        try:
            with open(cwd / self.sources_file_name) as f:
                for line in f.readlines():
                    match = self.re_source.match(line)
                    if match is None:
                        raise GeneralError(
                            f"Couldn't match '{self.sources_file_name}' "
                            f"content with '{self.re_source.pattern}'.")
                    used_hash, source_name, hash_value = match.groups()
                    ret_values.append((self.lookaside_server + self.uri.format(
                        name=package,
                        filename=source_name,
                        hash=hash_value,
                        hashtype=used_hash.lower()
                        ), source_name))
        except Exception as error:
            raise GeneralError(f"Couldn't read '{self.sources_file_name}' file.") from error
        if not ret_values:
            raise GeneralError(
                "No sources found in '{self.sources_file_name}' file.")
        return ret_values

    def its_me(self, remotes: List[str]) -> bool:
        """ True if self can work with remotes """
        return any(self.remote_substring.search(item) for item in remotes)


class FedoraDistGit(DistGitHandler):
    """ Fedora Handler """
    usage_name = "fedora"
    re_source = re.compile(r"^(\w+) \(([^)]+)\) = ([0-9a-fA-F]+)$")
    lookaside_server = "https://src.fedoraproject.org/repo/pkgs"
    remote_substring = re.compile(r'fedoraproject\.org')


class CentOSDistGit(DistGitHandler):
    """ CentOS Handler """
    usage_name = "centos"
    re_source = re.compile(r"^(\w+) \(([^)]+)\) = ([0-9a-fA-F]+)$")
    lookaside_server = "https://sources.stream.centos.org/sources"
    remote_substring = re.compile(r'redhat/centos')


class RedHatGitlab(DistGitHandler):
    """ Red Hat on Gitlab """
    usage_name = "redhatgitlab"
    re_source = re.compile(r"^(\w+) \(([^)]+)\) = ([0-9a-fA-F]+)$")
    # Location already public (standard-test-roles)
    lookaside_server = "http://pkgs.devel.redhat.com/repo"
    remote_substring = re.compile(r'redhat/rhel/')


def get_distgit_handler(
        remotes: Optional[List[str]] = None,
        usage_name: Optional[str] = None) -> DistGitHandler:
    """
    Return the right DistGitHandler

    Pick the DistGitHandler class which understands specified
    remotes or by usage_name.
    """
    for candidate_class in DistGitHandler.__subclasses__():
        if usage_name is not None and usage_name == candidate_class.usage_name:
            return candidate_class()
        if remotes is not None:
            ret_val = candidate_class()
            if ret_val.its_me(remotes):
                return ret_val
    raise GeneralError(f"No known remote in '{remotes}'.")


def get_distgit_handler_names() -> List[str]:
    """ All known distgit handlers """
    return [i.usage_name for i in DistGitHandler.__subclasses__()]


def git_clone(
        url: str,
        destination: Path,
        common: Common,
        env: Optional[EnvironmentType] = None,
        shallow: bool = False
        ) -> CommandOutput:
    """
    Git clone url to destination, retry without shallow if necessary

    For shallow=True attempt to clone repository using --depth=1 option first.
    If not successful attempt to clone whole repo.

    Common instance is used to run the command for appropriate logging.
    Environment is updated by 'env' dictionary.
    """
    depth = ['--depth=1'] if shallow else []
    try:
        return common.run(
            Command(
                'git', 'clone',
                *depth,
                url, str(destination)
                ), env=env)
    except RunError:
        if not shallow:
            # Do not retry if shallow was not used
            raise
        # Git server might not support shallow cloning, try again
        return git_clone(url, destination, common, env, shallow=False)


# ignore[type-arg]: base class is a generic class, but we cannot list its parameter type, because
# in Python 3.6 the class "is not subscriptable".
class updatable_message(contextlib.AbstractContextManager):  # type: ignore[type-arg]
    """ Updatable message suitable for progress-bar-like reporting """

    def __init__(
            self,
            key: str,
            enabled: bool = True,
            indent_level: int = 0,
            key_color: Optional[str] = None,
            default_value_color: Optional[str] = None
            ) -> None:
        """
        Updatable message suitable for progress-bar-like reporting.

        .. code:block:: python3

           with updatable_message('foo') as message:
               while ...:
                   ...

                   # check state of remote request, and update message
                   state = remote_api.check()
                   message.update(state)

        :param key: a string to use as the left-hand part of logged message.
        :param enabled: if unset, no output would be performed.
        :param indent_level: desired indentation level.
        :param key_color: optional color to apply to ``key``.
        :param default_color: optional color to apply to value when
            :py:meth:`update` is called with ``color`` left out.
        """

        self.key = key
        self.enabled = enabled
        self.indent_level = indent_level
        self.key_color = key_color
        self.default_value_color = default_value_color

        # No progress if terminal not attached
        if not sys.stdout.isatty():
            self.enabled = False

        self._previous_line: Optional[str] = None

    def __enter__(self) -> 'updatable_message':
        return self

    def __exit__(self, *args: Any) -> None:
        sys.stdout.write('\n')
        sys.stdout.flush()

    def update(self, value: str, color: Optional[str] = None) -> None:
        if not self.enabled:
            return

        if self._previous_line is not None:
            message = value.ljust(len(self._previous_line))

        else:
            message = value

        self._previous_line = value

        message = tmt.log.indent(
            self.key,
            value=style(
                message,
                fg=color or self.default_value_color),
            color=self.key_color,
            level=self.indent_level)

        sys.stdout.write(f"\r{message}")
        sys.stdout.flush()


def find_fmf_root(path: Path) -> List[Path]:
    """
    Search trough path and return all fmf roots that exist there

    Returned list is ordered by path length, shortest one first.

    Raise `MetadataError` if no fmf root is found.
    """
    fmf_roots = []
    for _root, _, files in os.walk(path):
        root = Path(_root)
        if root.name != '.fmf':
            continue
        if 'version' in files:
            fmf_roots.append(root.parent)
    if not fmf_roots:
        raise MetadataError(f"No fmf root present inside '{path}'.")
    fmf_roots.sort(key=lambda path: len(str(path)))
    return fmf_roots


#
# JSON schema-based validation helpers
#
# Aims at FMF data consumed by tmt, but can be used for any structure.
#

# `Schema` represents a loaded JSON schema structure. It may be fairly complex,
# but it's not needed to provide the exhaustive and fully detailed type since
# tmt code is not actually "reading" it. Loaded schema is passed down to
# jsonschema library, and while `Any` would be perfectly valid, let's use an
# alias to make schema easier to track in our code.
Schema = Dict[str, Any]
SchemaStore = Dict[str, Schema]


def _patch_plan_schema(schema: Schema, store: SchemaStore) -> None:
    """
    Resolve references to per-plugin schema known to steps. All schemas have
    been loaded into store, all that's left is to update each step in plan
    schema with the list of schemas allowed for that particular step.

    For each step, we create the following schema (see also plan.yaml for the
    rest of plan schema):

    .. code-block:: yaml

       <step name>:
         oneOf:
           - $ref: "/schemas/<step name>/plugin1"
           - $ref: "/schemas/<step name>/plugin2"
           ...
           - $ref: "/schemas/<step name>/pluginN"
           - type: array
             items:
               anyOf:
                 - $ref: "/schemas/<step name>/plugin1"
                 - $ref: "/schemas/<step name>/plugin2"
                 ...
                 - $ref: "/schemas/<step name>/pluginN"
    """

    for step in ('discover', 'execute', 'finish', 'prepare', 'provision', 'report'):
        step_schema_prefix = f'/schemas/{step}/'

        step_plugin_schema_ids = [schema_id for schema_id in store.keys() if schema_id.startswith(
            step_schema_prefix) and schema_id not in PLAN_SCHEMA_IGNORED_IDS]

        refs: List[Schema] = [
            {'$ref': schema_id} for schema_id in step_plugin_schema_ids
            ]

        schema['properties'][step] = {
            'oneOf': refs + [
                {
                    'type': 'array',
                    'items': {
                        'anyOf': refs
                        }
                    }
                ]
            }


def _load_schema(schema_filepath: Path) -> Schema:
    """
    Load a JSON schema from a given filepath.

    A helper returning the raw loaded schema.
    """

    if not schema_filepath.is_absolute():
        schema_filepath = Path(pkg_resources.resource_filename('tmt', 'schemas')) \
            / schema_filepath

    try:
        with open(schema_filepath, 'r', encoding='utf-8') as f:
            return cast(Schema, yaml_to_dict(f.read()))

    except Exception as exc:
        raise FileError(f"Failed to load schema file {schema_filepath}\n{exc}")


@functools.lru_cache(maxsize=None)
def load_schema(schema_filepath: Path) -> Schema:
    """
    Load a JSON schema from a given filepath.

    Recommended for general use, the method may apply some post-loading touches
    to the given schema, and unless caller is interested in the raw content of
    the file, this functions should be used instead of the real workhorse of
    schema loading, :py:func:`_load_schema`.
    """

    schema = _load_schema(schema_filepath)

    if schema.get('$id') == '/schemas/plan':
        _patch_plan_schema(schema, load_schema_store())

    return schema


@functools.lru_cache(maxsize=None)
def load_schema_store() -> SchemaStore:
    """
    Load all available JSON schemas, and put them into a "store".

    Schema store is a simple mapping between schema IDs and schemas.
    """

    store: SchemaStore = {}

    schema_dirpath = Path(pkg_resources.resource_filename('tmt', 'schemas'))

    try:
        for dirpath, _, filenames in os.walk(
                schema_dirpath, followlinks=True):
            for filename in filenames:
                filepath = Path(dirpath) / filename

                # Ignore all files but YAML files.
                if filepath.suffix.lower() not in ('.yaml', '.yml'):
                    continue

                schema = _load_schema(filepath)

                store[schema['$id']] = schema

    except Exception as exc:
        raise FileError(f"Failed to discover schema files\n{exc}")

    if '/schemas/plan' not in store:
        raise FileError('Failed to discover schema for plans')

    _patch_plan_schema(store['/schemas/plan'], store)

    return store


def _prenormalize_fmf_node(node: fmf.Tree, schema_name: str) -> fmf.Tree:
    """
    Apply the minimal possible normalization steps to nodes before validating them with schemas.

    tmt allows some fields to have default values, and at least ``how`` field is necessary for
    schema-based validation to work reliably. Based on ``how`` field, plan schema identifies
    the correct *plugin* schema for step validation. Without ``how``, it's hard to pick the
    correct schema.

    This function tries to do minimal number of changes to a given fmf node to honor the promise
    of ``how`` being optional, with known defaults for each step. It might be possible to resolve
    this purely with schemas, but since we don't know how (yet?), a Python implementation has been
    chosen to unblock schema-based validation while keeping things easier for users. This may
    change in the future, dropping the need for this pre-validation step.

    .. note::

       This function is not part of the normalization process that happens after validation. The
       purpose of this function is to make the world nice and shiny for tmt users while avoiding
       the possibility of schema becoming way too complicated, especially when we would need
       non-trivial amount fo time for experiments.

       The real normalization process takes place after validation, and is responsible for
       converting raw fmf data to data types and structures more suited for tmt internal
       implementation.
    """

    # As of now, only `how` field in plan steps seems to be required for schema-based validation
    # to work correctly, therefore ignore any other node.
    if schema_name != 'plan.yaml':
        return node

    # Perform the very crude and careful semi-validation. We need to set the `how` key to a default
    # value - but it's not our job to validate the general structure of node data. Walk the "happy"
    # path, touch the node only when it matches the specification of being a mapping of steps and
    # these being either mappings or lists of mappings. Whenever we notice some value does not
    # match this basic structure, ignore the step completely - its issues will be cought by schema
    # later, don't waste time on steps that do not follow specification.

    # Fmf data describing a plan shall be a mapping (with keys like `discover` or `adjust`).
    if not isinstance(node.data, dict):
        return node

    # Avoid possible circular imports
    import tmt.steps

    def _process_step(step_name: str, step: Dict[Any, Any]) -> None:
        """
        Process a single step configuration.
        """

        # If `how` is set, don't touch it, and there's nothing to do.
        if 'how' in step:
            return

        # Magic!
        # No, seriously: step is implemented in `tmt.steps.$step_name` package,
        # by a class `tmt.steps.$step_name.$step_name_with_capitalized_first_letter`.
        # Instead of having a set of if-elif tests, we can reach the default `how`
        # dynamically.

        from tmt.plugins import import_member

        step_module_name = f'tmt.steps.{step_name}'
        step_class_name = step_name.capitalize()

        step_class = import_member(step_module_name, step_class_name)

        if not issubclass(step_class, tmt.steps.Step):
            raise GeneralError(
                'Possible step {step_name} implementation '
                f'{step_module_name}.{step_class_name} is not a subclass '
                'of tmt.steps.Step class.')

        step['how'] = step_class.DEFAULT_HOW

    def _process_step_collection(step_name: str, step_collection: Any) -> None:
        """
        Process a collection of step configurations.
        """

        # Ignore anything that is not a step.
        if step_name not in tmt.steps.STEPS:
            return

        # A single step configuration, represented as a mapping.
        if isinstance(step_collection, dict):
            _process_step(step_name, step_collection)

            return

        # Multiple step configurations, as mappings in a list
        if isinstance(step_collection, list):
            for step_config in step_collection:
                # Unexpected, maybe instead of a mapping describing a step someone put
                # in an integer... Ignore, schema will report it.
                if not isinstance(step_config, dict):
                    continue

                _process_step(step_name, step_config)

    for step_name, step_config in node.data.items():
        _process_step_collection(step_name, step_config)

    return node


def validate_fmf_node(
        node: fmf.Tree, schema_name: str) -> List[Tuple[jsonschema.ValidationError, str]]:
    """ Validate a given fmf node """

    node = _prenormalize_fmf_node(node, schema_name)

    result = node.validate(load_schema(Path(schema_name)), schema_store=load_schema_store())

    if result.result is True:
        return []

    # A bit of error formatting. It is possible to use str(error), but the result
    # is a bit too JSON-ish. Let's present an error message in a way that helps
    # users to point finger on each and every issue. But don't throw the original
    # errors away!

    errors: List[Tuple[jsonschema.ValidationError, str]] = []

    for error in result.errors:
        path = f'{node.name}:{".".join(error.path)}'

        errors.append((error, f'{path} - {error.message}'))

    return errors


# A type for callbacks given to wait()
WaitCheckType = Callable[[], T]


def wait(
        parent: Common,
        check: WaitCheckType[T],
        timeout: datetime.timedelta,
        tick: float = DEFAULT_WAIT_TICK,
        tick_increase: float = DEFAULT_WAIT_TICK_INCREASE
        ) -> T:
    """
    Wait for a condition to become true.

    To test the condition state, a ``check`` callback is called every ``tick``
    seconds until ``check`` reports a success. The callback may:

    * decide the condition has been fulfilled. This is a successfull outcome,
      ``check`` shall then simply return, and waiting ends. Or,
    * decide more time is needed. This is not a successfull outcome, ``check``
      shall then raise :py:clas:`WaitingIncomplete` exception, and ``wait()``
      will try again later.

    :param parent: "owner" of the wait process. Used for its logging capability.
    :param check: a callable responsible for testing the condition. Accepts no
        arguments. To indicate more time and attempts are needed, the callable
        shall raise :py:class:`WaitingIncomplete`, otherwise it shall return
        without exception. Its return value will be propagated by ``wait()`` up
        to ``wait()``'s. All other exceptions raised by ``check`` will propagate
        to ``wait()``'s caller as well, terminating the wait.
    :param timeout: amount of time ``wait()`` is alowed to spend waiting for
        successfull outcome of ``check`` call.
    :param tick: how many seconds to wait between two consecutive calls of
        ``check``.
    :param tick_increase: a multiplier applied to ``tick`` after every attempt.
    :returns: value returned by ``check`` reporting success.
    :raises GeneralError: when ``tick`` is not a positive integer.
    :raises WaitingTimedOutError: when time quota has been consumed.
    """

    if tick <= 0:
        raise GeneralError('Tick must be a positive integer')

    NOW = time.monotonic

    deadline = NOW() + timeout.total_seconds()

    parent.debug(
        'wait',
        f"waiting for condition '{check.__name__}' with timeout {timeout},"
        f" deadline in {timeout.total_seconds()} seconds,"
        f" checking every {tick:.2f} seconds")

    while True:
        now = NOW()

        if now > deadline:
            parent.debug(
                'wait',
                f"'{check.__name__}' did not succeed,"
                f" {now - deadline:.2f} over quota")

            raise WaitingTimedOutError(check, timeout)

        try:
            ret = check()

            # Perform one extra check: if `check()` succeeded, but took more time than
            # allowed, it should be recognized as a failed waiting too.
            now = NOW()

            if now > deadline:
                parent.debug(
                    'wait',
                    f"'{check.__name__}' finished successfully but took too much time,"
                    f" {now - deadline:.2f} over quota")

                raise WaitingTimedOutError(check, timeout, check_success=True)

            parent.debug(
                'wait',
                f"'{check.__name__}' finished successfully,"
                f" {deadline - now:.2f} seconds left")

            return ret

        except WaitingIncomplete:
            # Update timestamp for more accurate logging - check() could have taken minutes
            # to complete, using the pre-check timestamp for logging would be misleading.
            now = NOW()

            parent.debug(
                'wait',
                f"'{check.__name__}' still pending,"
                f" {deadline - now:.2f} seconds left,"
                f" current tick {tick:.2f} seconds")

            time.sleep(tick)

            tick *= tick_increase

            continue


class ValidateFmfMixin(_CommonBase):
    """
    Mixin adding validation of an fmf node.

    Loads a schema whose name is derived from class name, and uses fmf's validate()
    method to perform the validation.
    """

    def _validate_fmf_node(
            self,
            node: fmf.Tree,
            raise_on_validation_error: bool,
            logger: tmt.log.Logger) -> None:
        """ Validate a given fmf node """

        errors = validate_fmf_node(
            node, f'{self.__class__.__name__.lower()}.yaml')

        if errors:
            if raise_on_validation_error:
                raise SpecificationError(
                    f'fmf node {node.name} failed validation',
                    validation_errors=errors)

            for _, error_message in errors:
                logger.warn(error_message, shift=1)

    def __init__(
            self,
            *,
            node: fmf.Tree,
            skip_validation: bool = False,
            raise_on_validation_error: bool = False,
            logger: tmt.log.Logger,
            **kwargs: Any) -> None:
        # Validate *before* letting next class in line touch the data.
        if not skip_validation:
            self._validate_fmf_node(node, raise_on_validation_error, logger)

        super().__init__(node=node, logger=logger, **kwargs)


# A type representing compatible sources of keys and values.
KeySource = Union[Dict[str, Any], fmf.Tree]

NormalizeCallback = Callable[[Any, tmt.log.Logger], T]


def dataclass_normalize_field(
        container: Any,
        keyname: str,
        raw_value: Any,
        logger: tmt.log.Logger) -> Any:
    """
    Normalize and assign a value to container field.

    If there is a normalization callback defined for the field, either in field
    metadata or as a special ``_normalize_$keyname`` method, the method is
    called for ``raw_value``, and its return value is assigned to container
    field instead of ``value``.
    """

    normalize_callback: Optional[NormalizeCallback[Any]] = None

    # First try new-style fields, i.e. normalize callback stored in field metadata
    if dataclasses.is_dataclass(container):
        field: dataclasses.Field[Any] = dataclass_field_by_name(container, keyname)
        normalize_callback = dataclass_field_metadata(field).normalize_callback

    if not normalize_callback:
        normalize_callback = getattr(container, f'_normalize_{keyname}', None)

    if normalize_callback:
        value = normalize_callback(raw_value, logger)

    else:
        value = raw_value

    # As mentioned in BasePlugin._update_data_from_options, the test
    # performed there is questionable. To gain more visibility into how
    # normalization and CLI updates work together, a bit of logging of
    # values the CLI update process does not consider.
    #
    # Keep for debugging purposes, as long as normalization settles down.
    if value is None or value == [] or value == ():
        logger.debug(
            'field normalized to false-ish value',
            f'{container.__class__.__name__}.{keyname}',
            level=4)

        with_getattr = getattr(container, keyname, None)
        with_dict = container.__dict__.get(keyname, None)

        logger.debug('value', str(value), level=4, shift=1)
        logger.debug('current value (getattr)', str(with_getattr), level=4, shift=1)
        logger.debug('current value (__dict__)', str(with_dict), level=4, shift=1)

        if value != with_getattr or with_getattr != with_dict:
            logger.debug('known values do not match', level=4, shift=2)

    # Set attribute by adding it to __dict__ directly. Messing with setattr()
    # might cause re-use of mutable values by other instances.
    container.__dict__[keyname] = value

    return value


def normalize_string_list(
        value: Union[None, str, List[str]],
        logger: tmt.log.Logger) -> List[str]:
    """
    Normalize a string-or-list-of-strings input value.

    This is a fairly common input format present mostly in fmf nodes where
    tmt, to make things easier for humans, allows this:

    .. code-block:: yaml

       foo: bar

       foo:
         - bar
         - baz

    Internally, we should stick to one type only, and make sure whatever we get
    on the input, a list of strings would be the output.

    :param value: input value from key source.
    """

    if value is None:
        return []

    return [value] if isinstance(value, str) else value


def normalize_path_list(
        value: Union[None, str, List[str]],
        logger: tmt.log.Logger) -> List[Path]:
    """
    Normalize a path-or-list-of-paths input value.

    This is a fairly common input format present mostly in fmf nodes where
    tmt, to make things easier for humans, allows this:

    .. code-block:: yaml

       foo: /foo/bar

       foo:
         - /foo/bar
         - /baz

    Internally, we should stick to one type only, and make sure whatever we get
    on the input, a list of strings would be the output.

    :param value: input value from key source.
    """

    if value is None:
        return []

    if isinstance(value, str):
        return [Path(value)]

    if isinstance(value, (list, tuple)):
        return [Path(path) for path in value]

    # TODO: propagate field name down to normalization callbacks for better exceptions
    raise SpecificationError(
        f"Field can be either path or list of paths, '{type(value).__name__}' found.")


def normalize_shell_script_list(
        value: Union[None, str, List[str]],
        logger: tmt.log.Logger) -> List[ShellScript]:
    """
    Normalize a string-or-list-of-strings input value.

    This is a fairly common input format present mostly in fmf nodes where
    tmt, to make things easier for humans, allows this:

    .. code-block:: yaml

       foo: bar

       foo:
         - bar
         - baz

    Internally, we should stick to one type only, and make sure whatever we get
    on the input, a list of strings would be the output.

    :param value: input value from key source.
    """

    if value is None:
        return []

    if isinstance(value, str):
        return [ShellScript(value)]

    if isinstance(value, (list, tuple)):
        return [ShellScript(str(item)) for item in value]

    # TODO: propagate field name down to normalization callbacks for better exceptions
    raise SpecificationError(
        f"Field can be either string or list of strings, '{type(value).__name__}' found.")


class NormalizeKeysMixin(_CommonBase):
    """
    Mixin adding support for loading fmf keys into object attributes.

    When invoked, annotated class-level variables are searched for in a given source
    container - a mapping, an fmf node, etc. - and if the key of the same name as the
    variable exists, its value is "promoted" to instance variable.

    If a method named ``_normalize_<variable name>`` exists, it is called with the fmf
    key value as its single argument, and its return value is assigned to instance
    variable. This gives class chance to modify or transform the original value when
    needed, e.g. to convert the original value to a type more suitable for internal
    processing.
    """

    # If specified, keys would be iterated over in the order as listed here.
    KEYS_SHOW_ORDER: List[str] = []

    # NOTE: these could be static methods, self is probably useless, but that would
    # cause complications when classes assign these to their members. That makes them
    # no longer static as far as class is concerned, which means they get called with
    # `self` as the first argument. A workaround would be to assign staticmethod()-ized
    # version of them, but that's too much repetition.
    #
    # TODO: wouldn't it be nice if these could be mention in dataclass.field()?
    # It would require a clone of dataclass.field() though.
    def _normalize_string_list(
            self,
            value: Union[None, str, List[str]],
            logger: tmt.log.Logger) -> List[str]:
        if value is None:
            return []

        if isinstance(value, str):
            return [value]

        if isinstance(value, (list, tuple)):
            return [item for item in value]

        raise SpecificationError(
            f"Field can be either string or list of strings, '{type(value).__name__}' found.")

    def _normalize_environment(
            self,
            value: Optional[Dict[str, Any]],
            logger: tmt.log.Logger) -> EnvironmentType:
        if value is None:
            return {}

        return {
            name: str(value) for name, value in value.items()
            }

    def _normalize_script(
            self,
            value: Union[None, str, List[str]],
            logger: tmt.log.Logger) -> List[ShellScript]:
        """ Normalize inputs to a list of shell scripts """

        return normalize_shell_script_list(value, logger)

    @classmethod
    def _iter_key_annotations(cls) -> Generator[Tuple[str, Any], None, None]:
        """
        Iterate over keys' type annotations.

        Keys are yielded in the order: keys declared by parent classes first, then
        keys declared by the class itself, all following the order in which keys
        were defined in their respective classes.

        Yields:
            pairs of key name and its annotations.
        """

        def _iter_class_annotations(klass: type) -> Generator[Tuple[str, Any], None, None]:
            # Skip, needs fixes to become compatible
            if klass is Common:
                return

            for name, value in klass.__dict__.get('__annotations__', {}).items():
                # Skip special fields that are not keys.
                if name == 'KEYS_SHOW_ORDER':
                    continue

                yield (name, value)

        # Reverse MRO to start with the most base classes first, to iterate over keys
        # in the order they are defined.
        for klass in reversed(cls.__mro__):
            yield from _iter_class_annotations(klass)

    @classmethod
    def keys(cls) -> Generator[str, None, None]:
        """
        Iterate over key names.

        Keys are yielded in the order: keys declared by parent classes first, then
        keys declared by the class itself, all following the order in which keys
        were defined in their respective classes.

        Yields:
            key names.
        """

        for keyname, _ in cls._iter_key_annotations():
            yield keyname

    def items(self) -> Generator[Tuple[str, Any], None, None]:
        """
        Iterate over keys and their values.

        Keys are yielded in the order: keys declared by parent classes first, then
        keys declared by the class itself, all following the order in which keys
        were defined in their respective classes.

        Yields:
            pairs of key name and its value.
        """

        for keyname in self.keys():
            yield (keyname, getattr(self, keyname))

    # TODO: exists for backward compatibility for the transition period. Once full
    # type annotations land, there should be no need for extra _keys attribute.
    @classmethod
    def _keys(cls) -> List[str]:
        """ Return a list of names of object's keys. """

        return list(cls.keys())

    def _load_keys(
            self,
            key_source: Dict[str, Any],
            key_source_name: str,
            logger: tmt.log.Logger) -> None:
        """ Extract values for class-level attributes, and verify they match declared types. """

        LOG_SHIFT, LOG_LEVEL = 2, 4

        debug_intro = functools.partial(logger.debug, shift=LOG_SHIFT - 1, level=LOG_LEVEL)
        debug = functools.partial(logger.debug, shift=LOG_SHIFT, level=LOG_LEVEL)

        debug_intro('key source')
        for k, v in key_source.items():
            debug(f'{k}: {v} ({type(v)})')

        debug('')

        for keyname, keytype in self._iter_key_annotations():
            key_address = f'{key_source_name}:{keyname}'
            source_keyname = key_to_option(keyname)

            # Do not indent this particular entry like the rest, so it could serve
            # as a "header" for a single key processing.
            debug_intro('key', key_address)
            debug('field', source_keyname)

            debug('desired type', str(keytype))

            value: Any = None

            # Verbose, let's hide it a bit deeper.
            debug('dict', self.__dict__, level=LOG_LEVEL + 1)

            if hasattr(self, keyname):
                # If the key exists as instance's attribute already, it is because it's been
                # declared with a default value, and the attribute now holds said default value.
                default_value = getattr(self, keyname)

                # If the default value is a mutable container, we cannot use it directly.
                # Should we do so, the very same default value would be assigned to multiple
                # instances/attributes instead of each instance having its own distinct container.
                if isinstance(default_value, (list, dict)):
                    debug('detected mutable default')
                    default_value = copy.copy(default_value)

                debug('default value', str(default_value))
                debug('default value type', str(type(default_value)))

                # try+except seems to work better than get(), especially when
                # semantic of fmf.Tree.get() is slightly different than that
                # of dict().get().
                try:
                    value = key_source[source_keyname]

                except KeyError:
                    value = default_value

                debug('raw value', str(value))
                debug('raw value type', str(type(value)))

            else:
                value = key_source.get(source_keyname)

                debug('raw value', str(value))
                debug('raw value type', str(type(value)))

            value = dataclass_normalize_field(self, keyname, value, logger)

            debug('final value', str(value))
            debug('final value type', str(type(value)))

            # Apparently pointless, but makes the debugging output more readable.
            # There may be plenty of tests and plans and keys, a bit of spacing
            # can't hurt.
            debug('')

        debug_intro('normalized fields')
        for k, v in self.__dict__.items():
            debug(f'{k}: {v} ({type(v)})')

        debug('')

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class LoadFmfKeysMixin(NormalizeKeysMixin):
    def __init__(
            self,
            *,
            node: fmf.Tree,
            logger: tmt.log.Logger,
            **kwargs: Any) -> None:
        self._load_keys(node.get(), node.name, logger)

        super().__init__(node=node, logger=logger, **kwargs)


FieldCLIOption = Union[str, Sequence[str]]


@overload
def field(
        *,
        default: bool,
        # Options
        option: Optional[FieldCLIOption] = None,
        is_flag: bool = True,
        choices: Union[None, Sequence[str], Callable[[], Sequence[str]]] = None,
        multiple: bool = False,
        metavar: Optional[str] = None,
        help: Optional[str] = None,
        # Input data normalization - not needed, the field is a boolean
        # flag.
        # normalize: Optional[NormalizeCallback[T]] = None
        # Custom serialization
        # serialize: Optional[SerializeCallback[bool]] = None,
        # unserialize: Optional[UnserializeCallback[bool]] = None
        ) -> bool:
    pass


@overload
def field(
        *,
        default: T,
        # Options
        option: Optional[FieldCLIOption] = None,
        is_flag: bool = False,
        choices: Union[None, Sequence[str], Callable[[], Sequence[str]]] = None,
        multiple: bool = False,
        metavar: Optional[str] = None,
        help: Optional[str] = None,
        # Input data normalization
        normalize: Optional[NormalizeCallback[T]] = None,
        # Custom serialization
        serialize: Optional[SerializeCallback[T]] = None,
        unserialize: Optional[UnserializeCallback[T]] = None
        ) -> T:
    pass


@overload
def field(
        *,
        default_factory: Callable[[], T],
        # Options
        option: Optional[FieldCLIOption] = None,
        is_flag: bool = False,
        choices: Union[None, Sequence[str], Callable[[], Sequence[str]]] = None,
        multiple: bool = False,
        metavar: Optional[str] = None,
        help: Optional[str] = None,
        # Input data normalization
        normalize: Optional[NormalizeCallback[T]] = None,
        # Custom serialization
        serialize: Optional[SerializeCallback[T]] = None,
        unserialize: Optional[UnserializeCallback[T]] = None
        ) -> T:
    pass


def field(
        *,
        default: Any = dataclasses.MISSING,
        default_factory: Any = dataclasses.MISSING,
        # Options
        option: Optional[FieldCLIOption] = None,
        is_flag: bool = False,
        choices: Union[None, Sequence[str], Callable[[], Sequence[str]]] = None,
        multiple: bool = False,
        metavar: Optional[str] = None,
        help: Optional[str] = None,
        # Input data normalization
        normalize: Optional[NormalizeCallback[T]] = None,
        # Custom serialization
        serialize: Optional[SerializeCallback[T]] = None,
        unserialize: Optional[UnserializeCallback[T]] = None
        ) -> Any:
    """
    Define a :py:class:`DataContainer` field.

    Effectively a fancy wrapper over :py:func:`dataclasses.field`, tailored for
    tmt code needs and simplification of various common tasks.

    :param default: if provided, this will be the default value for this field.
        Passed directly to :py:func:`dataclass.field`.
        It is an error to specify both ``default`` and ``default_factory``.
    :param default_factory: if provided, it must be a zero-argument callable
        that will be called when a default value is needed for this field.
        Passed directly to :py:func:`dataclass.field`.
        It is an error to specify both ``default`` and ``default_factory``.
    :param option: one or more command-line option names.
        Passed directly to :py:func:`click.option`.
    :param is_flag: marks this option as a flag.
        Passed directly to :py:func:`click.option`.
    :param choices: if provided, the command-line option would accept only
        the listed input values.
        Passed to :py:func:`click.option` as a :py:class:`click.Choice` instance.
    :param metavar: how the input value is represented in the help page.
        Passed directly to :py:func:`click.option`.
    :param help: the help string for the command-line option.
        Passed directly to :py:func:`click.option`.
    :param normalize: a callback for normalizing the input value. Consumed by
        :py:class:`NormalizeKeysMixin`.
    :param serialize: a callback for custom serialization of the field value.
        Consumed by :py:class:`SerializableKeysMixin`.
    :param unserialize: a callback for custom unserialization of the field value.
        Consumed by :py:class:`SerializableKeysMixin`.
    """

    if default is dataclasses.MISSING and default_factory is dataclasses.MISSING:
        raise GeneralError("Container field must define one of 'default' or 'default_factory'.")

    metadata: FieldMetadata[T] = FieldMetadata()

    if option:
        assert is_flag is False or isinstance(default, bool)

        metadata.option_args = (option,) if isinstance(option, str) else option
        metadata.option_kwargs = {
            'is_flag': is_flag,
            'multiple': multiple,
            'metavar': metavar,
            'help': help
            }
        metadata.option_choices = choices

        if default is not dataclasses.MISSING and not is_flag:
            metadata.option_kwargs['default'] = default

    if normalize:
        metadata.normalize_callback = normalize

    metadata.serialize_callback = serialize
    metadata.unserialize_callback = unserialize

    # ignore[call-overload]: returning "wrong" type on purpose. field() must be annotated
    # as if returning the value of type matching the field declaration, and the original
    # field() is called with wider argument types than expected, because we use our own
    # overloading to narrow types *our* custom field() accepts.
    return dataclasses.field(  # type: ignore[call-overload]
        default=default,
        default_factory=default_factory,
        metadata={'tmt': metadata}
        )


def default_template_environment() -> jinja2.Environment:
    """
    Create a Jinja2 environment with default settings.

    Adds common filters, and enables block trimming and left strip.
    """

    environment = jinja2.Environment()

    environment.filters['findall'] = lambda s, pattern: re.findall(pattern, s)
    environment.filters['listed'] = fmf.utils.listed
    environment.filters['strip'] = lambda x: x.strip()
    environment.filters['search'] = lambda string, pattern: re.search(pattern, string)
    environment.filters['match'] = lambda string, pattern: re.search(pattern, string)
    environment.filters['regex_replace'] = lambda string, pattern, repl: re.sub(
        pattern, repl, string)

    environment.trim_blocks = True
    environment.lstrip_blocks = True

    return environment


def render_template_file(
        template_filepath: Path,
        environment: Optional[jinja2.Environment] = None,
        **variables: Any
        ) -> str:
    """ Render a template read from a file """

    environment = environment or default_template_environment()

    try:
        template = environment.from_string(template_filepath.read_text())

        return cast(str, template.render(**variables).strip())

    except jinja2.exceptions.TemplateSyntaxError as exc:
        raise GeneralError(
            f"Could not parse template '{template_filepath}' at line {exc.lineno}.") from exc

    except jinja2.exceptions.TemplateError as exc:
        raise GeneralError(f"Could not render template '{template_filepath}'.") from exc


@lru_cache(maxsize=None)
def is_selinux_supported() -> bool:
    """
    Returns ``true`` if SELinux filesystem is supported by the kernel, ``false`` otherwise.

    For detection ``/proc/filesystems`` is used, see ``man 5 filesystems`` for details.
    """
    with open('/proc/filesystems', 'r') as file:
        return any('selinuxfs' in line for line in file)
