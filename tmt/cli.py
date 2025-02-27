# coding: utf-8

""" Command line interface for the Test Management Tool """

import collections
import dataclasses
import subprocess
import sys
from typing import TYPE_CHECKING, Any, DefaultDict, List, Optional, Set

import click
import fmf
from click import echo, style
from fmf.utils import listed

import tmt
import tmt.base
import tmt.convert
import tmt.export
import tmt.identifier
import tmt.log
import tmt.options
import tmt.plugins
import tmt.steps
import tmt.templates
import tmt.utils
from tmt.options import create_options_decorator
from tmt.utils import Path

if TYPE_CHECKING:
    import tmt.steps.discover
    import tmt.steps.execute

# Explore available plugins (need to detect all supported methods first)
tmt.plugins.explore()

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Click Context Object Container
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@dataclasses.dataclass
class ContextObject:
    """
    Click Context Object container.

    In Click terms, this is "an arbitrary object of user data." In this container,
    tmt CLI code stores all structures relevant for the command execution. The
    container itself is then attached to :py:class:`click.Context` object Click
    manages across commands.
    """

    logger: tmt.log.Logger
    common: tmt.utils.Common
    fmf_context: tmt.utils.FmfContextType
    tree: tmt.Tree
    steps: Set[str] = dataclasses.field(default_factory=set)
    clean: Optional[tmt.Clean] = None
    clean_logger: Optional[tmt.log.Logger] = None
    clean_partials: DefaultDict[str, List[tmt.base.CleanCallback]] = dataclasses.field(
        default_factory=lambda: collections.defaultdict(list))
    run: Optional[tmt.Run] = None


class Context(click.Context):
    """
    Custom :py:class:`click.Context`-like class for typing purposes.

    Objects of this class are never instantiated, it serves only as a type
    stub in commands below, to simplify handling and static analysis of
    ``context.obj``. There is no added functionality, the only change is
    a much narrower type of ``obj`` attribute.

    This class shall be used instead of the original :py:class:`click.Context`.
    Click is obviously not aware of our type annotations, and ``context``
    objects managed by Click would always be of type :py:class:`click.Context`,
    we would just convince mypy their ``obj`` attribute is no longer ``Any``.
    """

    # In contrast to the original Context, we *know* we do set obj to a valid
    # object, and every time we touch it, it should absolutely be not-None.
    obj: ContextObject


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Custom Group
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class CustomGroup(click.Group):
    """ Custom Click Group """

    # ignore[override]: expected, we want to use more specific `Context`
    # type than the one declared in superclass.
    def list_commands(self, context: Context) -> List[str]:  # type: ignore[override]
        """ Prevent alphabetical sorting """
        return list(self.commands.keys())

    # ignore[override]: expected, we want to use more specific `Context`
    # type than the one declared in superclass.
    def get_command(  # type: ignore[override]
            self,
            context: Context,
            cmd_name: str
            ) -> Optional[click.Command]:
        """ Allow command shortening """
        # Backward-compatible 'test convert' (just temporary for now FIXME)
        cmd_name = cmd_name.replace('convert', 'import')
        # Support both story & stories
        cmd_name = cmd_name.replace('story', 'stories')
        found = click.Group.get_command(self, context, cmd_name)
        if found is not None:
            return found
        matches = [command for command in self.list_commands(context)
                   if command.startswith(cmd_name)]
        if not matches:
            return None
        elif len(matches) == 1:
            return click.Group.get_command(self, context, matches[0])
        context.fail('Did you mean {}?'.format(
            listed(sorted(matches), join='or')))
        return None


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Common Options
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
verbosity_options = create_options_decorator(tmt.options.VERBOSITY_OPTIONS)
dry_options = create_options_decorator(tmt.options.DRY_OPTIONS)
force_dry_options = create_options_decorator(tmt.options.FORCE_DRY_OPTIONS)
fix_options = create_options_decorator(tmt.options.FIX_OPTIONS)
workdir_root_options = create_options_decorator(tmt.options.WORKDIR_ROOT_OPTIONS)
filter_options = create_options_decorator(tmt.options.FILTER_OPTIONS)
filter_options_long = create_options_decorator(tmt.options.FILTER_OPTIONS_LONG)
fmf_source_options = create_options_decorator(tmt.options.FMF_SOURCE_OPTIONS)
story_flags_filter_options = create_options_decorator(tmt.options.STORY_FLAGS_FILTER_OPTIONS)
remote_plan_options = create_options_decorator(tmt.options.REMOTE_PLAN_OPTIONS)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Main
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
@click.group(invoke_without_command=True, cls=CustomGroup)
@click.pass_context
@click.option(
    '-r', '--root', metavar='PATH', show_default=True, default='.',
    help="Path to the tree root, '.' by default.")
@click.option(
    '-c', '--context', metavar='DATA', multiple=True,
    help='Set the fmf context. Use KEY=VAL or KEY=VAL1,VAL2... format '
         'to define individual dimensions or the @FILE notation to load data '
         'from provided yaml file. Can be specified multiple times. ')
@verbosity_options
@click.option(
    '--version', is_flag=True,
    help='Show tmt version and commit hash.')
@click.option(
    '--no-color', is_flag=True, default=False,
    help='Forces tmt to not use any colors in the output or logging.'
    )
@click.option(
    '--force-color', is_flag=True, default=False,
    help='Forces tmt to use colors in the output and logging.'
    )
def main(
        click_contex: Context,
        root: str,
        context: List[str],
        no_color: bool,
        force_color: bool,
        **kwargs: Any) -> None:
    """ Test Management Tool """
    # Show current tmt version and exit
    if kwargs.get('version'):
        print(f"tmt version: {tmt.__version__}")
        raise SystemExit(0)

    apply_colors_output, apply_colors_logging = tmt.log.decide_colorization(no_color, force_color)

    logger = tmt.log.Logger.create(**kwargs)
    logger.add_console_handler(apply_colors=apply_colors_logging)

    # Propagate color setting to Click as well.
    click_contex.color = apply_colors_output

    # Save click context and fmf context for future use
    tmt.utils.Common._save_context(click_contex)

    # Initialize metadata tree (from given path or current directory)
    tree = tmt.Tree(logger=logger, path=Path(root))

    # TODO: context object details need checks
    click_contex.obj = ContextObject(
        logger=logger,
        common=tmt.utils.Common(logger=logger),
        fmf_context=tmt.utils.context_to_dict(context=context, logger=logger),
        steps=set(),
        tree=tree
        )

    # Show overview of available tests, plans and stories
    if click_contex.invoked_subcommand is None:
        tmt.Test.overview(tree)
        tmt.Plan.overview(tree)
        tmt.Story.overview(tree)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Run
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

