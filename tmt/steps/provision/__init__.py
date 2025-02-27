import collections
import dataclasses
import datetime
import enum
import os
import random
import re
import shlex
import string
import subprocess
import tempfile
from shlex import quote
from typing import (TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type,
                    Union, cast, overload)

import click
import fmf

import tmt
import tmt.log
import tmt.plugins
import tmt.steps
import tmt.utils
from tmt.steps import Action
from tmt.utils import BaseLoggerFnType, Command, Path, ShellScript

if TYPE_CHECKING:
    import tmt.base
    import tmt.cli


# Timeout in seconds of waiting for a connection after reboot
CONNECTION_TIMEOUT = 5 * 60

# When waiting for guest to recover from reboot, try re-connecting every
# this many seconds.
RECONNECT_WAIT_TICK = 5
RECONNECT_WAIT_TICK_INCREASE = 1.0

# Default rsync options
DEFAULT_RSYNC_OPTIONS = [
    "-s", "-R", "-r", "-z", "--links", "--safe-links", "--delete"]

DEFAULT_RSYNC_PUSH_OPTIONS = ["-s", "-R", "-r", "-z", "--links", "--safe-links", "--delete"]
DEFAULT_RSYNC_PULL_OPTIONS = ["-s", "-R", "-r", "-z", "--links", "--safe-links", "--protect-args"]


class CheckRsyncOutcome(enum.Enum):
    ALREADY_INSTALLED = 'already-installed'
    INSTALLED = 'installed'


@dataclasses.dataclass
class GuestData(tmt.utils.SerializableContainer):
    """
    Keys necessary to describe, create, save and restore a guest.

    Very basic set of keys shared across all known guest classes.
    """

    # guest role in the multihost scenario
    role: Optional[str] = None
    # hostname or ip address
    guest: Optional[str] = None


