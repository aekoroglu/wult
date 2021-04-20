# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2020-2021 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Antti Laakso <antti.laakso@linux.intel.com>

"""
This module provides API for managing settings in MSR 0x1FC (MSR_POWER_CTL). This is a
model-specific register found on many Intel platforms.
"""

from wultlibs.helperlibs import Procs
from wultlibs.pepclibs import CPUInfo
from wultlibs.pepclibs.msr import MSR
from wultlibs.helperlibs.Exceptions import ErrorNotSupported

# Power control Model Specific Register.
MSR_POWER_CTL = 0x1FC
C1E_ENABLE = 1
CSTATE_PREWAKE_DISABLE = 30
# Indicates whether dynamic switching is enabled in power perf tuning algorithm. Available on ICX.
PWR_PERF_TUNING_ENABLE_DYN_SWITCHING = 33

# Dynamic Load Line feature is available only on some CPUs.
_DLL_CPUS = (CPUInfo.INTEL_FAM6_ICELAKE_X, CPUInfo.INTEL_FAM6_ICELAKE_D)

class PowerCtl:
    """
    This class provides API for managing settings in MSR 0x1FC (MSR_POWER_CTL). This is a
    model-specific register found on many Intel platforms.
    """

    def c1e_autopromote_enabled(self, cpu):
        """
        Returns 'True' if C1E autopromotion is enabled for CPU 'cpu', otherwise returns 'False'.
        """

        regval = self._msr.read(MSR_POWER_CTL, cpu=cpu)
        return MSR.is_bit_set(C1E_ENABLE, regval)

    def set_c1e_autopromote(self, enable: bool, cpus="all"):
        """
        Enable or disable C1E autopromote for CPUs 'cpus'. The 'cpus' argument is the same as the
        'cpus' argument of the 'CPUIdle.get_cstates_info()' function - please, refer to the
        'CPUIdle' module for the exact format description.
        """
        self._msr.toggle_bit(MSR_POWER_CTL, C1E_ENABLE, enable, cpus=cpus)

    def cstate_prewake_enabled(self, cpu):
        """Returns 'True' if C-state prewake is enabled for CPU 'cpu', otherwise returns 'False'."""

        regval = self._msr.read(MSR_POWER_CTL, cpu=cpu)
        return not MSR.is_bit_set(CSTATE_PREWAKE_DISABLE, regval)

    def set_cstate_prewake(self, enable: bool, cpus="all"):
        """
        Enable or disable C-state prewake for CPUs 'cpus'. The 'cpus' argument is the same as in
        'set_c1e_autopromote()'.
        """
        self._msr.toggle_bit(MSR_POWER_CTL, CSTATE_PREWAKE_DISABLE, not enable, cpus=cpus)

    def _check_dll_support(self):
        """
        Check if Dynamic Load Line feature is supported by host 'self._proc'. Raise an
        'ErrorNotSupported' if DLL is not supported.
        """

        model = self._lscpu_info["model"]

        if model not in _DLL_CPUS:
            fmt = "%s (CPU model %#x)"
            cpus_str = "\n* ".join([fmt % (CPUInfo.CPU_DESCR[model], model) for model in _DLL_CPUS])
            raise ErrorNotSupported(f"dynamic load line feature is not supported"
                                    f"{self._proc.hostmsg} - CPU '{self._lscpu_info['vendor']}, "
                                    f"(CPU model {hex(model)})' is not supported.\nThe supported "
                                    f"CPUs are:\n* {cpus_str}")

    def set_dll(self, enable: bool, cpus="all"):
        """
        Enable or disable Dynamic Load Line (DLL) for CPUs 'cpus'. The 'cpus' argument is the same
        as in 'set_c1e_autopromote()'.
        """

        self._check_dll_support()
        self._msr.toggle_bit(MSR_POWER_CTL, PWR_PERF_TUNING_ENABLE_DYN_SWITCHING, enable, cpus=cpus)

    def dll_enabled(self, cpu):
        """
        Returns 'True' if Dynamic Load Line feature is enabled for CPU 'cpu', otherwise returns
        'False'.
        """

        self._check_dll_support()
        regval = self._msr.read(MSR_POWER_CTL, cpu=cpu)
        return MSR.is_bit_set(regval, PWR_PERF_TUNING_ENABLE_DYN_SWITCHING)

    def __init__(self, proc=None, lscpu_info=None):
        """
        The class constructor. The argument are as follows.
          * proc - the 'Proc' or 'SSH' object that defines the host to run the measurements on.
          * lscpu_info - CPU information generated by 'CPUInfo.get_lscpu_info()'.
        """

        if not proc:
            proc = Procs.Proc()

        self._proc = proc
        self._lscpu_info = lscpu_info
        self._msr = MSR.MSR(proc=self._proc)

        if self._lscpu_info is None:
            self._lscpu_info = CPUInfo.get_lscpu_info(proc=self._proc)

        if self._lscpu_info["vendor"] != "GenuineIntel":
            raise ErrorNotSupported(f"unsupported CPU model '{self._lscpu_info['vendor']}', "
                                    f"model-specific register {hex(MSR_POWER_CTL)} (MSR_POWER_CTL) "
                                    f"is not available{self._proc.hostmsg}. MSR_POWER_CTL is "
                                    f"available only on Intel platforms")

    def close(self):
        """Uninitialize the class object."""

        if getattr(self, "_proc", None):
            self._proc = None
        if getattr(self, "_msr", None):
            self._msr.close()
            self._msr = None

    def __enter__(self):
        """Enter the runtime context."""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Exit the runtime context."""
        self.close()