@main.group(chain=True, invoke_without_command=True, cls=CustomGroup)
@click.pass_context
@click.option(
    '-i', '--id', 'id_', help='Run id (name or directory path).', metavar="ID")
@click.option(
    '-l', '--last', help='Execute the last run once again.', is_flag=True)
@click.option(
    '-r', '--rm', '--remove', 'remove', is_flag=True,
    help='Remove the workdir when test run is finished.')
@click.option(
    '-k', '--keep', is_flag=True,
    help='Keep all files in the run workdir after testing is done '
         '(skip pruning during the finish step).')
@click.option(
    '--scratch', is_flag=True,
    help='Remove the run workdir before executing to start from scratch.')
@click.option(
    '--follow', is_flag=True,
    help='Output the logfile as it grows.')
@click.option(
    '-a', '--all', help='Run all steps, customize some.', is_flag=True)
@click.option(
    '-u', '--until', type=click.Choice(tmt.steps.STEPS), metavar='STEP',
    help='Enable given step and all preceding steps.')
@click.option(
    '-s', '--since', type=click.Choice(tmt.steps.STEPS), metavar='STEP',
    help='Enable given step and all following steps.')
@click.option(
    '-A', '--after', type=click.Choice(tmt.steps.STEPS), metavar='STEP',
    help='Enable all steps after the given one.')
@click.option(
    '-B', '--before', type=click.Choice(tmt.steps.STEPS), metavar='STEP',
    help='Enable all steps before the given one.')
@click.option(
    '-S', '--skip', type=click.Choice(tmt.steps.STEPS), metavar='STEP',
    help='Skip given step(s) during test run execution.', multiple=True)
@click.option(
    '-e', '--environment', metavar='KEY=VALUE|@FILE', multiple=True,
    help='Set environment variable. Can be specified multiple times. The '
         '"@" prefix marks a file to load (yaml or dotenv formats supported).')
@click.option(
    '--environment-file', metavar='FILE|URL', multiple=True,
    help='Set environment variables from file or url (yaml or dotenv formats '
         'are supported). Can be specified multiple times.')
@verbosity_options
@force_dry_options
def run(context: Context, id_: Optional[str], **kwargs: Any) -> None:
    """ Run test steps. """
    # Initialize
    logger = context.obj.logger.descend(logger_name='run', extra_shift=0)
    logger.apply_verbosity_options(**kwargs)

    run = tmt.Run(
        id_=Path(id_) if id_ is not None else None,
        tree=context.obj.tree,
        context=context,
        logger=logger
        )
    context.obj.run = run


# Steps options
run.add_command(tmt.steps.discover.DiscoverPlugin.command())
run.add_command(tmt.steps.provision.ProvisionPlugin.command())
run.add_command(tmt.steps.prepare.PreparePlugin.command())
run.add_command(tmt.steps.execute.ExecutePlugin.command())
run.add_command(tmt.steps.report.ReportPlugin.command())
run.add_command(tmt.steps.finish.FinishPlugin.command())
run.add_command(tmt.steps.Login.command())
run.add_command(tmt.steps.Reboot.command())


@run.command(name='plans')
@click.pass_context
@click.option(
    '-n', '--name', 'names', metavar='[REGEXP|.]', multiple=True,
    help="Regular expression to match plan name or '.' for current directory.")
@click.option(
    '-f', '--filter', 'filters', metavar='FILTER', multiple=True,
    help="Apply advanced filter (see 'pydoc fmf.filter').")
@click.option(
    '-c', '--condition', 'conditions', metavar="EXPR", multiple=True,
    help="Use arbitrary Python expression for filtering.")
@click.option(
    '--link', 'links', metavar="RELATION:TARGET", multiple=True,
    help="Filter by linked objects (regular expressions are "
         "supported for both relation and target).")
@click.option(
    '--default', is_flag=True,
    help="Use default plans even if others are available.")
@verbosity_options
def run_plans(context: Context, **kwargs: Any) -> None:
    """
    Select plans which should be executed.

    Regular expression can be used to filter plans by name.
    Use '.' to select plans under the current working directory.
    """
    tmt.base.Plan._save_context(context)


@run.command(name='tests')
@click.pass_context
@click.option(
    '-n', '--name', 'names', metavar='[REGEXP|.]', multiple=True,
    help="Regular expression to match test name or '.' for current directory.")
@click.option(
    '-f', '--filter', 'filters', metavar='FILTER', multiple=True,
    help="Apply advanced filter (see 'pydoc fmf.filter').")
@click.option(
    '-c', '--condition', 'conditions', metavar="EXPR", multiple=True,
    help="Use arbitrary Python expression for filtering.")
@click.option(
    '--link', 'links', metavar="RELATION:TARGET", multiple=True,
    help="Filter by linked objects (regular expressions are "
         "supported for both relation and target).")
@verbosity_options
def run_tests(context: Context, **kwargs: Any) -> None:
    """
    Select tests which should be executed.

    Regular expression can be used to filter tests by name.
    Use '.' to select tests under the current working directory.
    """
    tmt.base.Test._save_context(context)


# FIXME: click 8.0 renamed resultcallback to result_callback. The former
#        name will be removed in click 8.1. However, click 8.0 will not
#        be added to F33 and F34. Get rid of this workaround once
#        all Fedora + EPEL releases have click 8.0 or newer available.
run_callback = run.result_callback
if run_callback is None:
    run_callback = run.resultcallback