class Guest(tmt.utils.Common):
    """
    Guest provisioned for test execution

    A base class for guest-like classes. Provides some of the basic methods
    and functionality, but note some of the methods are left intentionally
    empty. These do not have valid implementation on this level, and it's up
    to Guest subclasses to provide one working in their respective
    infrastructure.

    The following keys are expected in the 'data' container::

        role ....... guest role in the multihost scenario
        guest ...... name, hostname or ip address

    These are by default imported into instance attributes.
    """

    # Used by save() to construct the correct container for keys.
    _data_class: Type[GuestData] = GuestData

    role: Optional[str]
    guest: Optional[str]

    # Flag to indicate localhost guest, requires special handling
    localhost = False

    # TODO: do we need this list? Can whatever code is using it use _data_class directly?
    # List of supported keys
    # (used for import/export to/from attributes during load and save)
    @property
    def _keys(self) -> List[str]:
        return list(self._data_class.keys())

    def __init__(self,
                 *,
                 data: GuestData,
                 name: Optional[str] = None,
                 parent: Optional[tmt.utils.Common] = None,
                 logger: tmt.log.Logger) -> None:
        """ Initialize guest data """
        super().__init__(logger=logger, parent=parent, name=name)

        self.load(data)

    def _random_name(self, prefix: str = '', length: int = 16) -> str:
        """ Generate a random name """
        # Append at least 5 random characters
        min_random_part = max(5, length - len(prefix))
        name = prefix + ''.join(
            random.choices(string.ascii_letters, k=min_random_part))
        # Return tail (containing random characters) of name
        return name[-length:]

    def _tmt_name(self) -> str:
        """ Generate a name prefixed with tmt run id """
        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(Provision, self.parent)

        assert parent.plan.my_run is not None  # narrow type
        assert parent.plan.my_run.workdir is not None  # narrow type
        run_id = parent.plan.my_run.workdir.name
        return self._random_name(prefix="tmt-{0}-".format(run_id[-3:]))

    @property
    def full_name(self) -> str:
        """ Return guest's full name, i.e. name and its role """

        if self.role is None:
            return self.name

        return f'{self.name} ({self.role})'

    @property
    def is_ready(self) -> bool:
        """ Detect guest is ready or not """

        raise NotImplementedError()

    @classmethod
    def options(cls, how: Optional[str] = None) -> List[tmt.options.ClickOptionDecoratorType]:
        """ Prepare command line options related to guests """
        return list()

    def load(self, data: GuestData) -> None:
        """
        Load guest data into object attributes for easy access

        Called during guest object initialization. Takes care of storing
        all supported keys (see class attribute _keys for the list) from
        provided data to the guest object attributes. Child classes can
        extend it to make additional guest attributes easily available.

        Data dictionary can contain guest information from both command
        line options / L2 metadata / user configuration and wake up data
        stored by the save() method below.
        """
        data.inject_to(self)

    def save(self) -> GuestData:
        """
        Save guest data for future wake up

        Export all essential guest data into a dictionary which will be
        stored in the `guests.yaml` file for possible future wake up of
        the guest. Everything needed to attach to a running instance
        should be added into the data dictionary by child classes.
        """
        return self._data_class.extract_from(self)

    def wake(self) -> None:
        """
        Wake up the guest

        Perform any actions necessary after step wake up to be able to
        attach to a running guest instance and execute commands. Called
        after load() is completed so all guest data should be prepared.
        """
        self.debug(f"Doing nothing to wake up guest '{self.guest}'.")

    def start(self) -> None:
        """
        Start the guest

        Get a new guest instance running. This should include preparing
        any configuration necessary to get it started. Called after
        load() is completed so all guest data should be available.
        """
        self.debug(f"Doing nothing to start guest '{self.guest}'.")

    def details(self) -> None:
        """ Show guest details such as distro and kernel """
        # Skip distro & kernel check in dry mode
        if self.opt('dry'):
            return

        self.info('full name', self.full_name, 'green')

        # A small helper to make the repeated run & extract combo easier on eyes.
        def _fetch_detail(command: Command, pattern: str) -> str:
            output = self.execute(command, silent=True)

            if not output.stdout:
                raise tmt.utils.RunError(
                    'command produced no usable output',
                    command,
                    0,
                    output.stdout,
                    output.stderr)

            match = re.search(pattern, output.stdout)

            if not match:
                raise tmt.utils.RunError(
                    'command produced no usable output',
                    command,
                    0,
                    output.stdout,
                    output.stderr)

            return match.group(1)

        # Distro
        distro_commands: List[Tuple[Command, str]] = [
            # Check os-release first
            (Command('cat', '/etc/os-release'), r'PRETTY_NAME="(.*)"'),
            # Check for lsb-release
            (Command('cat', '/etc/lsb-release'), r'DISTRIB_DESCRIPTION="(.*)"'),
            # Check for redhat-release
            (Command('cat', '/etc/redhat-release'), r'(.*)')
            ]

        for command, pattern in distro_commands:
            try:
                distro: Optional[str] = _fetch_detail(command, pattern)
                break
            except tmt.utils.RunError:
                continue
        else:
            distro = None

        # Handle standard cloud images message when connecting
        if distro is not None and 'Please login as the user' in distro:
            raise tmt.utils.GeneralError(
                f'Login to the guest failed.\n{distro}')

        if distro:
            self.info('distro', distro, 'green')

        # Kernel
        kernel = _fetch_detail(Command('uname', '-r'), r'(.+)')
        self.verbose('kernel', kernel, 'green')

    def _ansible_verbosity(self) -> List[str]:
        """ Prepare verbose level based on the --debug option count """
        if self.opt('debug') < 3:
            return []
        else:
            return ['-' + (self.opt('debug') - 2) * 'v']

    @staticmethod
    def _ansible_extra_args(extra_args: Optional[str]) -> List[str]:
        """ Prepare extra arguments for ansible-playbook"""
        if extra_args is None:
            return []
        else:
            return shlex.split(str(extra_args))

    def _ansible_summary(self, output: Optional[str]) -> None:
        """ Check the output for ansible result summary numbers """
        if not output:
            return
        keys = 'ok changed unreachable failed skipped rescued ignored'.split()
        for key in keys:
            matched = re.search(rf'^.*\s:\s.*{key}=(\d+).*$', output, re.M)
            if matched and int(matched.group(1)) > 0:
                tasks = fmf.utils.listed(matched.group(1), 'task')
                self.verbose(key, tasks, 'green')

    def _ansible_playbook_path(self, playbook: Path) -> Path:
        """ Prepare full ansible playbook path """
        self.debug(f"Applying playbook '{playbook}' on guest '{self.guest}'.")
        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(Provision, self.parent)
        assert parent.plan.my_run is not None  # narrow type
        assert parent.plan.my_run.tree is not None  # narrow type
        assert parent.plan.my_run.tree.root is not None  # narrow type
        # Playbook paths should be relative to the metadata tree root
        playbook = parent.plan.my_run.tree.root / playbook.unrooted()
        self.debug(f"Playbook full path: '{playbook}'", level=2)
        return playbook

    def _prepare_environment(
        self,
        execute_environment: Optional[tmt.utils.EnvironmentType] = None
            ) -> tmt.utils.EnvironmentType:
        """ Prepare dict of environment variables """
        # Prepare environment variables so they can be correctly passed
        # to shell. Create a copy to prevent modifying source.
        environment: tmt.utils.EnvironmentType = dict()
        environment.update(execute_environment or dict())
        # Plan environment and variables provided on the command line
        # override environment provided to execute().
        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(Provision, self.parent)
        environment.update(parent.plan.environment)
        return environment

    @staticmethod
    def _export_environment(environment: tmt.utils.EnvironmentType) -> List[ShellScript]:
        """ Prepare shell export of environment variables """
        if not environment:
            return []
        return [
            ShellScript(f'export {variable}')
            for variable in tmt.utils.shell_variables(environment)
            ]

    def ansible(self, playbook: Path, extra_args: Optional[str] = None) -> None:
        """ Prepare guest using ansible playbook """

        raise NotImplementedError()

    @overload
    def execute(self,
                command: tmt.utils.ShellScript,
                cwd: Optional[Path] = None,
                env: Optional[tmt.utils.EnvironmentType] = None,
                friendly_command: Optional[str] = None,
                test_session: bool = False,
                silent: bool = False,
                log: Optional[BaseLoggerFnType] = None,
                interactive: bool = False,
                **kwargs: Any) -> tmt.utils.CommandOutput:
        pass

    @overload
    def execute(self,
                command: tmt.utils.Command,
                cwd: Optional[Path] = None,
                env: Optional[tmt.utils.EnvironmentType] = None,
                friendly_command: Optional[str] = None,
                test_session: bool = False,
                silent: bool = False,
                log: Optional[BaseLoggerFnType] = None,
                interactive: bool = False,
                **kwargs: Any) -> tmt.utils.CommandOutput:
        pass

    def execute(self,
                command: Union[tmt.utils.Command, tmt.utils.ShellScript],
                cwd: Optional[Path] = None,
                env: Optional[tmt.utils.EnvironmentType] = None,
                friendly_command: Optional[str] = None,
                test_session: bool = False,
                silent: bool = False,
                log: Optional[BaseLoggerFnType] = None,
                interactive: bool = False,
                **kwargs: Any) -> tmt.utils.CommandOutput:
        """
        Execute a command on the guest.

        :param command: either a command or a shell script to execute.
        :param cwd: if set, execute command in this directory on the guest.
        :param env: if set, set these environment variables before running the command.
        :param friendly_command: nice, human-friendly representation of the command.
        """

        raise NotImplementedError()

    def push(self,
             source: Optional[Path] = None,
             destination: Optional[Path] = None,
             options: Optional[List[str]] = None) -> None:
        """
        Push files to the guest
        """

        raise NotImplementedError()

    def pull(self,
             source: Optional[Path] = None,
             destination: Optional[Path] = None,
             options: Optional[List[str]] = None,
             extend_options: Optional[List[str]] = None) -> None:
        """
        Pull files from the guest
        """

        raise NotImplementedError()

    def stop(self) -> None:
        """
        Stop the guest

        Shut down a running guest instance so that it does not consume
        any memory or cpu resources. If needed, perform any actions
        necessary to store the instance status to disk.
        """

        raise NotImplementedError()

    def reboot(
            self,
            hard: bool = False,
            command: Optional[Union[Command, ShellScript]] = None,
            timeout: Optional[int] = None) -> bool:
        """
        Reboot the guest, return True if successful

        Parameter 'hard' set to True means that guest should be
        rebooted by way which is not clean in sense that data can be
        lost. When set to False reboot should be done gracefully.

        Use the 'command' parameter to specify a custom reboot command
        instead of the default 'reboot'.

        Parameter 'timeout' can be used to specify time (in seconds) to
        wait for the guest to come back up after rebooting.
        """

        raise NotImplementedError()

    def reconnect(
            self,
            timeout: Optional[int] = None,
            tick: float = RECONNECT_WAIT_TICK,
            tick_increase: float = RECONNECT_WAIT_TICK_INCREASE
            ) -> bool:
        """
        Ensure the connection to the guest is working

        The default timeout is 5 minutes. Custom number of seconds can be
        provided in the `timeout` parameter. This may be useful when long
        operations (such as system upgrade) are performed.
        """
        # The default is handled here rather than in the argument so that
        # the caller can pass in None as an argument (i.e. don't care value)
        timeout = timeout or CONNECTION_TIMEOUT
        self.debug("Wait for a connection to the guest.")

        def try_whoami() -> None:
            try:
                self.execute(Command('whoami'), silent=True)

            except tmt.utils.RunError:
                raise tmt.utils.WaitingIncomplete()

        try:
            tmt.utils.wait(
                self,
                try_whoami,
                datetime.timedelta(seconds=timeout),
                tick=tick,
                tick_increase=tick_increase)

        except tmt.utils.WaitingTimedOutError:
            self.debug("Connection to guest failed after reboot.")
            return False

        return True

    def remove(self) -> None:
        """
        Remove the guest

        Completely remove all guest instance data so that it does not
        consume any disk resources.
        """
        self.debug(f"Doing nothing to remove guest '{self.guest}'.")

    def _check_rsync(self) -> CheckRsyncOutcome:
        """
        Make sure that rsync is installed on the guest

        On read-only distros install it under the '/root/pkg' directory.
        Returns 'already installed' when rsync is already present.
        """

        # Check for rsync (nothing to do if already installed)
        self.debug("Ensure that rsync is installed on the guest.")
        try:
            self.execute(Command('rsync', '--version'))
            return CheckRsyncOutcome.ALREADY_INSTALLED
        except tmt.utils.RunError:
            pass

        # Check the package manager
        self.debug("Check the package manager.")
        try:
            self.execute(Command('dnf', '--version'))
            package_manager = "dnf"
        except tmt.utils.RunError:
            package_manager = "yum"

        # Install under '/root/pkg' for read-only distros
        # (for now the check is based on 'rpm-ostree' presence)
        # FIXME: Find a better way how to detect read-only distros
        self.debug("Check for a read-only distro.")
        try:
            self.execute(Command('rpm-ostree', '--version'))
            readonly = (
                " --installroot=/root/pkg --releasever / "
                "&& ln -sf /root/pkg/bin/rsync /usr/local/bin/rsync")
        except tmt.utils.RunError:
            readonly = ""

        # Install the rsync
        self.execute(ShellScript(f"{package_manager} install -y rsync" + readonly))

        return CheckRsyncOutcome.INSTALLED

    @classmethod
    def requires(cls) -> List['tmt.base.Require']:
        """ All requirements of the guest implementation """
        return []


