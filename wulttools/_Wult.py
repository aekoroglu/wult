# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2019-2022 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>

"""
wult - a tool for measuring C-state latency.
"""

import sys
import logging
from pathlib import Path

try:
    import argcomplete
except ImportError:
    # We can live without argcomplete, we only lose tab completions.
    argcomplete = None

from pepclibs.helperlibs import Logging, Human, ArgParse
from pepclibs.helperlibs.Exceptions import Error
from wultlibs import Deploy, ToolsCommon
from wulttools import _WultCommon

VERSION = "1.10.17"
OWN_NAME = "wult"

LOG = logging.getLogger()
Logging.setup_logger(prefix=OWN_NAME)

def build_arguments_parser():
    """Build and return the arguments parser object."""

    text = f"{OWN_NAME} - a tool for measuring C-state latency."
    parser = ArgParse.SSHOptsAwareArgsParser(description=text, prog=OWN_NAME, ver=VERSION)

    text = "Force coloring of the text output."
    parser.add_argument("--force-color", action="store_true", help=text)
    subparsers = parser.add_subparsers(title="commands", metavar="")
    subparsers.required = True

    #
    # Create parsers for the "deploy" command.
    #
    Deploy.add_deploy_cmdline_args(OWN_NAME, subparsers, deploy_command, argcomplete=argcomplete)

    #
    # Create parsers for the "scan" command.
    #
    text = "Scan for device id."
    descr = """Scan for compatible devices."""
    subpars = subparsers.add_parser("scan", help=text, description=descr)
    subpars.set_defaults(func=ToolsCommon.scan_command)

    ArgParse.add_ssh_options(subpars)

    #
    # Create parsers for the "start" command.
    #
    text = "Start the measurements."
    descr = """Start measuring and recording C-state latency."""
    subpars = subparsers.add_parser("start", help=text, description=descr)
    subpars.set_defaults(func=start_command)

    ArgParse.add_ssh_options(subpars)

    subpars.add_argument("-c", "--datapoints", default=1000000, metavar="COUNT", dest="dpcnt",
                         help=ToolsCommon.DATAPOINTS_DESCR)
    subpars.add_argument("--time-limit", dest="tlimit", metavar="LIMIT",
                         help=ToolsCommon.TIME_LIMIT_DESCR)
    subpars.add_argument("--exclude", action=ArgParse.OrderedArg, help=ToolsCommon.EXCL_START_DESCR)
    subpars.add_argument("--include", action=ArgParse.OrderedArg, help=ToolsCommon.INCL_DESCR)
    text = f"""{ToolsCommon.KEEP_FILTERED_DESCR} Here is an example. Suppose you want to collect
               100000 datapoints where PC6 residency is greater than 0. In this case, you can use
               these options: -c 100000 --exclude="PC6%% == 0". The result will contain 100000
               datapoints, all of them will have non-zero PC6 residency. But what if you do not want
               to simply discard the other datapoints, because they are also interesting? Well, add
               the '--keep-filtered' option. The result will contain, say, 150000 datapoints, 100000
               of which will have non-zero PC6 residency."""
    subpars.add_argument("--keep-filtered", action="store_true", help=text)

    arg = subpars.add_argument("-o", "--outdir", type=Path, help=ToolsCommon.START_OUTDIR_DESCR)
    if argcomplete:
        arg.completer = argcomplete.completers.DirectoriesCompleter()

    subpars.add_argument("--reportid", help=ToolsCommon.START_REPORTID_DESCR)

    text = """Comma-separated list of statistics to collect. The statistics are collected in
              parallel with measuring C-state latency. They are stored in the the "stats"
              sub-directory of the output directory. By default, only 'sysinfo' statistics are
              collected. Use 'all' to collect all possible statistics. Use '--stats=""' or
              --stats='none' to disable statistics collection. If you know exactly what statistics
              you need, specify the comma-separated list of statistics to collect. For example, use
              'turbostat,acpower' if you need only turbostat and AC power meter statistics. You can
              also specify the statistics you do not want to be collected by pre-pending the '!'
              symbol. For example, 'all,!turbostat' would mean: collect all the statistics supported
              by the SUT, except for 'turbostat'.  Use the '--list-stats' option to get more
              information about available statistics. By default, only 'sysinfo' statistics are
              collected."""
    subpars.add_argument("--stats", default="sysinfo", help=text)

    text = """The intervals for statistics. Statistics collection is based on doing periodic
              snapshots of data. For example, by default the 'acpower' statistics collector reads
              SUT power consumption for the last second every second, and 'turbostat' default
              interval is 5 seconds. Use 'acpower:5,turbostat:10' to increase the intervals to 5 and
              10 seconds correspondingly.  Use the '--list-stats' to get the default interval
              values."""
    subpars.add_argument("--stats-intervals", help=text)

    text = f"""Print information about the statistics '{OWN_NAME}' can collect and exit."""
    subpars.add_argument("--list-stats", action="store_true", help=text)

    text = f"""This tool works by scheduling a delayed event, then sleeping and waiting for it to
                happen. This step is referred to as a "measurement cycle" and it is usually repeated
                many times. The launch distance defines how far in the future the delayed event is
                sceduled. By default this tool randomly selects launch distance within a range. The
                default range is [0,4ms], but you can override it with this option. Specify a
                comma-separated range (e.g '--ldist 10,5000'), or a single value if you want launch
                distance to be precisely that value all the time.  The default unit is microseconds,
                but you can use the following specifiers as well: {Human.DURATION_NS_SPECS_DESCR}.
                For example, '--ldist 10us,5ms' would be a [10,5000] microseconds range. Too small
                values may cause failures or prevent the SUT from reaching deep C-states. If the
                range starts with 0, the minimum possible launch distance value allowed by the
                delayed event source will be used. The optimal launch distance range is
                system-specific."""
    subpars.add_argument("-l", "--ldist", help=text, default="0,4000")

    text = """The logical CPU number to measure, default is CPU 0."""
    subpars.add_argument("--cpunum", help=text, type=int, default=0)

    text = f"""{OWN_NAME.title()} receives raw datapoints from the driver, then processes them, and
               then saves the processed datapoint in the 'datapoints.csv' file. The processing
               involves converting TSC cycles to microseconds, so {OWN_NAME} needs SUT's TSC rate.
               TSC rate is calculated from the datapoints, which come with TSC counters and
               timestamps, so TSC rate can be calculated as "delta TSC / delta timestamp". In other
               words, {OWN_NAME} needs two datapoints to calculate TSC rate. However, the datapoints
               have to be far enough apart, and this option defines the distance between the
               datapoints (in seconds). The default distance is 10 seconds, which means that
               {OWN_NAME} will keep collecting and buffering datapoints for 10s without processing
               them (because processing requires TSC rate to be known). After 10s, {OWN_NAME} will
               start processing all the buffered datapoints, and then the newly collected
               datapoints. Generally, longer TSC calculation time translates to better accuracy."""
    subpars.add_argument("--tsc-cal-time", default="10s", help=text)

    text = f"""{OWN_NAME.title()} receives raw datapoints from the driver, then processes them, and
               then saves the processed datapoint in the 'datapoints.csv' file. In order to keep the
               CSV file smaller, {OWN_NAME} keeps only the esential information, and drops the rest.
               For example, raw timestamps are dropped. With this option, however, {OWN_NAME} saves
               all the raw data to the CSV file, along with the processed data."""
    subpars.add_argument("--keep-raw-data", action="store_true", dest="keep_rawdp", help=text)

    text = f"""This option exists for debugging and troubleshooting purposes. Please, do not use
               for other reasons. While normally {OWN_NAME} kernel modules are unloaded after the
               measurements are done, with this option the modules will stay loaded into the
               kernel. Keep in mind that if the the specified 'devid' device was bound to some
               driver (e.g., a network driver), it will be unbinded and with this option it won't be
               binded back."""
    subpars.add_argument("--no-unload", action="store_true", help=text)

    text = """This option is for research purposes and you most probably do not need it. Linux's
              'cpuidle' subsystem enters most C-states with interrupts disabled. So when the CPU
              exits the C-state becaouse of an interrupt, it will not jump to the interrupt
              handler, but instead, continue running some 'cpuidle' housekeeping code. After this,
              the 'cpuidle' subsystem enables interrupts, and the CPU jumps to the interrupt
              hanlder. Therefore, there is a tiny delay the 'cpuidle' subsystem adds on top of the
              hardware C-state latency. For fast C-states like C1, this tiny delay may even be
              measurable on some platforms. This option allows to measure that delay. It makes wult
              enable interrupts before linux enters the C-state."""
    subpars.add_argument("--early-intr", action="store_true", help=text)

    subpars.add_argument("--report", action="store_true", help=ToolsCommon.START_REPORT_DESCR)
    subpars.add_argument("--force", action="store_true", help=ToolsCommon.START_FORCE_DESCR)

    text = """The ID of the device to use for measuring the latency. For example, it can be a PCI
              address of the Intel I210 device, or "tdt" for the TSC deadline timer block of the
              CPU. Use the 'scan' command to get supported devices."""
    subpars.add_argument("devid", nargs= '?' if '--list-stats' in sys.argv else None, help=text)

    #
    # Create parsers for the "report" command.
    #
    text = "Create an HTML report."
    descr = """Create an HTML report for one or multiple test results."""
    subpars = subparsers.add_parser("report", help=text, description=descr)
    subpars.set_defaults(func=report_command)

    subpars.add_argument("-o", "--outdir", type=Path,
                         help=ToolsCommon.get_report_outdir_descr(OWN_NAME))
    subpars.add_argument("--exclude", action=ArgParse.OrderedArg, help=ToolsCommon.EXCL_DESCR)
    subpars.add_argument("--include", action=ArgParse.OrderedArg, help=ToolsCommon.INCL_DESCR)
    subpars.add_argument("--even-up-dp-count", action="store_true", dest="even_dpcnt",
                         help=ToolsCommon.EVEN_UP_DP_DESCR)
    subpars.add_argument("-x", "--xaxes",
                         help=ToolsCommon.XAXES_DESCR % _WultCommon.get_axes("xaxes"))
    subpars.add_argument("-y", "--yaxes",
                         help=ToolsCommon.YAXES_DESCR % _WultCommon.get_axes("yaxes"))
    subpars.add_argument("--hist", help=ToolsCommon.HIST_DESCR % _WultCommon.get_axes("hist"))
    subpars.add_argument("--chist", help=ToolsCommon.CHIST_DESCR % _WultCommon.get_axes("chist"))
    subpars.add_argument("--reportids", help=ToolsCommon.REPORTIDS_DESCR)
    subpars.add_argument("--title-descr", help=ToolsCommon.TITLE_DESCR)
    subpars.add_argument("--relocatable", action="store_true", help=ToolsCommon.RELOCATABLE_DESCR)
    subpars.add_argument("--list-metrics", action="store_true", help=ToolsCommon.LIST_METRICS_DESCR)

    text = """Generate HTML report with a pre-defined set of diagrams and histograms. Possible
              values: 'small', 'medium' or 'large'. This option is mutually exclusive with
              '--xaxes', '--yaxes', '--hist', '--chist'."""
    subpars.add_argument("--size", dest="report_size", type=str, help=text)

    text = f"""One or multiple {OWN_NAME} test result paths."""
    subpars.add_argument("respaths", nargs="+", type=Path, help=text)

    #
    # Create parsers for the "filter" command.
    #
    text = "Filter datapoints out of a test result."
    subpars = subparsers.add_parser("filter", help=text, description=ToolsCommon.FILT_DESCR)
    subpars.set_defaults(func=ToolsCommon.filter_command)

    subpars.add_argument("--exclude", action=ArgParse.OrderedArg, help=ToolsCommon.EXCL_DESCR)
    subpars.add_argument("--include", action=ArgParse.OrderedArg, help=ToolsCommon.INCL_DESCR)
    subpars.add_argument("--exclude-metrics", action=ArgParse.OrderedArg, dest="mexclude",
                         help=ToolsCommon.MEXCLUDE_DESCR)
    subpars.add_argument("--include-metrics", action=ArgParse.OrderedArg, dest="minclude",
                         help=ToolsCommon.MINCLUDE_DESCR)
    subpars.add_argument("--human-readable", action="store_true",
                         help=ToolsCommon.FILTER_HUMAN_DESCR)
    subpars.add_argument("-o", "--outdir", type=Path, help=ToolsCommon.FILTER_OUTDIR_DESCR)
    subpars.add_argument("--list-metrics", action="store_true", help=ToolsCommon.LIST_METRICS_DESCR)
    subpars.add_argument("--reportid", help=ToolsCommon.FILTER_REPORTID_DESCR)

    text = f"The {OWN_NAME} test result path to filter."
    subpars.add_argument("respath", type=Path, help=text)

    #
    # Create parsers for the "calc" command.
    #
    text = f"Calculate summary functions for a {OWN_NAME} test result."
    descr = f"""Calculates various summary functions for a {OWN_NAME} test result (e.g., the median
                value for one of the CSV columns)."""
    subpars = subparsers.add_parser("calc", help=text, description=descr)
    subpars.set_defaults(func=ToolsCommon.calc_command)

    subpars.add_argument("--exclude", action=ArgParse.OrderedArg, help=ToolsCommon.EXCL_DESCR)
    subpars.add_argument("--include", action=ArgParse.OrderedArg, help=ToolsCommon.INCL_DESCR)
    subpars.add_argument("--exclude-metrics", action=ArgParse.OrderedArg, dest="mexclude",
                         help=ToolsCommon.MEXCLUDE_DESCR)
    subpars.add_argument("--include-metrics", action=ArgParse.OrderedArg, dest="minclude",
                         help=ToolsCommon.MINCLUDE_DESCR)
    subpars.add_argument("-f", "--funcs", help=ToolsCommon.FUNCS_DESCR)
    subpars.add_argument("--list-funcs", action="store_true", help=ToolsCommon.LIST_FUNCS_DESCR)

    text = f"""The {OWN_NAME} test result path to calculate summary functions for."""
    subpars.add_argument("respath", type=Path, help=text)

    if argcomplete:
        argcomplete.autocomplete(parser)

    return parser

def parse_arguments():
    """Parse input arguments."""

    parser = build_arguments_parser()

    args = parser.parse_args()
    args.toolname = OWN_NAME
    args.toolver = VERSION

    return args

def deploy_command(args):
    """Implements the 'wult deploy' command."""

    from wulttools import _WultDeploy # pylint: disable=import-outside-toplevel

    _WultDeploy.deploy_command(args)

def start_command(args):
    """Implements the 'wult start' command."""

    from wulttools import _WultStart # pylint: disable=import-outside-toplevel

    _WultStart.start_command(args)

def report_command(args):
    """Implements the 'wult report' command."""

    from wulttools import _WultReport # pylint: disable=import-outside-toplevel

    _WultReport.report_command(args)

def main():
    """Script entry point."""

    try:
        args = parse_arguments()

        if not getattr(args, "func", None):
            LOG.error("please, run '%s -h' for help.", OWN_NAME)
            return -1

        args.func(args)
    except KeyboardInterrupt:
        LOG.info("Interrupted, exiting")
        return -1
    except Error as err:
        LOG.error_out(err)
        return -1

    return 0

# The script entry point.
if __name__ == "__main__":
    sys.exit(main())