# TODO: commands is unknown, needs revisit
@run_callback()
@click.pass_context
def finito(
        click_context: Context,
        commands: Any,
        *args: Any,
        **kwargs: Any) -> None:
    """ Run tests if run defined """
    if isinstance(click_context.obj, ContextObject) and click_context.obj.run:
        click_context.obj.run.go()


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Test
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

@main.group(invoke_without_command=True, cls=CustomGroup)
@click.pass_context
@verbosity_options
def tests(context: Context, **kwargs: Any) -> None:
    """
    Manage tests (L1 metadata).

    Check available tests, inspect their metadata.
    Convert old metadata into the new fmf format.
    """

    # Show overview of available tests
    if context.invoked_subcommand is None:
        tmt.Test.overview(context.obj.tree)


@tests.command(name='ls')
@click.pass_context
@filter_options
@verbosity_options
def tests_ls(context: Context, **kwargs: Any) -> None:
    """
    List available tests.

    Regular expression can be used to filter tests by name.
    Use '.' to select tests under the current working directory.
    """
    tmt.Test._save_context(context)
    for test in context.obj.tree.tests():
        test.ls()


@tests.command(name='show')
@click.pass_context
@filter_options
@verbosity_options
def tests_show(context: Context, **kwargs: Any) -> None:
    """
    Show test details.

    Regular expression can be used to filter tests by name.
    Use '.' to select tests under the current working directory.
    """
    tmt.Test._save_context(context)
    for test in context.obj.tree.tests():
        test.show()
        echo()


@tests.command(name='lint')
@click.pass_context
@filter_options
@fmf_source_options
@fix_options
@verbosity_options
def tests_lint(context: Context, **kwargs: Any) -> None:
    """
    Check tests against the L1 metadata specification.

    Regular expression can be used to filter tests for linting.
    Use '.' to select tests under the current working directory.
    """
    # FIXME: Workaround https://github.com/pallets/click/pull/1840 for click 7
    context.params.update(**kwargs)
    tmt.Test._save_context(context)
    exit_code = 0
    for test in context.obj.tree.tests():
        if not test.lint():
            exit_code = 1
        echo()
    raise SystemExit(exit_code)


_test_templates = listed(tmt.templates.TEST, join='or')


@tests.command(name='create')
@click.pass_context
@click.argument('name')
@click.option(
    '-t', '--template', metavar='TEMPLATE',
    help='Test template ({}).'.format(_test_templates),
    prompt='Template ({})'.format(_test_templates))
@verbosity_options
@force_dry_options
def tests_create(
        context: Context,
        name: str,
        template: str,
        force: bool,
        **kwargs: Any) -> None:
    """
    Create a new test based on given template.

    Specify directory name or use '.' to create tests under the
    current working directory.
    """
    assert context.obj.tree.root is not None  # narrow type
    tmt.Test._save_context(context)
    tmt.Test.create(name, template, context.obj.tree.root, force)


@tests.command(name='import')
@click.pass_context
@click.argument('paths', nargs=-1, metavar='[PATH]...')
@click.option(
    '--nitrate / --no-nitrate', default=True,
    help='Import test metadata from Nitrate.')
@click.option(
    '--polarion / --no-polarion', default=False,
    help='Import test metadata from Polarion.')
@click.option(
    '--purpose / --no-purpose', default=True,
    help='Migrate description from PURPOSE file.')
@click.option(
    '--makefile / --no-makefile', default=True,
    help='Convert Beaker Makefile metadata.')
@click.option(
    '--restraint / --no-restraint', default=False,
    help='Convert restraint metadata file.')
@click.option(
    '--general / --no-general', default=True,
    help='Detect components from linked nitrate general plans '
         '(overrides Makefile/restraint component).')
@click.option(
    '--polarion-case-id',
    help='Polarion Test case ID to import data from.')
@click.option(
    '--link-polarion / --no-link-polarion', default=True,
    help='Add Polarion link to fmf testcase metadata.')
@click.option(
    '--type', 'types', metavar='TYPE', default=['multihost'], multiple=True,
    show_default=True,
    help="Convert selected types from Makefile into tags. "
         "Use 'all' to convert all detected types.")
@click.option(
    '--disabled', default=False, is_flag=True,
    help='Import disabled test cases from Nitrate as well.')
@click.option(
    '--manual', default=False, is_flag=True,
    help='Import manual test cases from Nitrate.')
@click.option(
    '--plan', metavar='PLAN', type=int,
    help='Identifier of test plan from which to import manual test cases.')
@click.option(
    '--case', metavar='CASE', type=int,
    help='Identifier of manual test case to be imported.')
@click.option(
    '--with-script', default=False, is_flag=True,
    help='Import manual cases with non-empty script field in Nitrate.')
@verbosity_options
@force_dry_options
def tests_import(
        context: Context,
        paths: List[str],
        makefile: bool,
        restraint: bool,
        general: bool,
        types: List[str],
        nitrate: bool,
        polarion: bool,
        polarion_case_id: Optional[str],
        link_polarion: bool,
        purpose: bool,
        disabled: bool,
        manual: bool,
        plan: int,
        case: int,
        with_script: bool,
        **kwargs: Any) -> None:
    """
    Import old test metadata into the new fmf format.

    Accepts one or more directories where old metadata are stored.
    By default all available sources and current directory are used.
    The following test metadata are converted for each source:

    \b
    makefile ..... summary, component, duration, require
    restraint .... name, description, entry_point, owner, max_time, repoRequires
    purpose ...... description
    nitrate ...... contact, component, tag, environment, relevancy, enabled
    polarion ..... summary, enabled, assignee, id, component, tag, description, link
    """
    tmt.Test._save_context(context)

    if manual:
        if not (case or plan):
            raise tmt.utils.GeneralError(
                "Option --case or --plan is mandatory when using --manual.")
        else:
            tmt.convert.read_manual(plan, case, disabled, with_script)
            return

    if not paths:
        paths = ['.']
    for _path in paths:
        path = Path(_path)
        # Make sure we've got a real directory
        path = path.resolve()
        if not path.is_dir():
            raise tmt.utils.GeneralError(
                "Path '{0}' is not a directory.".format(path))
        # Gather old metadata and store them as fmf
        common, individual = tmt.convert.read(
            path, makefile, restraint, nitrate, polarion, polarion_case_id, link_polarion,
            purpose, disabled, types, general)
        # Add path to common metadata if there are virtual test cases
        if individual:
            root = Path(fmf.Tree(str(path)).root)
            common['path'] = str(Path('/') / path.relative_to(root))
        # Store common metadata
        common_path = path / 'main.fmf'
        tmt.convert.write(common_path, common)
        # Store individual data (as virtual tests)
        for testcase in individual:
            testcase_path = path / f'{testcase["extra-nitrate"]}.fmf'
            tmt.convert.write(testcase_path, testcase)
        # Adjust runtest.sh content and permission if needed
        tmt.convert.adjust_runtest(path / 'runtest.sh')


