# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2019-2022 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Authors: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>
#          Vladislav Govtva <vladislav.govtva@intel.com>

"""
This module provides the capability of generating plots and diagrams using the "Plotly" library for
Metric Tabs.
"""

from pepclibs.helperlibs.Exceptions import Error
from wultlibs.htmlreport import _ScatterPlot, _Histogram

class PlotsBuilder:
    """
    This class provides the capability of generating plots and diagrams using the "Plotly"
    library for Metric Tabs.

    Public method overview:
    1. Build histograms and cumulative histograms.
        * build_histograms()
    2. Build scatter plots.
        * build_scatter()
    """

    def _base_unit(self, df, colname):
        """
        Convert columns with 'microsecond' units to seconds, and return the converted column.
        """

        # This is not generic, but today we have to deal only with microseconds, so this is good
        # enough.
        if self._refdefs.info[colname].get("unit") != "microsecond":
            return df[colname]

        base_colname = f"{colname}_base"
        if base_colname not in df:
            df.loc[:, base_colname] = df[colname] / 1000000
        return df[base_colname]

    @staticmethod
    def _get_base_si_unit(unit):
        """
        Plotly will handle SI unit prefixes therefore we should provide only the base unit.
        Several defs list 'us' as the 'short_unit' which includes the prefix so should be
        reduced to just the base unit 's'.
        """

        # This is not generic, but today we have to deal only with microseconds, so this is good
        # enough.
        if unit == "us":
            return "s"
        return unit

    def build_scatter(self, xdef, ydef):
        """
        Create scatter plots with the metric represented by 'xdef' on the X-axis and the metric
        represented by 'ydef' on the Y-axis using data from 'rsts' which is provided to the class
        during initialisation. Returns the filepath of the generated plot HTML.
        """

        xmetric = xdef["name"]
        ymetric = ydef["name"]
        xaxis_label = xdef.get("title", xdef)
        yaxis_label = ydef.get("title", xdef)
        xaxis_fsname = xdef.get("fsname", xaxis_label)
        yaxis_fsname = ydef.get("fsname", yaxis_label)
        xaxis_unit = self._get_base_si_unit(xdef.get("short_unit", ""))
        yaxis_unit = self._get_base_si_unit(ydef.get("short_unit", ""))

        fname = yaxis_fsname + "-vs-" + xaxis_fsname + ".html"
        outpath = self.outdir / fname

        plot = _ScatterPlot.ScatterPlot(xmetric, ymetric, outpath, xaxis_label=xaxis_label,
                                        yaxis_label=yaxis_label, xaxis_unit=xaxis_unit,
                                        yaxis_unit=yaxis_unit)

        for res in self._rsts:
            # Check that each result contains data for 'xmetric' and 'ymetric'.
            for metric in [xmetric, ymetric]:
                if metric not in res.df:
                    raise Error(f"cannot build scatter plots. Metric '{metric}' not available for "
                                f"result '{res.reportid}'.")

            df = plot.reduce_df_density(res.df, res.reportid)
            hov_defs = [res.defs.info[metric] for metric in self._hov_metrics[res.reportid]]
            text = plot.get_hover_text(hov_defs, df)
            df[xmetric] = self._base_unit(df, xmetric)
            df[ymetric] = self._base_unit(df, ymetric)
            plot.add_df(df, res.reportid, text)

        plot.generate()
        return outpath

    def _build_histogram(self, xmetric, xbins, xaxis_label, xaxis_unit, cumulative=False):
        """
        Helper function for 'build_histograms()'. Create a histogram or cumulative histogram with
        'xmetric' on the x-axis data from 'self._rsts'. Returns the filepath of the generated plot
        HTML.
        """

        if cumulative:
            fname = f"Count-vs-{self._refdefs.info[xmetric]['fsname']}.html"
        else:
            fname = f"Percentile-vs-{self._refdefs.info[xmetric]['fsname']}.html"

        outpath = self.outdir / fname

        hst = _Histogram.Histogram(xmetric, outpath, xaxis_label, xaxis_unit, xbins=xbins,
                                   cumulative=cumulative)

        for res in self._rsts:
            df = res.df
            df[xmetric] = self._base_unit(df, xmetric)
            hst.add_df(df, res.reportid)
        hst.generate()
        return outpath

    def _get_xbins(self, xcolname):
        """
        Helper function for 'build_histograms()'. Returns the 'xbins' dictionary for plotly's
        'Histogram()' method.
        """

        xmin, xmax = (float("inf"), -float("inf"))
        for res in self._rsts:
            # In case of non-numeric column there is only one x-value per bin.
            if not res.is_numeric(xcolname):
                return {"size" : 1}

            xdata = self._base_unit(res.df, xcolname)
            xmin = min(xmin, xdata.min())
            xmax = max(xmax, xdata.max())

        return {"size" : (xmax - xmin) / 1000}

    def build_histograms(self, xmetric, hist=False, chist=False):
        """
        Create a histogram and/or cumulative histogram with 'xmetric' on the x-axis using data from
        'rsts' which is provided to the class during initialisation. Returns the filepath of the
        generated plot HTML.
        """

        # Check that all results contain data for 'xmetric'.
        if any(xmetric not in res.df for res in self._rsts):
            raise Error(f"cannot build histograms. Metric '{xmetric}' not available for all "
                        f"results.")

        xbins = self._get_xbins(xmetric)

        xaxis_def = self._refdefs.info.get(xmetric, {})
        xaxis_label = xaxis_def.get("title", xmetric)
        xaxis_unit = self._get_base_si_unit(xaxis_def.get("short_unit", ""))

        ppaths = []
        if hist:
            ppaths.append(self._build_histogram(xmetric, xbins, xaxis_label, xaxis_unit))

        if chist:
            ppaths.append(self._build_histogram(xmetric, xbins, xaxis_label, xaxis_unit,
                                                cumulative=True))
        return ppaths

    def __init__(self, rsts, hov_metrics, opacity, outdir):
        """
        The class constructor. The arguments are as follows:
         * rsts - list of 'RORawResult' objects representing the raw test results to generate the
                  plots for.
         * hov_metrics - a mapping from report_id to metric names which should be included in the
                         hovertext of scatter plots.
         * opacity - opacity of plot points on scatter diagrams.
         * outdir - the directory path which will store the HTML plots.
        """

        self._hov_metrics = hov_metrics
        self._opacity = opacity
        self.outdir = outdir

        self._rsts = rsts
        # The reference definitions - it contains helpful information about every CSV file column,
        # for example the title, units, and so on.
        self._refdefs = rsts[0].defs
