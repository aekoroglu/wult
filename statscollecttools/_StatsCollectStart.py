# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2022-2023 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Authors: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>
#          Adam Hawley <adam.james.hawley@intel.com>

"""This module includes the "start" 'stats-collect' command implementation."""

import contextlib
import logging
from pathlib import Path
from pepclibs import CPUInfo
from pepclibs.helperlibs import Human, Logging
from pepclibs.helperlibs.Exceptions import Error
from statscollecttools import _Common
from statscollectlibs.deploylibs import _Deploy
from statscollectlibs.collector import StatsCollectBuilder
from statscollectlibs.helperlibs import ProcHelpers, ReportID
from statscollectlibs.rawresultlibs import RawResult

_LOG = logging.getLogger()

def generate_reportid(args, pman):
    """
    If user provided report ID for the 'start' command, this function validates it and returns.
    Otherwise, it generates the default report ID and returns it.
    """

    if not args.reportid and pman.is_remote:
        prefix = pman.hostname
    else:
        prefix = None

    return ReportID.format_reportid(prefix=prefix, reportid=args.reportid,
                                    strftime=f"{args.toolname}-%Y%m%d")

def _generate_report(res, outdir):
    """Implements the 'report' command for start."""

    from statscollectlibs.htmlreport import HTMLReport # pylint: disable=import-outside-toplevel

    stats_paths =  {res.reportid: res.stats_path}
    rep = HTMLReport.HTMLReport(outdir)
    rep.generate_report(stats_paths=stats_paths, title="stats-collect report")

def start_command(args):
    """Implements the 'start' command."""

    with contextlib.ExitStack() as stack:
        pman = _Common.get_pman(args)
        stack.enter_context(pman)

        if args.tlimit:
            args.tlimit = Human.parse_duration(args.tlimit, default_unit="m", name="time limit")

        args.reportid = generate_reportid(args, pman)

        if not args.outdir:
            args.outdir = Path(f"./{args.reportid}")

        cpuinfo = CPUInfo.CPUInfo(pman=pman)
        stack.enter_context(cpuinfo)
        args.cpunum = cpuinfo.normalize_cpu(args.cpunum)

        with _Deploy.DeployCheck("wult", args.toolname, args.deploy_info, pman=pman) as depl:
            depl.check_deployment()

        res = RawResult.RawResult(args.reportid, args.outdir, args.toolver, args.cpunum)

        Logging.setup_stdout_logging(args.toolname, res.logs_path)

        if not args.stats or args.stats == "none":
            raise Error("No statistics specified. Use '--stats' to specify which statistics "
                        "should be collected.")

        stcoll_builder = StatsCollectBuilder.StatsCollectBuilder()
        stcoll_builder.parse_stnames(args.stats)
        if args.stats_intervals:
            stcoll_builder.parse_intervals(args.stats_intervals)

        stcoll = stcoll_builder.build_stcoll(pman, args.outdir)
        if not stcoll:
            return

        stack.enter_context(stcoll)

        stcoll.start()

        proc = pman.run_async(args.cmd)
        _, _, exitcode = proc.wait(timeout=args.tlimit)
        if exitcode is None:
            _LOG.notice("statistics collection stopped because the time limit was reached before "
                        "the command finished executing.")
            ProcHelpers.kill_pids(proc.pid, kill_children=True, must_die=True, pman=pman)

        stcoll.stop()
        stcoll.copy_remote_data()
        res.write_info()

    if args.report:
        _generate_report(res, args.outdir / "html-report")