_test_export_formats = list(tmt.Test.get_export_plugin_registry().keys())
_test_export_default = 'yaml'


@tests.command(name='export')
@click.pass_context
@filter_options_long
@click.option(
    '-h', '--how', metavar='METHOD', default=_test_export_default, show_default=True,
    help='Output format.',
    type=click.Choice(choices=_test_export_formats))
@click.option(
    '--format', metavar='FORMAT', default=_test_export_default, show_default=True,
    help='Output format. Deprecated, use --how instead.',
    type=click.Choice(choices=_test_export_formats))
@click.option(
    '--nitrate', is_flag=True,
    help="Export test metadata to Nitrate, deprecated by '--how nitrate'.")
@click.option(
    '--project-id', help='Use specific Polarion project ID.')
@click.option(
    '--link-polarion / --no-link-polarion', default=True,
    help='Add Polarion link to fmf testcase metadata')
@click.option(
    '--bugzilla', is_flag=True,
    help="Link Nitrate case to Bugzilla specified in the 'link' attribute "
         "with the relation 'verifies'.")
@click.option(
    '--ignore-git-validation', is_flag=True,
    help="Ignore unpublished git changes and export to Nitrate. "
    "The case might not be able to be scheduled!")
@click.option(
    '--create', is_flag=True,
    help="Create test cases in nitrate if they don't exist.")
@click.option(
    '--general / --no-general', default=False,
    help="Link Nitrate case to component's General plan. Disabled by default. "
         "Note that this will unlink any previously connected general plans.")
@click.option(
    '--link-runs / --no-link-runs', default=False,
    help="Link Nitrate case to all open runs of descendant plans of "
         "General plan. Disabled by default. Implies --general option.")
@click.option(
    '--fmf-id', is_flag=True,
    help='Show fmf identifiers instead of test metadata.')
@click.option(
    '--duplicate / --no-duplicate', default=False, show_default=True,
    help='Allow or prevent creating duplicates in Nitrate by searching for '
         'existing test cases with the same fmf identifier.')
@click.option(
    '-n', '--dry', is_flag=True,
    help="Run in dry mode. No changes, please.")
@click.option(
    '-d', '--debug', is_flag=True,
    help='Provide as much debugging details as possible.')
# TODO: move to `template` export plugin options
@click.option(
    '--template', metavar='PATH',
    help="Path to a template to use for rendering the export. Used with '--how=template' only."
    )
def tests_export(
        context: Context,
        format: str,
        how: str,
        nitrate: bool,
        bugzilla: bool,
        template: Optional[str],
        **kwargs: Any) -> None:
    """
    Export test data into the desired format.

    Regular expression can be used to filter tests by name.
    Use '.' to select tests under the current working directory.
    """
    tmt.Test._save_context(context)

    if nitrate:
        context.obj.common.warn(
            "Option '--nitrate' is deprecated, please use '--how nitrate' instead.")
        how = 'nitrate'

    if format != _test_export_default:
        context.obj.common.warn("Option '--format' is deprecated, please use '--how' instead.")

        how = format

    # TODO: move this "requires bugzilla" flag to export plugin level.
    if bugzilla and how not in ('nitrate', 'polarion'):
        raise tmt.utils.GeneralError(
            "The --bugzilla option is supported only with --nitrate "
            "or --polarion for now.")

    if kwargs.get('fmf_id'):
        echo(tmt.base.FmfId.export_collection(
            collection=[test.fmf_id for test in context.obj.tree.tests()],
            format=how,
            template=Path(template) if template else None
            ))

    else:
        echo(tmt.Test.export_collection(
            collection=context.obj.tree.tests(),
            format=how,
            template=Path(template) if template else None
            ))


@tests.command(name="id")
@click.pass_context
@filter_options
@verbosity_options
@force_dry_options
def tests_id(context: Context, **kwargs: Any) -> None:
    """
    Generate a unique id for each selected test.

    A new UUID is generated for each test matching the provided
    filter and the value is stored to disk. Existing identifiers
    are kept intact.
    """
    tmt.Test._save_context(context)
    for test in context.obj.tree.tests():
        tmt.identifier.id_command(test.node, "test", dry=kwargs["dry"])


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Plan
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

@main.group(invoke_without_command=True, cls=CustomGroup)
@click.pass_context
@verbosity_options
@remote_plan_options
def plans(context: Context, **kwargs: Any) -> None:
    """
    Manage test plans (L2 metadata).

    \b
    Search for available plans.
    Explore detailed test step configuration.
    """
    tmt.Plan._save_context(context)

    # Show overview of available plans
    if context.invoked_subcommand is None:
        tmt.Plan.overview(context.obj.tree)


@plans.command(name='ls')
@click.pass_context
@filter_options
@verbosity_options
@remote_plan_options
def plans_ls(context: Context, **kwargs: Any) -> None:
    """
    List available plans.

    Regular expression can be used to filter plans by name.
    Use '.' to select plans under the current working directory.
    """
    tmt.Plan._save_context(context)
    for plan in context.obj.tree.plans():
        plan.ls()


@plans.command(name='show')
@click.pass_context
@filter_options
@verbosity_options
@remote_plan_options
def plans_show(context: Context, **kwargs: Any) -> None:
    """
    Show plan details.

    Regular expression can be used to filter plans by name.
    Use '.' to select plans under the current working directory.
    """
    tmt.Plan._save_context(context)
    for plan in context.obj.tree.plans():
        plan.show()
        echo()