@dataclasses.dataclass
class GuestSshData(GuestData):
    """
    Keys necessary to describe, create, save and restore a guest with SSH
    capability.

    Derived from GuestData, this class adds keys relevant for guests that can be
    reached over SSH.
    """

    # port to connect to
    port: Optional[int] = None
    # user name to log in
    user: Optional[str] = None
    # path to the private key
    key: List[str] = dataclasses.field(default_factory=list)
    # password
    password: Optional[str] = None
    ssh_option: List[str] = dataclasses.field(default_factory=list)

    _normalize_key = tmt.utils.NormalizeKeysMixin._normalize_string_list


class GuestSsh(Guest):
    """
    Guest provisioned for test execution, capable of accepting SSH connections

    The following keys are expected in the 'data' dictionary::

        role ....... guest role in the multihost scenario (inherited)
        guest ...... hostname or ip address (inherited)
        port ....... port to connect to
        user ....... user name to log in
        key ........ path to the private key (str or list)
        password ... password

    These are by default imported into instance attributes.
    """

    _data_class: Type[GuestData] = GuestSshData

    port: Optional[int]
    user: Optional[str]
    key: List[Path]
    password: Optional[str]
    ssh_option: List[str]

    # Master ssh connection process and socket path
    _ssh_master_process: Optional['subprocess.Popen[bytes]'] = None
    _ssh_socket_path: Optional[Path] = None

    def _ssh_guest(self) -> str:
        """ Return user@guest """
        return f'{self.user}@{self.guest}'

    def _ssh_socket(self) -> Path:
        """ Prepare path to the master connection socket """
        if not self._ssh_socket_path:
            # Use '/run/user/uid' if it exists, '/tmp' otherwise
            run_dir = Path(f"/run/user/{os.getuid()}")
            if run_dir.is_dir():
                socket_dir = run_dir / "tmt"
            else:
                socket_dir = Path("/tmp")
            socket_dir.mkdir(exist_ok=True)
            self._ssh_socket_path = Path(tempfile.mktemp(dir=socket_dir))
        return self._ssh_socket_path

    def _ssh_options(self) -> Command:
        """ Return common ssh options (list or joined) """
        options = [
            '-oForwardX11=no',
            '-oStrictHostKeyChecking=no',
            '-oUserKnownHostsFile=/dev/null',
            # Prevent ssh from disconnecting if no data has been
            # received from the server for a long time (#868).
            '-oServerAliveInterval=60',
            '-oServerAliveCountMax=5',
            ]
        if self.key or self.password:
            # Skip ssh-agent (it adds additional identities)
            options.append('-oIdentitiesOnly=yes')
        if self.port:
            options.append(f'-p{self.port}')
        if self.key:
            for key in self.key:
                options.extend(['-i', str(key)])
        if self.password:
            options.extend(['-oPasswordAuthentication=yes'])

        # Use the shared master connection
        options.append(f'-S{self._ssh_socket()}')

        options.extend([f'-o{option}' for option in self.ssh_option])

        return Command(*options)

    def _ssh_master_connection(self, command: Command) -> None:
        """ Check/create the master ssh connection """
        if self._ssh_master_process:
            return

        # Do not modify the original command...
        ssh_master_command = command + self._ssh_options() + Command("-MNnT", self._ssh_guest())
        self.debug(f"Create the master ssh connection: {ssh_master_command}")
        self._ssh_master_process = subprocess.Popen(
            ssh_master_command.to_popen(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def _ssh_command(self) -> Command:
        """ Prepare an ssh command line for execution """
        command = Command(
            *(["sshpass", "-p", self.password] if self.password else []),
            "ssh"
            )

        # Check the master connection
        self._ssh_master_connection(command)

        return command + self._ssh_options()

    @classmethod
    def options(cls, how: Optional[str] = None) -> List[tmt.options.ClickOptionDecoratorType]:
        """ Prepare command line options related to SSH-capable guests """
        return super().options(how=how) + [
            click.option('--ssh-option', metavar="OPTION", multiple=True, default=[],
                         help="Specify additional SSH option. "
                              "Value is passed to SSH's -o option, see ssh_config(5) for "
                              "supported options. Can be specified multiple times.")]

    def ansible(self, playbook: Path, extra_args: Optional[str] = None) -> None:
        """ Prepare guest using ansible playbook """
        playbook = self._ansible_playbook_path(playbook)

        ansible_command = Command('ansible-playbook', *self._ansible_verbosity())

        if extra_args:
            ansible_command += self._ansible_extra_args(extra_args)

        ansible_command += Command(
            '--ssh-common-args', self._ssh_options().to_element(),
            '-i', f'{self._ssh_guest()},',
            str(playbook))

        # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
        parent = cast(Provision, self.parent)

        stdout, _ = self.run(
            ansible_command,
            cwd=parent.plan.worktree,
            env=self._prepare_environment())
        self._ansible_summary(stdout)

    @property
    def is_ready(self) -> bool:
        """ Detect guest is ready or not """

        # Enough for now, ssh connection can be created later
        return self.guest is not None

    def execute(self,
                command: Union[tmt.utils.Command, tmt.utils.ShellScript],
                cwd: Optional[Path] = None,
                env: Optional[tmt.utils.EnvironmentType] = None,
                friendly_command: Optional[str] = None,
                test_session: bool = False,
                silent: bool = False,
                log: Optional[BaseLoggerFnType] = None,
                interactive: bool = False,
                **kwargs: Any) -> tmt.utils.CommandOutput:
        """
        Execute a command on the guest.

        :param command: either a command or a shell script to execute.
        :param cwd: execute command in this directory on the guest.
        :param env: if set, set these environment variables before running the command.
        :param friendly_command: nice, human-friendly representation of the command.
        """

        # Abort if guest is unavailable
        if self.guest is None:
            if not self.opt('dry'):
                raise tmt.utils.GeneralError('The guest is not available.')

        ssh_command: tmt.utils.Command = self._ssh_command()

        # Run in interactive mode if requested
        if interactive:
            ssh_command += Command('-t')

        # Force ssh to allocate pseudo-terminal if requested. Without a pseudo-terminal,
        # remote processes spawned by SSH would keep running after SSH process death, e.g.
        # in the case of a timeout.
        #
        # Note that polite request, `-t`, is not enough since `ssh` itself has no pseudo-terminal,
        # and a single `-t` wouldn't have the necessary effect.
        if test_session:
            ssh_command += Command('-tt')

        # Accumulate all necessary commands - they will form a "shell" script, a single
        # string passed to SSH to execute on the remote machine.
        remote_commands: ShellScript = ShellScript.from_scripts(
            self._export_environment(self._prepare_environment(env))
            )

        # Change to given directory on guest if cwd provided
        if cwd:
            remote_commands += ShellScript(f'cd {quote(str(cwd))}')

        if isinstance(command, Command):
            remote_commands += command.to_script()

        else:
            remote_commands += command

        remote_command = remote_commands.to_element()

        ssh_command += [
            self._ssh_guest(),
            remote_command
            ]

        self.debug(f"Execute command '{remote_command}' on guest '{self.guest}'.")

        if friendly_command is None:
            friendly_command = str(command)

        return self.run(
            ssh_command,
            log=log if log else self._command_verbose_logger,
            friendly_command=friendly_command,
            silent=silent,
            cwd=cwd,
            interactive=interactive,
            **kwargs)

    def push(self,
             source: Optional[Path] = None,
             destination: Optional[Path] = None,
             options: Optional[List[str]] = None) -> None:
        """
        Push files to the guest

        By default the whole plan workdir is synced to the same location
        on the guest. Use the 'source' and 'destination' to sync custom
        location and the 'options' parametr to modify default options
        which are '-Rrz --links --safe-links --delete'.
        """
        # Abort if guest is unavailable
        if self.guest is None:
            if not self.opt('dry'):
                raise tmt.utils.GeneralError('The guest is not available.')

        # Prepare options and the push command
        options = options or DEFAULT_RSYNC_PUSH_OPTIONS
        if destination is None:
            destination = Path("/")
        if source is None:
            # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
            parent = cast(Provision, self.parent)

            assert parent.plan.workdir is not None

            source = parent.plan.workdir
            self.debug(f"Push workdir to guest '{self.guest}'.")
        else:
            self.debug(f"Copy '{source}' to '{destination}' on the guest.")

        def rsync() -> None:
            """ Run the rsync command """
            # In closure, mypy has hard times to reason about the state of used variables.
            assert options
            assert source
            assert destination

            self.run(Command(
                "rsync",
                *options,
                "-e", self._ssh_command().to_element(),
                str(source),
                f"{self._ssh_guest()}:{destination}"
                ))

        # Try to push twice, check for rsync after the first failure
        try:
            rsync()
        except tmt.utils.RunError:
            try:
                if self._check_rsync() == CheckRsyncOutcome.ALREADY_INSTALLED:
                    raise
                rsync()
            except tmt.utils.RunError:
                # Provide a reasonable error to the user
                self.fail(
                    f"Failed to push workdir to the guest. This usually means "
                    f"that login as '{self.user}' to the guest does not work.")
                raise

    def pull(self,
             source: Optional[Path] = None,
             destination: Optional[Path] = None,
             options: Optional[List[str]] = None,
             extend_options: Optional[List[str]] = None) -> None:
        """
        Pull files from the guest

        By default the whole plan workdir is synced from the same
        location on the guest. Use the 'source' and 'destination' to
        sync custom location, the 'options' parameter to modify
        default options :py:data:`DEFAULT_RSYNC_PULL_OPTIONS`
        and 'extend_options' to extend them (e.g. by exclude).
        """
        # Abort if guest is unavailable
        if self.guest is None:
            if not self.opt('dry'):
                raise tmt.utils.GeneralError('The guest is not available.')

        # Prepare options and the pull command
        options = options or DEFAULT_RSYNC_PULL_OPTIONS
        if extend_options is not None:
            options.extend(extend_options)
        if destination is None:
            destination = Path("/")
        if source is None:
            # FIXME: cast() - https://github.com/teemtee/tmt/issues/1372
            parent = cast(Provision, self.parent)

            assert parent.plan.workdir is not None

            source = parent.plan.workdir
            self.debug(f"Pull workdir from guest '{self.guest}'.")
        else:
            self.debug(f"Copy '{source}' from the guest to '{destination}'.")

        def rsync() -> None:
            """ Run the rsync command """
            # In closure, mypy has hard times to reason about the state of used variables.
            assert options
            assert source
            assert destination

            self.run(Command(
                "rsync",
                *options,
                "-e", self._ssh_command().to_element(),
                f"{self._ssh_guest()}:{source}",
                str(destination)
                ))

        # Try to pull twice, check for rsync after the first failure
        try:
            rsync()
        except tmt.utils.RunError:
            try:
                if self._check_rsync() == CheckRsyncOutcome.ALREADY_INSTALLED:
                    raise
                rsync()
            except tmt.utils.RunError:
                # Provide a reasonable error to the user
                self.fail(
                    f"Failed to pull workdir from the guest. "
                    f"This usually means that login as '{self.user}' "
                    f"to the guest does not work.")
                raise

    def stop(self) -> None:
        """
        Stop the guest

        Shut down a running guest instance so that it does not consume
        any memory or cpu resources. If needed, perform any actions
        necessary to store the instance status to disk.
        """

        # Close the master ssh connection
        if self._ssh_master_process:
            self.debug("Close the master ssh connection.", level=3)
            try:
                self._ssh_master_process.terminate()
                self._ssh_master_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

        # Remove the ssh socket
        if self._ssh_socket_path and self._ssh_socket_path.exists():
            self.debug(
                f"Remove ssh socket '{self._ssh_socket_path}'.", level=3)
            try:
                self._ssh_socket_path.unlink()
            except OSError as error:
                self.debug(f"Failed to remove the socket: {error}", level=3)

    def reboot(
            self,
            hard: bool = False,
            command: Optional[Union[Command, ShellScript]] = None,
            timeout: Optional[int] = None,
            tick: float = tmt.utils.DEFAULT_WAIT_TICK,
            tick_increase: float = tmt.utils.DEFAULT_WAIT_TICK_INCREASE) -> bool:
        """
        Reboot the guest, return True if reconnect was successful

        Parameter 'hard' set to True means that guest should be
        rebooted by way which is not clean in sense that data can be
        lost. When set to False reboot should be done gracefully.

        Use the 'command' parameter to specify a custom reboot command
        instead of the default 'reboot'.
        """

        if hard:
            raise tmt.utils.ProvisionError(
                "Method does not support hard reboot.")

        command = command or Command("reboot")
        self.debug(f"Reboot using the command '{command}'.")

        re_boot_time = re.compile(r'btime\s+(\d+)')

        def get_boot_time() -> int:
            """ Reads btime from /proc/stat """
            stdout = self.execute(Command("cat", "/proc/stat")).stdout
            assert stdout

            match = re_boot_time.search(stdout)

            if match is None:
                raise tmt.utils.ProvisionError('Failed to retrieve boot time from guest')

            return int(match.group(1))

        current_boot_time = get_boot_time()

        try:
            self.execute(command)
        except tmt.utils.RunError as error:
            # Connection can be closed by the remote host even before the
            # reboot command is completed. Let's ignore such errors.
            if error.returncode == 255:
                self.debug(
                    "Seems the connection was closed too fast, ignoring.")
            else:
                raise

        # Wait until we get new boot time, connection will drop and will be
        # unreachable for some time
        def check_boot_time() -> None:
            try:
                new_boot_time = get_boot_time()

                if new_boot_time != current_boot_time:
                    # Different boot time and we are reconnected
                    return

                # Same boot time, reboot didn't happen yet, retrying
                raise tmt.utils.WaitingIncomplete()

            except tmt.utils.RunError:
                self.debug('Failed to connect to the guest.')
                raise tmt.utils.WaitingIncomplete()

        timeout = timeout or CONNECTION_TIMEOUT

        try:
            tmt.utils.wait(
                self,
                check_boot_time,
                datetime.timedelta(seconds=timeout),
                tick=tick,
                tick_increase=tick_increase)

        except tmt.utils.WaitingTimedOutError:
            self.debug("Connection to guest failed after reboot.")
            return False

        self.debug("Connection to guest succeeded after reboot.")
        return True

    def remove(self) -> None:
        """
        Remove the guest

        Completely remove all guest instance data so that it does not
        consume any disk resources.
        """
        self.debug(f"Doing nothing to remove guest '{self.guest}'.")

    def _check_rsync(self) -> CheckRsyncOutcome:
        """
        Make sure that rsync is installed on the guest

        On read-only distros install it under the '/root/pkg' directory.
        Returns 'already installed' when rsync is already present.
        """

        # Check for rsync (nothing to do if already installed)
        self.debug("Ensure that rsync is installed on the guest.")
        try:
            self.execute(Command('rsync', '--version'))
            return CheckRsyncOutcome.ALREADY_INSTALLED
        except tmt.utils.RunError:
            pass

        # Check the package manager
        self.debug("Check the package manager.")
        try:
            self.execute(Command('dnf', '--version'))
            package_manager = "dnf"
        except tmt.utils.RunError:
            package_manager = "yum"

        # Install under '/root/pkg' for read-only distros
        # (for now the check is based on 'rpm-ostree' presence)
        # FIXME: Find a better way how to detect read-only distros
        self.debug("Check for a read-only distro.")
        try:
            self.execute(Command('rpm-ostree', '--version'))
            readonly = (
                " --installroot=/root/pkg --releasever / "
                "&& ln -sf /root/pkg/bin/rsync /usr/local/bin/rsync")
        except tmt.utils.RunError:
            readonly = ""

        # Install the rsync
        self.execute(ShellScript(f"{package_manager} install -y rsync" + readonly))

        return CheckRsyncOutcome.INSTALLED


@dataclasses.dataclass
class ProvisionStepData(tmt.steps.StepData):
    pass


class ProvisionPlugin(tmt.steps.GuestlessPlugin):
    """ Common parent of provision plugins """

    _data_class = ProvisionStepData
    _guest_class = Guest

    # Default implementation for provision is a virtual machine
    how = 'virtual'

    # List of all supported methods aggregated from all plugins of the same step.
    _supported_methods: List[tmt.steps.Method] = []

    # TODO: Generics would provide a better type, https://github.com/teemtee/tmt/issues/1437
    _guest: Optional[Guest] = None

    @classmethod
    def base_command(
            cls,
            usage: str,
            method_class: Optional[Type[click.Command]] = None) -> click.Command:
        """ Create base click command (common for all provision plugins) """

        # Prepare general usage message for the step
        if method_class:
            usage = Provision.usage(method_overview=usage)

        # Create the command
        @click.command(cls=method_class, help=usage)
        @click.pass_context
        @click.option(
            '-h', '--how', metavar='METHOD',
            help='Use specified method for provisioning.')
        def provision(context: 'tmt.cli.Context', **kwargs: Any) -> None:
            context.obj.steps.add('provision')
            Provision._save_context(context)

        return provision

    # TODO: this might be needed until https://github.com/teemtee/tmt/issues/1696 is resolved
    def opt(self, option: str, default: Optional[Any] = None) -> Any:
        """ Get an option from the command line options """

        if option == 'ssh-option':
            value = super().opt(option, default=default)

            if isinstance(value, tuple):
                return list(value)

            return value

        return super().opt(option, default=default)

    def wake(self, data: Optional[GuestData] = None) -> None:
        """
        Wake up the plugin

        Override data with command line options.
        Wake up the guest based on provided guest data.
        """
        super().wake()

        if data is not None:
            guest = self._guest_class(
                logger=self._logger,
                data=data,
                name=self.name,
                parent=self.step)
            guest.wake()
            self._guest = guest

    def guest(self) -> Optional[Guest]:
        """
        Return provisioned guest

        Each ProvisionPlugin has to implement this method.
        Should return a provisioned Guest() instance.
        """
        raise NotImplementedError()

    def requires(self) -> List['tmt.base.Require']:
        """
        All requirements of the guest implementation.

        Provide a list of requirements for the workdir sync.

        By default, plugin's guest class, :py:attr:`ProvisionPlugin._guest_class`,
        is asked to provide the list of required packages via
        :py:meth:`Guest.requires` method.

        :returns: a list of requirements.
        """

        return self._guest_class.requires()

    @classmethod
    def options(cls, how: Optional[str] = None) -> List[tmt.options.ClickOptionDecoratorType]:
        """ Return list of options. """
        return super().options(how) + cls._guest_class.options(how)

    @classmethod
    def clean_images(cls, clean: 'tmt.base.Clean', dry: bool) -> bool:
        """ Remove the images of one particular plugin """
        return True


class Provision(tmt.steps.Step):
    """ Provision an environment for testing or use localhost. """

    # Default implementation for provision is a virtual machine
    DEFAULT_HOW = 'virtual'

    _plugin_base_class = ProvisionPlugin

    _preserved_files = ['step.yaml', 'guests.yaml']

    def __init__(
            self,
            *,
            plan: 'tmt.Plan',
            data: tmt.steps.RawStepDataArgument,
            logger: tmt.log.Logger) -> None:
        """ Initialize provision step data """
        super().__init__(plan=plan, data=data, logger=logger)

        # List of provisioned guests and loaded guest data
        self._guests: List[Guest] = []
        self._guest_data: Dict[str, GuestData] = {}
        self.is_multihost = False

    def _normalize_data(
            self,
            raw_data: List[tmt.steps._RawStepData],
            logger: tmt.log.Logger) -> List[tmt.steps.StepData]:
        data = super()._normalize_data(raw_data, logger)

        # Check that the names are unique
        names = [datum.name for datum in data]
        names_count = collections.Counter(names)
        if len(data) > 1 and names_count.most_common(1)[0][1] > 1:
            duplicate_names = [name for name in names_count.elements() if names_count[name] > 1]
            duplicate_string = ', '.join(duplicate_names)
            raise tmt.utils.GeneralError(
                f"Provision step names must be unique for multihost testing. "
                f"Duplicate names: '{duplicate_string}' in plan '{self.plan.name}'.")

        return data

    def load(self) -> None:
        """ Load guest data from the workdir """
        super().load()
        try:
            raw_guest_data = tmt.utils.yaml_to_dict(self.read(Path('guests.yaml')))

            self._guest_data = {
                name: tmt.utils.SerializableContainer.unserialize(guest_data)
                for name, guest_data in raw_guest_data.items()
                }

        except tmt.utils.FileError:
            self.debug('Provisioned guests not found.', level=2)

    def save(self) -> None:
        """ Save guest data to the workdir """
        super().save()
        try:
            raw_guest_data = {guest.name: guest.save().to_serialized()
                              for guest in self.guests()}

            self.write(Path('guests.yaml'), tmt.utils.dict_to_yaml(raw_guest_data))
        except tmt.utils.FileError:
            self.debug('Failed to save provisioned guests.')

    def wake(self) -> None:
        """ Wake up the step (process workdir and command line) """
        super().wake()

        # Choose the right plugin and wake it up
        for data in self.data:
            # FIXME: cast() - see https://github.com/teemtee/tmt/issues/1599
            plugin = cast(ProvisionPlugin, ProvisionPlugin.delegate(self, data=data))
            self._phases.append(plugin)
            # If guest data loaded, perform a complete wake up
            plugin.wake(data=self._guest_data.get(plugin.name))

            guest = plugin.guest()
            if guest:
                self._guests.append(guest)

        # Nothing more to do if already done
        if self.status() == 'done':
            self.debug(
                'Provision wake up complete (already done before).', level=2)
        # Save status and step data (now we know what to do)
        else:
            self.status('todo')
            self.save()

    def summary(self) -> None:
        """ Give a concise summary of the provisioning """
        # Summary of provisioned guests
        guests = fmf.utils.listed(self.guests(), 'guest')
        self.info('summary', f'{guests} provisioned', 'green', shift=1)
        # Guest list in verbose mode
        for guest in self.guests():
            if not guest.name.startswith(tmt.utils.DEFAULT_NAME):
                self.verbose(guest.name, color='red', shift=2)

    def go(self) -> None:
        """ Provision all guests"""
        super().go()

        # Nothing more to do if already done
        if self.status() == 'done':
            self.info('status', 'done', 'green', shift=1)
            self.summary()
            self.actions()
            return

        # Provision guests
        self._guests = []
        save = True
        self.is_multihost = sum(isinstance(phase, ProvisionPlugin) for phase in self.phases()) > 1
        try:
            for phase in self.phases(classes=(Action, ProvisionPlugin)):
                try:
                    if isinstance(phase, Action):
                        phase.go()

                    elif isinstance(phase, ProvisionPlugin):
                        phase.go()

                        guest = phase.guest()
                        if guest:
                            guest.details()

                    if self.is_multihost:
                        self.info('')
                except (tmt.utils.RunError, tmt.utils.ProvisionError) as error:
                    self.fail(str(error))
                    raise
                finally:
                    if isinstance(phase, ProvisionPlugin):
                        guest = phase.guest()
                        if guest and (guest.is_ready or self.opt('dry')):
                            self._guests.append(guest)

            # Give a summary, update status and save
            self.summary()
            self.status('done')
        except (SystemExit, tmt.utils.SpecificationError) as error:
            # A plugin will only raise SystemExit if the exit is really desired
            # and no other actions should be done. An example of this is
            # listing available images. In such case, the workdir is deleted
            # as it's redundant and save() would throw an error.
            save = False
            raise error
        finally:
            if save:
                self.save()

    def guests(self) -> List[Guest]:
        """ Return the list of all provisioned guests """
        return self._guests
