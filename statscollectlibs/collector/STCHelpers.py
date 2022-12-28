# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2019-2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>

"""
This module implements several misc. helpers for tools using 'StatsCollect'.
"""

import contextlib
import logging
from pepclibs.helperlibs import LocalProcessManager, Trivial
from pepclibs.helperlibs.Exceptions import Error
from statscollectlibs.collector import StatsCollect
from statscollectlibs.deploylibs import Deploy

_LOG = logging.getLogger()

_DEFAULT_STCONF = {
        "discover"  : False,
        "include"   : set(),
        "exclude"   : set(),
        "intervals" : {},
}

def parse_stnames(stnames, stconf=None):
    """
    Parse the statistics names string and return the result in form of a dictionary. The arguments
    are as follows:
      * stnames - a string containing a comma-separated list of statistic names. The "!" symbol at
                  the beginning of a statistics name means that this statistics should not be
                  collected. The spacial "all" name means that all the discovered statistics should
                  be included.
      * stconf - an optional statistics configuration dictionary to fill with the results of
                 parsing 'stnames'.

    This function adds statistics names to the following keys to the resulting statistics
    configuration dictionary ('stconf').
      * include: statistics names that should be collected.
      * exclude: statistics names that should not be collected.
      * discover: if 'True', then include all the discovered statistics except for those in
                  'exclude'.

    Returns the resulting statistics configuration dictionary.
    """

    if not stconf:
        stconf = _DEFAULT_STCONF

    for stname in Trivial.split_csv_line(stnames):
        if stname == "all":
            stconf["discover"] = True
        elif stname.startswith("!"):
            # The "!" prefix indicates that the statistics must not be collected.
            stname = stname[1:]
            stconf["exclude"].add(stname)
        else:
            stconf["include"].add(stname)

    bogus = stconf["include"] & stconf["exclude"]
    if bogus:
        bogus = ", ".join(bogus)
        raise Error(f"cannot simultaneously include and exclude the following statistics: {bogus}")

    StatsCollect.check_stnames(stconf["include"])
    StatsCollect.check_stnames(stconf["exclude"])

    return stconf

def parse_intervals(intervals, stconf=None):
    """
    Parse a string containing statistics collector's intervals and return the result in form of a
    dictionary. The arguments are as follows:
      * intervals - a comma-separated list of "stname:interval" entries, where 'stname' is the
                    statistics name, and 'interval' is the desired collection interval in seconds.
      * stconf - an optional statistics configuration dictionary to fill with the results of
                 parsing 'intervals'.

    This function adds statistics collection intervals to the "intervals" keys to the resulting
    statistics configuration dictionary ('stconf'). The interval values are floating point numbers.

    Returns the resulting statistics configuration dictionary.
    """

    if not stconf:
        stconf = _DEFAULT_STCONF

    for entry in Trivial.split_csv_line(intervals):
        split = Trivial.split_csv_line(entry, sep=":")
        if len(split) != 2:
            raise Error(f"bad intervals entry '{entry}', should be 'stname:interval', where "
                        f"'stname' is the statistics name and 'interval' is a floating point "
                        f"interval for collecting the 'stname' statistics.")
        stname, interval = split
        StatsCollect.check_stname(stname)

        if not Trivial.is_float(interval):
            raise Error(f"bad interval value '{interval}' for the '{stname}' statistics: should "
                        f"be a positive floating point or integer number")

        stconf["intervals"][stname] = float(interval)

    return stconf

def apply_stconf(stcoll, stconf):
    """
    Configure statistics collector by applying the statistics configuration from 'stconf'. The
    arguments are as follows.
      * stcoll - the 'StatsCollect' object to configure.
      * stconf - the statistics configuration dictionary to apply to 'stcoll'.

    This helper function applies 'stconf' to 'stcoll' and runs 'stcoll.configure()'.
    """

    stcoll.set_intervals(stconf["intervals"])

    if stconf["discover"]:
        stcoll.set_enabled_stats("all")
        stcoll.set_disabled_stats(stconf["exclude"])

        discovered = stcoll.discover()

        # Make sure that all the required statistics are actually available.
        not_found = stconf["include"] - (discovered & stconf["include"])
        if not_found:
            not_found = ", ".join(not_found)
            raise Error(f"the following statistics cannot be collected: {not_found}")

        stcoll.set_disabled_stats("all")
        stcoll.set_enabled_stats(discovered)
    else:
        stcoll.set_disabled_stats("all")
        stcoll.set_enabled_stats(stconf["include"])

    stcoll.configure()

def create_and_configure_stcoll(stnames, intervals, outdir, pman):
    """
    This helper creates an instance of 'StatsCollect' and configures it based on a required list of
    statistics and related options. The arguments are as follows:
     * stnames - a string which will be passed to 'parse_stnames()' see that function's docstring
                 for more info.
     * intervals - a string which will be passed to 'parse_intervals()' see that function's
                   docstring for more info.
     * outdir - an output directory path on the for storing the 'stc-agent' logs and results (the
                collected statistics).
     * pman - the process manager object associated with the SUT (the host to collect the
              statistics for).
    """

    if not stnames or stnames == "none":
        return None

    stconf = parse_stnames(stnames)

    stcoll = StatsCollect.StatsCollect(pman, local_outdir=outdir)
    stcoll.set_info_logging(True)

    if intervals:
        stconf = parse_intervals(intervals, stconf=stconf)
        stcoll.set_intervals(stconf["intervals"])

    if stconf["discover"]:
        stcoll.set_enabled_stats("all")
        stcoll.set_disabled_stats(stconf["exclude"])
    else:
        stcoll.set_disabled_stats("all")
        stcoll.set_enabled_stats(stconf["include"])

    if "acpower" in stcoll.get_enabled_stats():
        # Assume that power meter is configured to match the SUT name.
        if pman.is_remote:
            devnode = pman.hostname
        else:
            devnode = "default"

        with contextlib.suppress(Error):
            stcoll.set_prop("acpower", "devnode", devnode)

    # Configure the 'stc-agent' program path.
    local_needed, remote_needed = stcoll.is_stcagent_needed()
    local_path, remote_path = (None, None)

    if local_needed:
        with LocalProcessManager.LocalProcessManager() as lpman:
            local_path = Deploy.get_installed_helper_path(lpman, "stats-collect", "stc-agent")
    if remote_needed:
        remote_path = Deploy.get_installed_helper_path(pman, "stats-collect", "stc-agent")

    stcoll.set_stcagent_path(local_path=local_path, remote_path=remote_path)

    if stconf["discover"]:
        discovered = stcoll.discover()

        # Make sure that all the required statistics are actually available.
        not_found = stconf["include"] - (discovered & stconf["include"])
        if not_found:
            not_found = ", ".join(not_found)
            raise Error(f"the following statistics cannot be collected: {not_found}")

        stcoll.set_disabled_stats("all")
        stcoll.set_enabled_stats(discovered)
        stcoll.set_enabled_stats("all")
        stcoll.set_disabled_stats(stconf["exclude"])

    stcoll.configure()

    if not stcoll.get_enabled_stats():
        _LOG.info("No statistics will be collected")
        stcoll.close()
        return None

    return stcoll