@plans.command(name='lint')
@click.pass_context
@filter_options
@fmf_source_options
@verbosity_options
def plans_lint(context: Context, **kwargs: Any) -> None:
    """
    Check plans against the L2 metadata specification.

    Regular expression can be used to filter plans by name.
    Use '.' to select plans under the current working directory.
    """
    # FIXME: Workaround https://github.com/pallets/click/pull/1840 for click 7
    context.params.update(**kwargs)
    tmt.Plan._save_context(context)
    exit_code = 0
    for plan in context.obj.tree.plans():
        if not plan.lint():
            exit_code = 1
        echo()
    raise SystemExit(exit_code)


_plan_templates = listed(tmt.templates.PLAN, join='or')


@plans.command(name='create')
@click.pass_context
@click.argument('name')
@click.option(
    '-t', '--template', metavar='TEMPLATE',
    help='Plan template ({}).'.format(_plan_templates),
    prompt='Template ({})'.format(_plan_templates))
@click.option(
    '--discover', metavar='YAML', multiple=True,
    help='Discover phase content in yaml format.')
@click.option(
    '--provision', metavar='YAML', multiple=True,
    help='Provision phase content in yaml format.')
@click.option(
    '--prepare', metavar='YAML', multiple=True,
    help='Prepare phase content in yaml format.')
@click.option(
    '--execute', metavar='YAML', multiple=True,
    help='Execute phase content in yaml format.')
@click.option(
    '--report', metavar='YAML', multiple=True,
    help='Report phase content in yaml format.')
@click.option(
    '--finish', metavar='YAML', multiple=True,
    help='Finish phase content in yaml format.')
@verbosity_options
@force_dry_options
def plans_create(
        context: Context,
        name: str,
        template: str,
        force: bool,
        **kwargs: Any) -> None:
    """ Create a new plan based on given template. """
    assert context.obj.tree.root is not None  # narrow type
    tmt.Plan._save_context(context)
    tmt.Plan.create(name, template, context.obj.tree.root, force)


_plan_export_formats = list(tmt.Plan.get_export_plugin_registry().keys())
_plan_export_default = 'yaml'


@plans.command(name='export')
@click.pass_context
@filter_options_long
@click.option(
    '-h', '--how', metavar='METHOD', default=_plan_export_default, show_default=True,
    help='Output format.',
    type=click.Choice(choices=_plan_export_formats))
@click.option(
    '--format', metavar='FORMAT', default=_plan_export_default, show_default=True,
    help='Output format. Deprecated, use --how instead.',
    type=click.Choice(choices=_plan_export_formats))
@click.option(
    '-d', '--debug', is_flag=True,
    help='Provide as much debugging details as possible.')
# TODO: move to `template` export plugin options
@click.option(
    '--template', metavar='PATH',
    help="Path to a template to use for rendering the export. Used with '--how=template' only."
    )
def plans_export(
        context: Context,
        how: str,
        format: str,
        template: Optional[str],
        **kwargs: Any) -> None:
    """
    Export plans into desired format.

    Regular expression can be used to filter plans by name.
    Use '.' to select plans under the current working directory.
    """
    tmt.Plan._save_context(context)

    if format != _test_export_default:
        context.obj.common.warn("Option '--format' is deprecated, please use '--how' instead.")

        how = format

    echo(tmt.Plan.export_collection(
        collection=context.obj.tree.plans(),
        format=how,
        template=Path(template) if template else None
        ))


@plans.command(name="id")
@click.pass_context
@filter_options
@verbosity_options
@force_dry_options
def plans_id(context: Context, **kwargs: Any) -> None:
    """
    Generate a unique id for each selected plan.

    A new UUID is generated for each plan matching the provided
    filter and the value is stored to disk. Existing identifiers
    are kept intact.
    """
    tmt.Plan._save_context(context)
    for plan in context.obj.tree.plans():
        tmt.identifier.id_command(plan.node, "plan", dry=kwargs["dry"])

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Story
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@main.group(invoke_without_command=True, cls=CustomGroup)
@click.pass_context
@verbosity_options
def stories(context: Context, **kwargs: Any) -> None:
    """
    Manage user stories.

    \b
    Check available user stories.
    Explore coverage (test, implementation, documentation).
    """
    tmt.Story._save_context(context)

    # Show overview of available stories
    if context.invoked_subcommand is None:
        tmt.Story.overview(context.obj.tree)


@stories.command(name='ls')
@click.pass_context
@filter_options_long
@story_flags_filter_options
@verbosity_options
def stories_ls(
        context: Context,
        implemented: bool,
        verified: bool,
        documented: bool,
        covered: bool,
        unimplemented: bool,
        unverified: bool,
        undocumented: bool,
        uncovered: bool,
        **kwargs: Any) -> None:
    """
    List available stories.

    Regular expression can be used to filter stories by name.
    Use '.' to select stories under the current working directory.
    """
    tmt.Story._save_context(context)
    for story in context.obj.tree.stories():
        if story._match(implemented, verified, documented, covered,
                        unimplemented, unverified, undocumented, uncovered):
            story.ls()


@stories.command(name='show')
@click.pass_context
@filter_options_long
@story_flags_filter_options
@verbosity_options
def stories_show(
        context: Context,
        implemented: bool,
        verified: bool,
        documented: bool,
        covered: bool,
        unimplemented: bool,
        unverified: bool,
        undocumented: bool,
        uncovered: bool,
        **kwargs: Any) -> None:
    """
    Show story details.

    Regular expression can be used to filter stories by name.
    Use '.' to select stories under the current working directory.
    """
    tmt.Story._save_context(context)
    for story in context.obj.tree.stories():
        if story._match(implemented, verified, documented, covered,
                        unimplemented, unverified, undocumented, uncovered):
            story.show()
            echo()


_story_templates = listed(tmt.templates.STORY, join='or')


@stories.command(name='create')
@click.pass_context
@click.argument('name')
@click.option(
    '-t', '--template', metavar='TEMPLATE',
    prompt='Template ({})'.format(_story_templates),
    help='Story template ({}).'.format(_story_templates))
@verbosity_options
@force_dry_options
def stories_create(
        context: Context,
        name: str,
        template: str,
        force: bool,
        **kwargs: Any) -> None:
    """ Create a new story based on given template. """
    assert context.obj.tree.root is not None  # narrow type
    tmt.Story._save_context(context)
    tmt.Story.create(name, template, context.obj.tree.root, force)


@stories.command(name='coverage')
@click.option(
    '--docs', is_flag=True, help='Show docs coverage.')
@click.option(
    '--test', is_flag=True, help='Show test coverage.')
@click.option(
    '--code', is_flag=True, help='Show code coverage.')
@click.pass_context
@filter_options_long
@story_flags_filter_options
@verbosity_options
def stories_coverage(
        context: Context,
        code: bool,
        test: bool,
        docs: bool,
        implemented: bool,
        verified: bool,
        documented: bool,
        covered: bool,
        unimplemented: bool,
        unverified: bool,
        undocumented: bool,
        uncovered: bool,
        **kwargs: Any) -> None:
    """
    Show code, test and docs coverage for given stories.

    Regular expression can be used to filter stories by name.
    Use '.' to select stories under the current working directory.
    """
    tmt.Story._save_context(context)

    def headfoot(text: str) -> None:
        """ Format simple header/footer """
        echo(style(text.rjust(4) + ' ', fg='blue'), nl=False)

    header = False
    total = code_coverage = test_coverage = docs_coverage = 0
    if not any([code, test, docs]):
        code = test = docs = True
    for story in context.obj.tree.stories():
        # Check conditions
        if not story._match(
                implemented, verified, documented, covered, unimplemented,
                unverified, undocumented, uncovered):
            continue
        # Show header once
        if not header:
            if code:
                headfoot('code')
            if test:
                headfoot('test')
            if docs:
                headfoot('docs')
            headfoot('story')
            echo()
            header = True
        # Show individual stats
        status = story.coverage(code, test, docs)
        total += 1
        code_coverage += status[0]
        test_coverage += status[1]
        docs_coverage += status[2]
    # Summary
    if not total:
        return
    if code:
        headfoot('{}%'.format(round(100 * code_coverage / total)))
    if test:
        headfoot('{}%'.format(round(100 * test_coverage / total)))
    if docs:
        headfoot('{}%'.format(round(100 * docs_coverage / total)))
    headfoot('from {}'.format(listed(total, 'story')))
    echo()


_story_export_formats = list(tmt.Story.get_export_plugin_registry().keys())
_story_export_default = 'yaml'


@stories.command(name='export')
@click.pass_context
@filter_options_long
@story_flags_filter_options
@click.option(
    '-h', '--how', metavar='METHOD', default=_story_export_default, show_default=True,
    help='Output format.',
    type=click.Choice(choices=_story_export_formats))
@click.option(
    '--format', metavar='FORMAT', default=_story_export_default, show_default=True,
    help='Output format. Deprecated, use --how instead.',
    type=click.Choice(choices=_story_export_formats))
@click.option(
    '-d', '--debug', is_flag=True,
    help='Provide as much debugging details as possible.')
# TODO: move to `template` export plugin options
@click.option(
    '--template', metavar='PATH',
    help="Path to a template to use for rendering the export. Used with '--how=template' only."
    )
def stories_export(
        context: Context,
        how: str,
        format: str,
        implemented: bool,
        verified: bool,
        documented: bool,
        covered: bool,
        unimplemented: bool,
        unverified: bool,
        undocumented: bool,
        uncovered: bool,
        template: Optional[str],
        **kwargs: Any) -> None:
    """
    Export selected stories into desired format.

    Regular expression can be used to filter stories by name.
    Use '.' to select stories under the current working directory.
    """
    tmt.Story._save_context(context)

    if format != _test_export_default:
        context.obj.common.warn("Option '--format' is deprecated, please use '--how' instead.")

        how = format

    stories = [
        story for story in context.obj.tree.stories(whole=True)
        if story._match(implemented, verified, documented, covered, unimplemented, unverified,
                        undocumented, uncovered)
        ]

    echo(tmt.Story.export_collection(
        collection=stories,
        format=how,
        template=Path(template) if template else None))


@stories.command(name='lint')
@click.pass_context
@filter_options
@fmf_source_options
@verbosity_options
def stories_lint(context: Context, **kwargs: Any) -> None:
    """
    Check stories against the L3 metadata specification.

    Regular expression can be used to filter stories by name.
    Use '.' to select stories under the current working directory.
    """
    # FIXME: Workaround https://github.com/pallets/click/pull/1840 for click 7
    context.params.update(**kwargs)
    tmt.Story._save_context(context)
    exit_code = 0
    for story in context.obj.tree.stories():
        if not story.lint():
            exit_code = 1
        echo()
    raise SystemExit(exit_code)


@stories.command(name="id")
@click.pass_context
@filter_options_long
@story_flags_filter_options
@verbosity_options
@force_dry_options
def stories_id(
        context: Context,
        implemented: bool,
        verified: bool,
        documented: bool,
        covered: bool,
        unimplemented: bool,
        unverified: bool,
        undocumented: bool,
        uncovered: bool,
        **kwargs: Any) -> None:
    """
    Generate a unique id for each selected story.

    A new UUID is generated for each story matching the provided
    filter and the value is stored to disk. Existing identifiers
    are kept intact.
    """
    tmt.Story._save_context(context)
    for story in context.obj.tree.stories():
        if story._match(implemented, verified, documented, covered,
                        unimplemented, unverified, undocumented, uncovered):
            tmt.identifier.id_command(story.node, "story", dry=kwargs["dry"])


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Init
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

@main.command()
@click.pass_context
@click.argument('path', default='.')
@click.option(
    '-t', '--template', default='empty', metavar='TEMPLATE',
    type=click.Choice(['empty'] + tmt.templates.INIT_TEMPLATES),
    help='Template ({}).'.format(
        listed(tmt.templates.INIT_TEMPLATES, join='or')))
@verbosity_options
@force_dry_options
def init(
        context: Context,
        path: str,
        template: str,
        force: bool,
        **kwargs: Any) -> None:
    """
    Initialize a new tmt tree.

    By default tree is created in the current directory.
    Provide a PATH to create it in a different location.

    \b
    A tree can be optionally populated with example metadata:
    * 'mini' template contains a minimal plan and no tests,
    * 'base' template contains a plan and a beakerlib test,
    * 'full' template contains a 'full' story, an 'full' plan and a shell test.
    """

    tmt.Tree._save_context(context)
    tmt.Tree.init(logger=context.obj.logger, path=Path(path), template=template, force=force)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Status
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

@main.command()
@click.pass_context
@workdir_root_options
@click.option(
    '-i', '--id', metavar="ID",
    help='Run id (name or directory path) to show status of.')
@click.option(
    '--abandoned', is_flag=True, default=False,
    help='List runs which have provision step completed but finish step '
         'not yet done.')
@click.option(
    '--active', is_flag=True, default=False,
    help='List runs where at least one of the enabled steps has not '
         'been finished.')
@click.option(
    '--finished', is_flag=True, default=False,
    help='List all runs which have all enabled steps completed.')
@verbosity_options
def status(
        context: Context,
        workdir_root: str,
        abandoned: bool,
        active: bool,
        finished: bool,
        **kwargs: Any) -> None:
    """
    Show status of runs.

    Lists past runs in the given directory filtered using options.
    /var/tmp/tmt is used by default.

    By default, status of the whole runs is listed. With more
    verbosity (-v), status of every plan is shown. By default,
    the last completed step is displayed, 'done' is used when
    all enabled steps are completed. Status of every step is
    displayed with the most verbosity (-vv).

    """
    if [abandoned, active, finished].count(True) > 1:
        raise tmt.utils.GeneralError(
            "Options --abandoned, --active and --finished cannot be "
            "used together.")
    if not Path(workdir_root).exists():
        raise tmt.utils.GeneralError(f"Path '{workdir_root}' doesn't exist.")
    status_obj = tmt.Status(logger=context.obj.logger, context=context)
    status_obj.show()


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Clean
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@main.group(chain=True, invoke_without_command=True, cls=CustomGroup)
@click.pass_context
@verbosity_options
@dry_options
def clean(context: Context, **kwargs: Any) -> None:
    """
    Clean workdirs, guests or images.

    Without any command, clean everything, stop the guests, remove
    all runs and then remove all images. Search for runs in
    /var/tmp/tmt, if runs are stored elsewhere, the path to them can
    be set using a subcommand (either runs or guests subcommand).

    The subcommands can be chained, the order of cleaning is always
    the same, irrespective of the order on the command line. First, all
    the guests are cleaned, followed by runs and images.
    """

    context.obj.clean_logger = context.obj.logger \
        .descend(logger_name='clean', extra_shift=0) \
        .apply_verbosity_options(**kwargs)

    echo(style('clean', fg='red'))
    clean_obj = tmt.Clean(
        logger=context.obj.clean_logger,
        parent=context.obj.common,
        context=context
        )
    context.obj.clean = clean_obj
    exit_code = 0
    if context.invoked_subcommand is None:
        assert context.obj.clean_logger is not None  # narrow type

        # Set path to default
        context.params['workdir_root'] = tmt.utils.WORKDIR_ROOT
        # Create another level to the hierarchy so that logging indent is
        # consistent between the command and subcommands
        clean_obj = tmt.Clean(
            logger=context.obj.clean_logger
            .descend(logger_name='clean-images', extra_shift=0)
            .apply_verbosity_options(**kwargs),
            parent=clean_obj,
            context=context
            )
        if tmt.utils.WORKDIR_ROOT.exists():
            if not clean_obj.guests():
                exit_code = 1
            if not clean_obj.runs():
                exit_code = 1
        else:
            clean_obj.warn(
                f"Directory '{tmt.utils.WORKDIR_ROOT}' does not exist, "
                f"skipping guest and run cleanup.")
        clean_obj.images()
        raise SystemExit(exit_code)


# FIXME: Click deprecation, see function finito for more info
clean_callback = clean.result_callback
if clean_callback is None:
    clean_callback = clean.resultcallback


@clean_callback()
@click.pass_context
def perform_clean(
        click_context: Context,
        commands: Any,
        *args: Any,
        **kwargs: Any) -> None:
    """
    Perform clean actions in the correct order.

    We need to ensure that guests are always cleaned before the run workdirs
    even if the user specified them in reverse order.
    """
    clean_order = ("guests", "runs", "images")
    exit_code = 0
    for phase in clean_order:
        for partial in click_context.obj.clean_partials[phase]:
            if not partial():
                exit_code = 1
    raise SystemExit(exit_code)


@clean.command(name='runs')
@click.pass_context
@workdir_root_options
@click.option(
    '-l', '--last', is_flag=True, help='Clean the workdir of the last run.')
@click.option(
    '-i', '--id', 'id_', metavar="ID",
    help='Run id (name or directory path) to clean workdir of.')
@click.option(
    '-k', '--keep', type=int,
    help='The number of latest workdirs to keep, clean the rest.')
@verbosity_options
@dry_options
def clean_runs(
        context: Context,
        workdir_root: str,
        last: bool,
        id_: str,
        keep: int,
        **kwargs: Any) -> None:
    """
    Clean workdirs of past runs.

    Remove all runs in '/var/tmp/tmt' by default.
    """
    defined = [last is True, id_ is not None, keep is not None]
    if defined.count(True) > 1:
        raise tmt.utils.GeneralError(
            "Options --last, --id and --keep cannot be used together.")
    if keep is not None and keep < 0:
        raise tmt.utils.GeneralError("--keep must not be a negative number.")
    if not Path(workdir_root).exists():
        raise tmt.utils.GeneralError(f"Path '{workdir_root}' doesn't exist.")

    assert context.obj.clean_logger is not None  # narrow type

    clean_obj = tmt.Clean(
        logger=context.obj.clean_logger
        .descend(logger_name='clean-runs', extra_shift=0)
        .apply_verbosity_options(**kwargs),
        parent=context.obj.clean,
        context=context)
    context.obj.clean_partials["runs"].append(clean_obj.runs)


@clean.command(name='guests')
@click.pass_context
@workdir_root_options
@click.option(
    '-l', '--last', is_flag=True, help='Stop the guest of the last run.')
@click.option(
    '-i', '--id', 'id_', metavar="ID",
    help='Run id (name or directory path) to stop the guest of.')
@click.option(
    '-h', '--how', metavar='METHOD',
    help='Stop guests of the specified provision method.')
@verbosity_options
@dry_options
def clean_guests(
        context: Context,
        workdir_root: str,
        last: bool,
        id_: int,
        **kwargs: Any) -> None:
    """
    Stop running guests of runs.

    Stop guests of all runs in '/var/tmp/tmt' by default.
    """
    if last and id_ is not None:
        raise tmt.utils.GeneralError(
            "Options --last and --id cannot be used together.")
    if not Path(workdir_root).exists():
        raise tmt.utils.GeneralError(f"Path '{workdir_root}' doesn't exist.")

    assert context.obj.clean_logger is not None  # narrow type

    clean_obj = tmt.Clean(
        logger=context.obj.clean_logger
        .descend(logger_name='clean-guests', extra_shift=0)
        .apply_verbosity_options(**kwargs),
        parent=context.obj.clean,
        context=context
        )
    context.obj.clean_partials["guests"].append(clean_obj.guests)


@clean.command(name='images')
@click.pass_context
@verbosity_options
@dry_options
def clean_images(context: Context, **kwargs: Any) -> None:
    """
    Remove images of supported provision methods.

    Currently supported methods are:
     - testcloud
    """
    # FIXME: If there are more provision methods supporting this,
    #        we should add options to specify which provision should be
    #        cleaned, similarly to guests.
    assert context.obj.clean_logger is not None  # narrow type

    clean_obj = tmt.Clean(
        logger=context.obj.clean_logger
        .descend(logger_name='clean-images', extra_shift=0)
        .apply_verbosity_options(**kwargs),
        parent=context.obj.clean,
        context=context
        )
    context.obj.clean_partials["images"].append(clean_obj.images)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Lint
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@main.command(name='lint')
@click.pass_context
@filter_options
@fmf_source_options
@fix_options
@verbosity_options
def lint(context: Context, **kwargs: Any) -> None:
    """
    Check all the present metadata against the specification.

    Combines all the partial linting (tests, plans and stories)
    into one command. Options are applied to all parts of the lint.

    Regular expression can be used to filter metadata by name.
    Use '.' to select tests, plans and stories under the current
    working directory.
    """
    exit_code = 0
    for command in (tests_lint, plans_lint, stories_lint):
        try:
            context.forward(command)
        except SystemExit as e:
            # SystemExit.code is Union[str, int, None], because that's all accepted by
            # `sys.exit()` (see https://docs.python.org/3.9/library/sys.html#sys.exit).
            # Our code is sane, returns either zero or another integer, so let's add
            # a check & raise an error should we run into an unexpected type. It'd mean
            # subcommands suddenly started returning non-integer exit code, and why would
            # they do anything like that??
            if not isinstance(e.code, int):
                raise tmt.utils.GeneralError(f"Unexpected non-integer exit code '{e.code}'.")
            exit_code |= e.code
    raise SystemExit(exit_code)

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Setup
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@main.group(cls=CustomGroup)
@click.pass_context
def setup(context: Context, **kwargs: Any) -> None:
    """
    Setup the environment for working with tmt.
    """


@setup.group(cls=CustomGroup)
@click.pass_context
def completion(context: Context, **kwargs: Any) -> None:
    """
    Setup shell completions.

    By default, these commands only write a shell script to the output
    which can then be sourced from the shell's configuration file. Use
    the '--install' option to store and enable the configuration
    permanently.
    """


COMPLETE_VARIABLE = '_TMT_COMPLETE'
COMPLETE_SCRIPT = 'tmt-complete'


def setup_completion(shell: str, install: bool) -> None:
    """ Setup completion based on the shell """
    config = tmt.utils.Config()
    # Fish gets installed into its special location where it is automatically
    # loaded.
    if shell == 'fish':
        script = Path('~/.config/fish/completions/tmt.fish').expanduser()
    # Bash and zsh get installed to tmt's config directory.
    else:
        script = Path(config.path) / f'{COMPLETE_SCRIPT}.{shell}'

    out = open(script, 'w') if install else sys.stdout
    subprocess.run(f'{COMPLETE_VARIABLE}={shell}_source tmt',
                   shell=True, stdout=out)

    if install:
        out.close()
        # If requested, modify .bashrc or .zshrc
        if shell != 'fish':
            config_path = Path(f'~/.{shell}rc').expanduser()
            with open(config_path, 'a') as shell_config:
                shell_config.write('\n# Generated by tmt\n')
                shell_config.write(f'source {script}')


@completion.command(name='bash')
@click.pass_context
@click.option(
    '--install', '-i', 'install', is_flag=True,
    help="Persistently store the script to tmt's configuration directory "
         "and set it up by modifying '~/.bashrc'.")
def completion_bash(context: Context, install: bool, **kwargs: Any) -> None:
    """
    Setup shell completions for bash.
    """
    setup_completion('bash', install)


@completion.command(name='zsh')
@click.pass_context
@click.option(
    '--install', '-i', 'install', is_flag=True,
    help="Persistently store the script to tmt's configuration directory "
         "and set it up by modifying '~/.zshrc'.")
def completion_zsh(context: Context, install: bool, **kwargs: Any) -> None:
    """
    Setup shell completions for zsh.
    """
    setup_completion('zsh', install)


@completion.command(name='fish')
@click.pass_context
@click.option(
    '--install', '-i', 'install', is_flag=True,
    help="Persistently store the script to "
         "'~/.config/fish/completions/tmt.fish'.")
def completion_fish(context: Context, install: bool, **kwargs: Any) -> None:
    """
    Setup shell completions for fish.
    """
    setup_completion('fish', install)
